"""Brave Search adapter — cheap, broad web search (links + snippets).

Brave's Web Search API returns ranked web results with a description and up to a few
``extra_snippets`` per result, plus a ``freshness`` recency filter. Links-only: no
synthesized answer and no extracted page body, so callers ``web_fetch`` a result to
read it. A single authenticated ``GET`` (``X-Subscription-Token`` header).

Native dials mapped from the normalized inputs:
  recency → ``freshness`` (pd=day · pw=week · pm=month · py=year)
Brave has no latency/quality dial, so it advertises only the ``balanced`` depth.
"""

import logging
import os
from typing import Any

from personalclaw.sdk.search import (
    DEFAULT_DEPTH,
    SearchCapabilities,
    SearchHit,
    SearchProvider,
    SearchResult,
)

logger = logging.getLogger(__name__)

_API = "https://api.search.brave.com/res/v1/web/search"

# Normalized recency word → Brave `freshness` code.
_RECENCY_TO_FRESHNESS = {
    "day": "pd", "today": "pd",
    "week": "pw",
    "month": "pm",
    "year": "py",
}


class BraveProvider(SearchProvider):
    def __init__(self, api_key: str = "", *, timeout_secs: int = 20) -> None:
        # Explicit key wins; else the conventional env var, so an exported
        # BRAVE_API_KEY works out-of-box (parity with the Tavily adapter).
        self._api_key = api_key or os.environ.get("BRAVE_API_KEY", "")
        self._timeout = timeout_secs

    @property
    def name(self) -> str:
        return "brave"

    @property
    def display_name(self) -> str:
        return "Brave Search"

    async def is_available(self) -> bool:
        return bool(self._api_key)

    def capabilities(self) -> SearchCapabilities:
        # Links + snippets + server-side recency; no answer, no extracted content,
        # no depth dial → advertise just the balanced depth.
        return SearchCapabilities(
            returns_content=False,
            returns_answer=False,
            returns_highlights=False,
            supports_recency=True,
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

        from personalclaw.sdk.net import CONNECTOR, EgressBlocked, fetch

        if not self._api_key:
            raise RuntimeError("Brave API key is not configured (Settings → Search)")

        params: dict[str, Any] = {"q": query, "count": max(1, min(max_results, 20))}
        fresh = _RECENCY_TO_FRESHNESS.get((recency or "").strip().lower())
        if fresh:
            params["freshness"] = fresh

        # Route through the net.fetch egress chokepoint (host classification, byte
        # cap, timeout, redirect-hop re-check, SEL audit) instead of raw httpx —
        # fetch takes no params kwarg, so the query string is built into the URL.
        url = f"{_API}?{urlencode(params)}"
        try:
            resp = await fetch(
                url, policy=CONNECTOR, method="GET",
                headers={"X-Subscription-Token": self._api_key,
                         "Accept": "application/json"},
            )
        except EgressBlocked as e:
            raise RuntimeError(f"Brave search blocked by egress guard: {e}") from e
        if resp.status != 200:
            raise RuntimeError(f"Brave search failed (HTTP {resp.status})")
        data = json.loads(resp.text)

        # Web results live under web.results; each carries a description + optional
        # extra_snippets (folded into the snippet so the link card is richer).
        web = data.get("web") if isinstance(data, dict) else None
        results = (web or {}).get("results") if isinstance(web, dict) else None
        hits: list[SearchHit] = []
        for r in (results or [])[:max_results]:
            if not isinstance(r, dict):
                continue
            url = str(r.get("url") or "").strip()
            if not url:
                continue
            snippet = str(r.get("description") or "")
            extra = r.get("extra_snippets")
            if isinstance(extra, list) and extra:
                snippet = " ".join([snippet, *[str(s) for s in extra]]).strip()
            hits.append(SearchHit(
                url=url,
                title=str(r.get("title") or ""),
                snippet=snippet,
                published_date=(str(r["page_age"]) if r.get("page_age") else None),
            ))
        return SearchResult(results=hits, answer="", provider=self.name, query=query,
                            depth=self.normalize_depth(depth))


def create_provider(config: dict[str, Any] | None = None) -> BraveProvider:
    """Extension factory — builds the Brave adapter from user settings."""
    config = config or {}
    return BraveProvider(
        api_key=str(config.get("api_key", "")),
        timeout_secs=int(config.get("timeout_secs", 20) or 20),
    )
