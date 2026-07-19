"""Exa search adapter — neural/semantic discovery with highlights.

Exa is an embeddings-based search API: a ``/search`` call returns semantically-ranked
results and can attach ``contents`` (extracted text + relevance ``highlights``), so it
both surfaces non-obvious sources and ``supports_fetch`` (via ``/contents``) for the
``fetch-article`` use-case.

Native dials mapped from the normalized inputs:
  depth → ``type`` (quick → ``fast`` · balanced → ``auto`` · deep → ``neural``)
  recency → ``startPublishedDate`` (a lower-bound ISO date computed from the window)
Highlights are folded into each result's snippet so the relevant passages travel with
the link even before a full fetch.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
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

_API = "https://api.exa.ai"

# Normalized depth → Exa search `type`.
_DEPTH_TO_TYPE = {"quick": "fast", "balanced": "auto", "deep": "neural"}
# Normalized recency word → lookback days (for startPublishedDate).
_RECENCY_DAYS = {"day": 1, "today": 1, "week": 7, "month": 30, "year": 365}


class ExaProvider(SearchProvider):
    def __init__(self, api_key: str = "", *, timeout_secs: int = 30) -> None:
        # Explicit key wins; else the conventional env var (parity with the other
        # keyed adapters), so an exported EXA_API_KEY works out-of-box.
        self._api_key = api_key or os.environ.get("EXA_API_KEY", "")
        self._timeout = timeout_secs

    @property
    def name(self) -> str:
        return "exa"

    @property
    def display_name(self) -> str:
        return "Exa"

    async def is_available(self) -> bool:
        return bool(self._api_key)

    def capabilities(self) -> SearchCapabilities:
        return SearchCapabilities(
            returns_content=True,      # contents.text
            returns_answer=False,      # Exa surfaces sources, not a synthesized answer
            returns_highlights=True,   # contents.highlights (relevant passages)
            supports_recency=True,     # startPublishedDate
            supports_domains=True,     # includeDomains
            supports_fetch=True,       # /contents endpoint
            depths=("quick", "balanced", "deep"),
        )

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self._api_key, "Content-Type": "application/json"}

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
            raise RuntimeError("Exa API key is not configured (Settings → Search)")

        d = self.normalize_depth(depth)
        body: dict[str, Any] = {
            "query": query,
            "type": _DEPTH_TO_TYPE.get(d, "auto"),
            "numResults": max(1, min(max_results, 25)),
            # Ask for text + highlights only at deeper modes (highlights cost compute);
            # a quick pass returns ranked links + highlights for the snippet.
            "contents": {"text": d == "deep", "highlights": True},
        }
        if domains:
            body["includeDomains"] = domains
        start_date = _recency_start_date(recency)
        if start_date:
            body["startPublishedDate"] = start_date

        # Route through the net.fetch egress chokepoint (host/redirect guard, byte
        # cap, timeout, SEL audit) instead of raw httpx.
        try:
            resp = await fetch(
                f"{_API}/search", policy=CONNECTOR, method="POST",
                headers=self._headers(), data=json.dumps(body).encode(),
            )
        except EgressBlocked as e:
            raise RuntimeError(f"Exa search blocked by egress guard: {e}") from e
        if resp.status != 200:
            raise RuntimeError(f"Exa search failed (HTTP {resp.status})")
        data = json.loads(resp.text)

        hits: list[SearchHit] = []
        for r in (data.get("results") or [])[:max_results]:
            if not isinstance(r, dict):
                continue
            url = str(r.get("url") or "").strip()
            if not url:
                continue
            # Fold highlights into the snippet so relevant passages travel with the link.
            highlights = r.get("highlights")
            snippet = " … ".join(str(h) for h in highlights) if isinstance(highlights, list) and highlights else ""
            hits.append(SearchHit(
                url=url,
                title=str(r.get("title") or ""),
                snippet=snippet,
                score=_as_float(r.get("score")),
                published_date=(str(r["publishedDate"]) if r.get("publishedDate") else None),
                raw_content=str(r.get("text") or ""),
            ))
        return SearchResult(results=hits, answer="", provider=self.name, query=query, depth=d)

    async def fetch(self, url: str, *, max_tokens: int = 0, start_index: int = 0) -> FetchResult:
        import json

        from personalclaw.sdk.net import CONNECTOR, EgressBlocked
        from personalclaw.sdk.net import fetch as net_fetch

        if not self._api_key:
            raise RuntimeError("Exa API key is not configured (Settings → Search)")

        try:
            resp = await net_fetch(
                f"{_API}/contents", policy=CONNECTOR, method="POST",
                headers=self._headers(),
                data=json.dumps({"urls": [url], "text": True}).encode(),
            )
        except EgressBlocked as e:
            raise RuntimeError(f"Exa fetch blocked by egress guard: {e}") from e
        if resp.status != 200:
            raise RuntimeError(f"Exa fetch failed (HTTP {resp.status})")
        data = json.loads(resp.text)

        results = data.get("results") or []
        first = results[0] if results and isinstance(results[0], dict) else {}
        content = str(first.get("text") or "")
        # Coarse char-window pagination (the native §4 pipeline owns token budgeting).
        window = content[start_index:]
        truncated = False
        next_index: int | None = None
        if max_tokens:
            char_budget = max_tokens * 4
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


def _recency_start_date(recency: str | None) -> str | None:
    """Map a normalized recency word to an ISO startPublishedDate lower bound."""
    days = _RECENCY_DAYS.get((recency or "").strip().lower())
    if not days:
        return None
    start = datetime.now(tz=timezone.utc) - timedelta(days=days)
    return start.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def create_provider(config: dict[str, Any] | None = None) -> ExaProvider:
    """Extension factory — builds the Exa adapter from user settings."""
    config = config or {}
    return ExaProvider(
        api_key=str(config.get("api_key", "")),
        timeout_secs=int(config.get("timeout_secs", 30) or 30),
    )
