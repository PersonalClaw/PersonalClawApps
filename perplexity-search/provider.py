"""Perplexity Sonar search adapter — answer-first web search.

Perplexity's Sonar models are an OpenAI-compatible chat endpoint that answers a query
*from live web results* and returns the cited sources alongside. So this adapter is
the answer-first profile: it populates ``SearchResult.answer`` (the synthesized
response) plus the ``search_results`` it drew on as the result list — no separate
fetch, the answer + citations are the product.

Native dials mapped from the normalized inputs:
  depth → model (quick/balanced → ``sonar`` · deep → ``sonar-pro``)
  recency → ``search_recency_filter`` (day/week/month/year)

Outbound HTTP is routed through the ``net.fetch`` egress chokepoint (host/redirect
guard, byte cap, timeout, SEL audit) rather than raw httpx.
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

_API = "https://api.perplexity.ai/chat/completions"

# Normalized depth → Sonar model (sonar-pro does deeper, multi-step search).
_DEPTH_TO_MODEL = {"quick": "sonar", "balanced": "sonar", "deep": "sonar-pro"}
# Normalized recency word → Perplexity search_recency_filter value.
_RECENCY_FILTER = {"day": "day", "today": "day", "week": "week", "month": "month", "year": "year"}


class PerplexityProvider(SearchProvider):
    def __init__(self, api_key: str = "", *, timeout_secs: int = 40) -> None:
        # Explicit key wins; else the conventional env var (parity with the other
        # keyed adapters), so an exported PERPLEXITY_API_KEY works out-of-box.
        self._api_key = api_key or os.environ.get("PERPLEXITY_API_KEY", "")
        self._timeout = timeout_secs

    @property
    def name(self) -> str:
        return "perplexity"

    @property
    def display_name(self) -> str:
        return "Perplexity Sonar"

    async def is_available(self) -> bool:
        return bool(self._api_key)

    def capabilities(self) -> SearchCapabilities:
        # Answer-first: a synthesized answer + the cited sources. The results carry
        # title/url/snippet but not extracted page bodies, and there's no single-URL
        # fetch endpoint, so callers web_fetch a citation to read it in full.
        return SearchCapabilities(
            returns_content=False,
            returns_answer=True,
            returns_highlights=False,
            supports_recency=True,
            supports_domains=True,       # search_domain_filter
            supports_fetch=False,
            depths=("quick", "balanced", "deep"),
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

        from personalclaw.sdk.net import CONNECTOR, EgressBlocked, fetch

        if not self._api_key:
            raise RuntimeError("Perplexity API key is not configured (Settings → Search)")

        d = self.normalize_depth(depth)
        body: dict[str, Any] = {
            "model": _DEPTH_TO_MODEL.get(d, "sonar"),
            "messages": [{"role": "user", "content": query}],
        }
        rec = _RECENCY_FILTER.get((recency or "").strip().lower())
        if rec:
            body["search_recency_filter"] = rec
        if domains:
            body["search_domain_filter"] = domains

        H = {"Authorization": f"Bearer {self._api_key}",
             "Content-Type": "application/json"}
        # Route through the net.fetch egress chokepoint (host/redirect guard, byte
        # cap, timeout, SEL audit) instead of raw httpx.
        try:
            resp = await fetch(_API, policy=CONNECTOR, method="POST", headers=H, data=json.dumps(body).encode())
        except EgressBlocked as e:
            raise RuntimeError(f"Perplexity search blocked by egress guard: {e}") from e
        if resp.status != 200:
            raise RuntimeError(f"Perplexity search failed (HTTP {resp.status})")
        data = json.loads(resp.text)

        # The answer is the assistant message; the cited sources come back as
        # search_results[] (newer field) with citations[] as a URL-only fallback.
        answer = ""
        choices = data.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            answer = str((choices[0].get("message") or {}).get("content") or "")

        hits: list[SearchHit] = []
        results = data.get("search_results")
        if isinstance(results, list) and results:
            for r in results[:max_results]:
                if not isinstance(r, dict):
                    continue
                url = str(r.get("url") or "").strip()
                if not url:
                    continue
                hits.append(SearchHit(
                    url=url,
                    title=str(r.get("title") or ""),
                    snippet=str(r.get("snippet") or ""),
                    published_date=(str(r["date"]) if r.get("date") else None),
                ))
        else:
            # Fallback: citations[] is a bare URL list on older responses.
            for url in (data.get("citations") or [])[:max_results]:
                u = str(url or "").strip()
                if u:
                    hits.append(SearchHit(url=u))

        return SearchResult(results=hits, answer=answer, provider=self.name, query=query, depth=d)


def create_provider(config: dict[str, Any] | None = None) -> PerplexityProvider:
    """Extension factory — builds the Perplexity adapter from user settings."""
    config = config or {}
    return PerplexityProvider(
        api_key=str(config.get("api_key", "")),
        timeout_secs=int(config.get("timeout_secs", 40) or 40),
    )
