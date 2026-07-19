"""WS2 — SearXNG search adapter (a standalone app): capability disclosure,
request shaping, and response normalization.

Mocks the net.fetch egress chokepoint (the adapter routes outbound HTTP through it
instead of raw httpx — SECURITY-CRITICAL here because SearXNG hits an operator-
configured endpoint, the prime SSRF target) so no network/credential is needed. The
fake records the fetched URL + headers and returns a FetchResponse-shaped object
(.status + .text). (Tavily + the other separable search providers moved to standalone
apps under ``apps/<name>/`` in the core/app split — their tests live with them as
``apps/<name>/test_provider.py``; SearXNG is a self-hosted-endpoint app.)
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from provider import SearxngProvider


# ── Fake net.fetch ─────────────────────────────────────────────────────────────

class _FakeFetchResponse:
    def __init__(self, status: int, payload: dict[str, Any]) -> None:
        self.status = status
        self.text = json.dumps(payload)


class _FakeFetch:
    """Records the last fetch() call + returns a canned FetchResponse."""
    last: dict[str, Any] = {}
    payload: dict[str, Any] = {}
    status: int = 200

    async def __call__(self, url: str, *, policy=None, method: str = "GET",
                       headers: dict | None = None, data: bytes | None = None) -> _FakeFetchResponse:
        parsed = urlparse(url)
        # parse_qs → single-value dict for easy assertions
        qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        type(self).last = {"url": url, "params": qs, "headers": headers or {},
                           "method": method, "policy": policy}
        return _FakeFetchResponse(type(self).status, type(self).payload)


@pytest.fixture
def fake_fetch(monkeypatch: pytest.MonkeyPatch):
    fake = _FakeFetch()
    _FakeFetch.last = {}
    _FakeFetch.payload = {}
    _FakeFetch.status = 200
    # The adapter does `from personalclaw.sdk.net import CONNECTOR, EgressBlocked, fetch`
    # inside search(); patch the source module's fetch so the late import picks it up.
    monkeypatch.setattr("personalclaw.net.client.fetch", fake, raising=False)
    monkeypatch.setattr("personalclaw.sdk.net.fetch", fake, raising=False)
    monkeypatch.setattr("personalclaw.net.fetch", fake, raising=False)
    return _FakeFetch


# ── SearXNG ──────────────────────────────────────────────────────────────────

def test_searxng_capabilities_links_only():
    caps = SearxngProvider("https://s.example.com").capabilities()
    assert caps.returns_content is False
    assert caps.returns_answer is False
    assert caps.supports_recency is True
    assert caps.supports_fetch is False
    assert caps.depths == ("balanced",)


@pytest.mark.asyncio
async def test_searxng_unavailable_without_endpoint():
    assert await SearxngProvider("").is_available() is False


@pytest.mark.asyncio
async def test_searxng_search_normalizes_results(fake_fetch):
    fake_fetch.payload = {"results": [
        {"url": "https://a.com", "title": "A", "content": "snip", "score": 0.9, "publishedDate": "2026-01-01"},
        {"title": "no-url drop"},  # dropped — no url
    ]}
    p = SearxngProvider("https://s.example.com")
    r = await p.search("hello", recency="week", max_results=5)
    assert r.provider == "searxng"
    assert [h.url for h in r.results] == ["https://a.com"]
    assert r.results[0].snippet == "snip"
    assert r.sources == ["https://a.com"]
    # recency mapped to time_range; format json requested (params come back as
    # strings via urlencode → parse_qs). Endpoint is folded into the URL.
    assert fake_fetch.last["params"]["time_range"] == "week"
    assert fake_fetch.last["params"]["format"] == "json"
    assert fake_fetch.last["url"].startswith("https://s.example.com/search?")
    assert fake_fetch.last["policy"] is not None  # CONNECTOR policy passed to the chokepoint


@pytest.mark.asyncio
async def test_searxng_search_no_recency_omits_time_range(fake_fetch):
    fake_fetch.payload = {"results": []}
    await SearxngProvider("https://s.example.com").search("q")
    assert "time_range" not in fake_fetch.last["params"]


@pytest.mark.asyncio
async def test_searxng_search_raises_on_http_error(fake_fetch):
    fake_fetch.status = 502  # bad gateway from the SearXNG instance
    with pytest.raises(RuntimeError):
        await SearxngProvider("https://s.example.com").search("q")


@pytest.mark.asyncio
async def test_searxng_search_raises_without_endpoint(fake_fetch):
    with pytest.raises(RuntimeError):
        await SearxngProvider("").search("q")
