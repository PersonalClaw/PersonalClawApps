"""Wikipedia search adapter — keyless, zero-config encyclopedic search.

Wikipedia's public MediaWiki API (``<lang>.wikipedia.org/w/api.php``) serves search
with **no API key**. We use the ``query``+``generator=search`` mode (not bare
OpenSearch) so each hit carries a real extract snippet, not just a title. Links +
snippets only (no synthesized answer, no full page body) — a caller ``web_fetch``es
an article URL to read it.

Encyclopedic, not general web: it's the right floor for factual/reference lookups,
bindable per use-case in Settings → Search alongside the broader web providers.
Outbound HTTP is routed through the net.fetch egress chokepoint (host classification,
byte cap, timeout, redirect-hop re-check, SEL audit) and the JSON is parsed from the
response text.
"""

import logging
import re
from typing import Any
from urllib.parse import quote

from personalclaw.sdk.search import (
    DEFAULT_DEPTH,
    SearchCapabilities,
    SearchHit,
    SearchProvider,
    SearchResult,
)

logger = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (compatible; PersonalClaw/1.0; +https://github.com/personalclaw/personalclaw)"

#: The MediaWiki API of the wiki for ``lang``.
_API = "https://{lang}.wikipedia.org/w/api.php"

#: A Wikipedia language code, the name of one edition (``en``, ``pt-br``, ``zh-min-nan``), or
#: nothing, which is English. Letters and hyphens only, so the code cannot name another host: it
#: goes into the hostname. ``app.json`` gives the settings schema the same pattern, so a value
#: that is not one is refused when it is saved as well as when a search would use it.
_LANGUAGE_CODE = re.compile(r"^\s*(?:[A-Za-z]{2,12}(?:-[A-Za-z]{2,12}){0,2})?\s*$")


class WikipediaProvider(SearchProvider):
    def __init__(self, *, lang: str = "en", timeout_secs: int = 20) -> None:
        self._lang = (lang or "").strip() or "en"
        self._timeout = max(1, int(timeout_secs or 20))

    @property
    def name(self) -> str:
        return "wikipedia"

    @property
    def display_name(self) -> str:
        return "Wikipedia"

    async def is_available(self) -> bool:
        # Keyless public API — always available (no credential/endpoint to resolve).
        return True

    def capabilities(self) -> SearchCapabilities:
        return SearchCapabilities(
            returns_content=False,
            returns_answer=False,
            returns_highlights=False,
            supports_recency=False,
            supports_domains=False,
            supports_fetch=False,
            depths=("balanced",),
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
        import json
        from urllib.parse import urlencode

        from personalclaw.sdk.net import (
            CONNECTOR,
            EgressBlocked,
            egress_policy_for,
            egress_refusal,
            fetch,
        )

        if not _LANGUAGE_CODE.fullmatch(self._lang):
            shown = self._lang if len(self._lang) <= 60 else self._lang[:60] + "…"
            raise RuntimeError(
                f"Wikipedia Search's Language is {shown!r}, which is not a Wikipedia language code "
                "such as en, de or pt-br. Change it in Settings → Providers → Wikipedia Search."
            )
        code = self._lang.lower()
        q = (query or "").strip()
        if not q:
            return SearchResult(results=[], provider=self.name, query=query,
                                depth=self.normalize_depth(depth))
        limit = max(1, min(int(max_results or 10), 20))
        api = _API.format(lang=code)
        # generator=search feeds matching pages into a prop=extracts query so each
        # hit carries an intro snippet; pithumbnail/info give us the canonical URL.
        params: dict[str, Any] = {
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrsearch": q,
            "gsrlimit": limit,
            "prop": "extracts|info",
            "exintro": 1,
            "explaintext": 1,
            "exchars": 320,
            "inprop": "url",
        }
        # Route through the net.fetch egress chokepoint instead of raw httpx — fetch
        # takes no params kwarg, so the query string is built into the URL. The
        # chokepoint re-checks every redirect hop internally, so follow_redirects is
        # both unnecessary and unsafe to bypass here.
        # egress_policy_for layers the owner's Settings → Security → Network egress
        # onto CONNECTOR, so a host they denied is never reached.
        url = f"{api}?{urlencode(params)}"
        try:
            resp = await fetch(
                url, policy=egress_policy_for(CONNECTOR), method="GET", headers={"User-Agent": _UA}
            )
        except EgressBlocked as e:
            raise RuntimeError(egress_refusal(api, e.decision)) from e
        if resp.status != 200:
            raise RuntimeError(f"Wikipedia search failed (HTTP {resp.status})")
        data = json.loads(resp.text)

        pages = ((data or {}).get("query", {}) or {}).get("pages", {}) or {}
        # Preserve relevance order: MediaWiki tags each page with its search index.
        ranked = sorted(
            pages.values(),
            key=lambda p: p.get("index", 1_000_000),
        )
        hits: list[SearchHit] = []
        for p in ranked[:limit]:
            title = p.get("title", "")
            url = p.get("fullurl") or (
                f"https://{code}.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}"
                if title else ""
            )
            if not url:
                continue
            hits.append(SearchHit(
                url=url, title=title,
                snippet=(p.get("extract") or "").strip(),
            ))
        return SearchResult(results=hits, answer="", provider=self.name, query=query,
                            depth=self.normalize_depth(depth))


def create_provider(config: dict[str, Any] | None = None) -> WikipediaProvider:
    """Extension factory — builds the Wikipedia adapter (language is configurable)."""
    config = config or {}
    return WikipediaProvider(
        lang=str(config.get("lang", "en") or "en"),
        timeout_secs=int(config.get("timeout_secs", 20) or 20),
    )
