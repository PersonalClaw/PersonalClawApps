"""SearXNG search adapter — the self-hosted, no-key default.

SearXNG is a self-hostable meta-search engine: it aggregates results from many
upstream engines and returns links + snippets over a JSON API (``/search?format=json``).
No API key — the user points the provider at their own instance URL (on-brand for a
self-hosted, private, $0 default). Links-only: it returns no synthesized answer and
no extracted page body, so callers must ``web_fetch`` a result to read it.
"""

import logging
from typing import Any

from personalclaw.sdk.search import (
    DEFAULT_DEPTH,
    SearchCapabilities,
    SearchHit,
    SearchProvider,
    SearchResult,
)

logger = logging.getLogger(__name__)

# SearXNG exposes a `time_range` filter (day/week/month/year). Map the normalized
# recency words callers pass onto that native enum; anything else → no filter.
_RECENCY_TO_TIME_RANGE = {
    "day": "day", "today": "day",
    "week": "week",
    "month": "month",
    "year": "year",
}


class SearxngProvider(SearchProvider):
    def __init__(self, endpoint: str = "", *, timeout_secs: int = 20) -> None:
        self._endpoint = endpoint.rstrip("/") if endpoint else ""
        self._timeout = timeout_secs

    @property
    def name(self) -> str:
        return "searxng"

    @property
    def display_name(self) -> str:
        return f"SearXNG ({self._endpoint})" if self._endpoint else "SearXNG"

    async def is_available(self) -> bool:
        return bool(self._endpoint)

    def capabilities(self) -> SearchCapabilities:
        # Meta-search: links + snippets, server-side recency (time_range) and
        # engine selection. No synthesized answer, no extracted content, no
        # depth dial → advertise just the balanced depth.
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

        from personalclaw.sdk.net import CONNECTOR, EgressBlocked, egress_policy_for, fetch

        if not self._endpoint:
            raise RuntimeError("SearXNG endpoint is not configured (Settings → Search)")

        params: dict[str, Any] = {"q": query, "format": "json"}
        tr = _RECENCY_TO_TIME_RANGE.get((recency or "").strip().lower())
        if tr:
            params["time_range"] = tr

        # SECURITY: route through the net.fetch egress chokepoint instead of raw
        # httpx. This provider hits an OPERATOR-CONFIGURED endpoint (self._endpoint),
        # so it's the prime SSRF / private-IP target — the chokepoint classifies the
        # host (blocking loopback/link-local/private ranges) before connecting.
        # fetch takes no params kwarg, so the query string is built into the URL.
        # We deliberately DROP follow_redirects here: the chokepoint re-checks EVERY
        # redirect hop against the same host guard, so a redirect can't be used to
        # bounce the request off a public URL into a private/internal address — the
        # exact protection raw httpx (follow_redirects=True) was missing.
        url = f"{self._endpoint}/search?{urlencode(params)}"
        # Layer the operator's ``security.egress`` config onto CONNECTOR so a
        # self-hoster who opts into LAN egress (allow_private, or allow_hosts for
        # their SearXNG host) can actually reach it. SearXNG is the ONE provider
        # whose endpoint is almost always a private/LAN address, so without this
        # the guard blocks every real instance and search silently falls back to
        # the keyless default. The guard still blocks LAN by default (config unset).
        policy = egress_policy_for(CONNECTOR)
        try:
            resp = await fetch(url, policy=policy, method="GET",
                               headers={"Accept": "application/json"})
        except EgressBlocked as e:
            raise RuntimeError(f"SearXNG search blocked by egress guard: {e}") from e
        if resp.status != 200:
            raise RuntimeError(f"SearXNG search failed (HTTP {resp.status})")
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
                published_date=(str(r["publishedDate"]) if r.get("publishedDate") else None),
            ))
        return SearchResult(results=hits, answer="", provider=self.name, query=query,
                            depth=self.normalize_depth(depth))


def _as_float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def create_provider(config: dict[str, Any] | None = None) -> SearxngProvider:
    """Extension factory — builds the SearXNG adapter from user settings."""
    config = config or {}
    return SearxngProvider(
        endpoint=str(config.get("endpoint", "")),
        timeout_secs=int(config.get("timeout_secs", 20) or 20),
    )
