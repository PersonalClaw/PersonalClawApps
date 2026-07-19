"""Tavily search adapter — the agent-optimized default.

Tavily is a search API built for LLM agents: one ``/search`` call returns a
synthesized ``answer``, scored results, and (optionally) the extracted page
``raw_content`` — so an agent often needs no follow-up fetch. It also exposes
``/extract`` for single-URL content extraction, so this provider ``supports_fetch``
and can back the ``fetch-article`` use-case directly.

Native dials mapped from the normalized ``depth``:
  quick/balanced → ``search_depth="basic"`` · deep → ``search_depth="advanced"``
``include_raw_content`` is requested only at ``deep`` (it costs latency/credits).
"""

import logging
import os
from typing import Any

from personalclaw.sdk.search import (
    DEFAULT_DEPTH,
    FetchResult,
    SearchCapabilities,
    SearchHit,
    SearchProvider,
    SearchResult,
)

logger = logging.getLogger(__name__)

_API = "https://api.tavily.com"

# Normalized depth → Tavily search_depth enum.
_DEPTH_TO_TAVILY = {"quick": "basic", "balanced": "basic", "deep": "advanced"}


class TavilyProvider(SearchProvider):
    def __init__(self, api_key: str = "", *, timeout_secs: int = 30) -> None:
        # An explicit key wins; else fall back to the conventional env var so a
        # user who exported TAVILY_API_KEY works out-of-box.
        self._api_key = api_key or os.environ.get("TAVILY_API_KEY", "")
        self._timeout = timeout_secs

    @property
    def name(self) -> str:
        return "tavily"

    @property
    def display_name(self) -> str:
        return "Tavily"

    async def is_available(self) -> bool:
        return bool(self._api_key)

    def capabilities(self) -> SearchCapabilities:
        return SearchCapabilities(
            returns_content=True,      # include_raw_content at deep depth
            returns_answer=True,       # include_answer
            returns_highlights=False,
            supports_recency=True,     # topic=news + days
            supports_domains=True,     # include_domains / exclude_domains
            supports_fetch=True,       # /extract endpoint
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
            raise RuntimeError("Tavily API key is not configured (Settings → Search)")

        d = self.normalize_depth(depth)
        body: dict[str, Any] = {
            "query": query,
            "search_depth": _DEPTH_TO_TAVILY.get(d, "basic"),
            "include_answer": True,
            "include_raw_content": d == "deep",
            "max_results": max_results,
        }
        if domains:
            body["include_domains"] = domains
        # Recency: Tavily biases recency via the `news` topic + a day window.
        days = _recency_days(recency)
        if days:
            body["topic"] = "news"
            body["days"] = days

        # Route through the net.fetch egress chokepoint (host/redirect guard, byte
        # cap, timeout, SEL audit) instead of raw httpx.
        try:
            resp = await fetch(
                f"{_API}/search", policy=CONNECTOR, method="POST",
                headers={"Authorization": f"Bearer {self._api_key}",
                         "Content-Type": "application/json"},
                data=json.dumps(body).encode(),
            )
        except EgressBlocked as e:
            raise RuntimeError(f"Tavily search blocked by egress guard: {e}") from e
        if resp.status != 200:
            raise RuntimeError(f"Tavily search failed (HTTP {resp.status})")
        data = json.loads(resp.text)

        hits: list[SearchHit] = []
        for r in (data.get("results") or [])[:max_results]:
            if not isinstance(r, dict):
                continue
            url = str(r.get("url") or "").strip()
            if not url:
                continue
            hits.append(SearchHit(
                url=url,
                title=str(r.get("title") or ""),
                snippet=str(r.get("content") or ""),
                score=_as_float(r.get("score")),
                published_date=(str(r["published_date"]) if r.get("published_date") else None),
                raw_content=str(r.get("raw_content") or ""),
            ))
        return SearchResult(
            results=hits, answer=str(data.get("answer") or ""),
            provider=self.name, query=query, depth=d,
        )

    async def fetch(self, url: str, *, max_tokens: int = 0, start_index: int = 0) -> FetchResult:
        import json

        from personalclaw.sdk.net import CONNECTOR, EgressBlocked
        from personalclaw.sdk.net import fetch as net_fetch

        if not self._api_key:
            raise RuntimeError("Tavily API key is not configured (Settings → Search)")

        try:
            resp = await net_fetch(
                f"{_API}/extract", policy=CONNECTOR, method="POST",
                headers={"Authorization": f"Bearer {self._api_key}",
                         "Content-Type": "application/json"},
                data=json.dumps({"urls": [url]}).encode(),
            )
        except EgressBlocked as e:
            raise RuntimeError(f"Tavily extract blocked by egress guard: {e}") from e
        if resp.status != 200:
            raise RuntimeError(f"Tavily extract failed (HTTP {resp.status})")
        data = json.loads(resp.text)

        results = data.get("results") or []
        first = results[0] if results and isinstance(results[0], dict) else {}
        content = str(first.get("raw_content") or "")
        # Coarse char-window pagination so callers can page a long extract (the
        # native §4 pipeline owns token-budgeted pagination; here char ranges).
        window = content[start_index:]
        truncated = False
        next_index: int | None = None
        if max_tokens:
            char_budget = max_tokens * 4  # ~4 chars/token heuristic
            if len(window) > char_budget:
                window = window[:char_budget]
                truncated = True
                next_index = start_index + char_budget
        return FetchResult(
            url=url, content=window, title=str(first.get("title") or ""),
            char_count=len(window), truncated=truncated, next_index=next_index,
        )


def _as_float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _recency_days(recency: str | None) -> int | None:
    """Map a normalized recency word to a Tavily `days` window."""
    r = (recency or "").strip().lower()
    return {"day": 1, "today": 1, "week": 7, "month": 30, "year": 365}.get(r)


def create_provider(config: dict[str, Any] | None = None) -> TavilyProvider:
    """Extension factory — builds the Tavily adapter from user settings."""
    config = config or {}
    return TavilyProvider(
        api_key=str(config.get("api_key", "")),
        timeout_secs=int(config.get("timeout_secs", 30) or 30),
    )
