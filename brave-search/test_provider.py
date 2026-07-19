"""WS7 — the Brave Search adapter: capability disclosure, recency→freshness mapping,
extra_snippets folding, and response normalization.

Mocks the net.fetch egress chokepoint (the adapter routes outbound HTTP through it
instead of raw httpx) so no network/key needed. The fake records the fetched URL +
headers and returns a FetchResponse-shaped object (.status + .text).
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from provider import BraveProvider


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


def test_capabilities_links_only_with_recency():
    caps = BraveProvider("k").capabilities()
    assert caps.returns_answer is False
    assert caps.returns_content is False
    assert caps.supports_recency is True
    assert caps.supports_fetch is False
    assert caps.depths == ("balanced",)


@pytest.mark.asyncio
async def test_unavailable_without_key(monkeypatch):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    assert await BraveProvider("").is_available() is False


def test_api_key_from_env(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "env-key")
    assert BraveProvider("")._api_key == "env-key"


@pytest.mark.asyncio
async def test_search_normalizes_results_and_folds_extra_snippets(fake_fetch):
    fake_fetch.payload = {"web": {"results": [
        {"url": "https://a.com", "title": "A", "description": "desc",
         "extra_snippets": ["more1", "more2"], "page_age": "2026-01-01"},
        {"title": "no-url drop"},
    ]}}
    r = await BraveProvider("k").search("hello", recency="week", max_results=5)
    assert r.provider == "brave"
    assert [h.url for h in r.results] == ["https://a.com"]
    # extra_snippets folded into the snippet for a richer link card
    assert "desc" in r.results[0].snippet and "more1" in r.results[0].snippet and "more2" in r.results[0].snippet
    assert r.results[0].published_date == "2026-01-01"
    # recency mapped to freshness; auth header set; routed through the chokepoint
    assert fake_fetch.last["params"]["freshness"] == "pw"
    assert fake_fetch.last["headers"]["X-Subscription-Token"] == "k"
    assert fake_fetch.last["policy"] is not None  # CONNECTOR policy passed


@pytest.mark.asyncio
async def test_search_count_clamped(fake_fetch):
    fake_fetch.payload = {"web": {"results": []}}
    await BraveProvider("k").search("q", max_results=999)
    assert fake_fetch.last["params"]["count"] == "20"  # clamped to Brave's max


@pytest.mark.asyncio
async def test_search_handles_missing_web_block(fake_fetch):
    fake_fetch.payload = {}  # no 'web' key
    r = await BraveProvider("k").search("q")
    assert r.results == []


@pytest.mark.asyncio
async def test_search_no_recency_omits_freshness(fake_fetch):
    fake_fetch.payload = {"web": {"results": []}}
    await BraveProvider("k").search("q")
    assert "freshness" not in fake_fetch.last["params"]


@pytest.mark.asyncio
async def test_search_raises_on_http_error(fake_fetch):
    fake_fetch.status = 429  # rate-limited
    with pytest.raises(RuntimeError):
        await BraveProvider("k").search("q")


@pytest.mark.asyncio
async def test_search_raises_without_key(fake_fetch, monkeypatch):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        await BraveProvider("").search("q")
