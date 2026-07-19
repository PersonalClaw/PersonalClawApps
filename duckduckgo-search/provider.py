"""DuckDuckGo search adapter — keyless, zero-config web search.

DuckDuckGo's HTML endpoint (``html.duckduckgo.com``) returns web results with **no API
key and no self-hosted instance** — the true zero-config default. Shipping it
enabled-by-default means ``web_search`` works out-of-box for a user who has configured
no provider at all, which in turn seeds the ``web_fetch`` provenance gate. A user can
still bind a higher-quality provider (Tavily/Exa/…) per use-case in Settings → Search;
DuckDuckGo is the floor, not the ceiling.

Best-effort HTML scrape — DDG exposes no official keyless JSON API. Links + snippets
only (no synthesized answer, no extracted page body), so callers ``web_fetch`` a result
to read it. Parsed with the stdlib HTML parser (no new dependency, more robust than a
regex over result markup).
"""

import logging
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from personalclaw.sdk.search import (
    DEFAULT_DEPTH,
    SearchCapabilities,
    SearchHit,
    SearchProvider,
    SearchResult,
)

logger = logging.getLogger(__name__)

# The HTML endpoint (not the JS app) returns server-rendered result markup.
_API = "https://html.duckduckgo.com/html/"
# A browser-ish UA — DDG returns an empty body to an unidentified client.
_UA = "Mozilla/5.0 (compatible; PersonalClaw/1.0; +https://github.com/personalclaw/personalclaw)"

# Normalized recency word → DDG `df` (date filter) code.
_RECENCY_TO_DF = {"day": "d", "today": "d", "week": "w", "month": "m", "year": "y"}


def _decode_ddg_url(href: str) -> str:
    """DDG wraps result links as ``//duckduckgo.com/l/?uddg=<encoded real url>``.
    Decode to the real target; pass through an already-direct http(s) href."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    try:
        p = urlparse(href)
    except ValueError:
        return href
    if "duckduckgo.com" in (p.netloc or "") and p.path.startswith("/l/"):
        uddg = parse_qs(p.query).get("uddg")
        if uddg:
            return unquote(uddg[0])
    return href


class _DDGResultParser(HTMLParser):
    """Collect (url, title, snippet) per result from DDG's HTML.

    DDG emits, per result and in order, an ``<a class="result__a">`` (url + title)
    then an ``<a class="result__snippet">`` (snippet). We append a result on the
    title anchor's close and attach the snippet to the last result on its close.
    """

    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._mode: str | None = None  # "title" | "snippet"
        self._url = ""
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        cls = dict(attrs).get("class") or ""
        if "result__a" in cls:
            self._mode, self._url, self._buf = "title", dict(attrs).get("href") or "", []
        elif "result__snippet" in cls:
            self._mode, self._buf = "snippet", []

    def handle_data(self, data: str) -> None:
        if self._mode:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or not self._mode:
            return
        text = "".join(self._buf).strip()
        if self._mode == "title":
            url = _decode_ddg_url(self._url)
            if url:
                self.results.append({"url": url, "title": text, "snippet": ""})
        elif self._mode == "snippet" and self.results:
            self.results[-1]["snippet"] = text
        self._mode, self._buf = None, []


class DuckDuckGoProvider(SearchProvider):
    def __init__(self, *, timeout_secs: int = 20) -> None:
        self._timeout = timeout_secs

    @property
    def name(self) -> str:
        return "duckduckgo"

    @property
    def display_name(self) -> str:
        return "DuckDuckGo"

    async def is_available(self) -> bool:
        # Keyless — always available (no credential/endpoint to resolve).
        return True

    def capabilities(self) -> SearchCapabilities:
        return SearchCapabilities(
            returns_content=False,
            returns_answer=False,
            returns_highlights=False,
            supports_recency=True,    # df date filter
            supports_domains=False,
            supports_fetch=False,
            depths=("balanced",),
            keyless=True,    # zero-config floor: no API key; core's out-of-box fallback
        )

    async def search(
        self,
        query: str,
        *,
        depth: str = DEFAULT_DEPTH,
        recency: str | None = None,
        domains: list[str] | None = None,
        max_results: int = 10,
    ) -> SearchResult:
        from urllib.parse import urlencode

        from personalclaw.sdk.net import CONNECTOR, EgressBlocked, fetch

        params: dict[str, Any] = {"q": query}
        df = _RECENCY_TO_DF.get((recency or "").strip().lower())
        if df:
            params["df"] = df

        # Route through the net.fetch egress chokepoint (host classification, byte
        # cap, timeout, redirect-hop re-check, SEL audit) instead of raw httpx —
        # fetch takes no params kwarg, so the query string is built into the URL,
        # and it re-checks each redirect hop internally (safer than follow_redirects).
        url = f"{_API}?{urlencode(params)}"
        try:
            resp = await fetch(url, policy=CONNECTOR, method="GET", headers={"User-Agent": _UA})
        except EgressBlocked as e:
            raise RuntimeError(f"DuckDuckGo search blocked by egress guard: {e}") from e
        if resp.status != 200:
            raise RuntimeError(f"DuckDuckGo search failed (HTTP {resp.status})")
        # DDG's HTML endpoint returns server-rendered result markup (not JSON);
        # .text is the decoded HTML body, fed straight to the parser.
        body = resp.text

        parser = _DDGResultParser()
        parser.feed(body)
        hits: list[SearchHit] = []
        for r in parser.results[:max_results]:
            url = r.get("url", "")
            if url:
                hits.append(SearchHit(url=url, title=r.get("title", ""), snippet=r.get("snippet", "")))
        return SearchResult(results=hits, answer="", provider=self.name, query=query,
                            depth=self.normalize_depth(depth))


def create_provider(config: dict[str, Any] | None = None) -> DuckDuckGoProvider:
    """Extension factory — builds the DuckDuckGo adapter (no settings needed)."""
    config = config or {}
    return DuckDuckGoProvider(timeout_secs=int(config.get("timeout_secs", 20) or 20))
