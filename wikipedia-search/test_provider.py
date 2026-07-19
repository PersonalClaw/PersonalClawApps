"""The Wikipedia search adapter: keyless availability, capability disclosure, and
MediaWiki generator=search response normalization (relevance order + extracts).

Mocks the net.fetch egress chokepoint (the adapter routes outbound HTTP through it
instead of raw httpx) so no network needed. The fake parses the query string out of
the fetched URL (fetch has no params kwarg) and returns a FetchResponse-shaped object
(.status + .text).
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from provider import (
    WikipediaProvider,
    create_provider,
)


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
        # parse_qs → single-value dict for easy assertions (values are STRINGS
        # after the urlencode round-trip, e.g. gsrlimit "5" not 5).
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


def test_capabilities_links_only():
    caps = WikipediaProvider().capabilities()
    assert caps.returns_answer is False
    assert caps.returns_content is False
    assert caps.supports_recency is False
    assert caps.supports_fetch is False
    assert caps.depths == ("balanced",)


@pytest.mark.asyncio
async def test_keyless_always_available():
    assert await WikipediaProvider().is_available() is True


@pytest.mark.asyncio
async def test_search_normalizes_and_preserves_relevance_order(fake_fetch):
    # MediaWiki returns pages keyed by id, each tagged with a search `index`.
    fake_fetch.payload = {"query": {"pages": {
        "42": {"index": 2, "title": "Second", "fullurl": "https://en.wikipedia.org/wiki/Second",
               "extract": "second extract"},
        "7": {"index": 1, "title": "First", "fullurl": "https://en.wikipedia.org/wiki/First",
              "extract": "first extract"},
    }}}
    r = await WikipediaProvider().search("hello", max_results=5)
    assert r.provider == "wikipedia"
    # Sorted by index → relevance order, not dict order.
    assert [h.title for h in r.results] == ["First", "Second"]
    assert r.results[0].snippet == "first extract"
    assert r.results[0].url == "https://en.wikipedia.org/wiki/First"
    # Query shape: generator=search against the en endpoint (params are strings after
    # the urlencode round-trip). Routed through the chokepoint with the CONNECTOR policy.
    assert fake_fetch.last["params"]["generator"] == "search"
    assert fake_fetch.last["params"]["gsrsearch"] == "hello"
    assert fake_fetch.last["params"]["gsrlimit"] == "5"
    assert "en.wikipedia.org" in fake_fetch.last["url"]
    assert fake_fetch.last["policy"] is not None  # CONNECTOR policy passed


@pytest.mark.asyncio
async def test_search_synthesizes_url_when_fullurl_missing(fake_fetch):
    fake_fetch.payload = {"query": {"pages": {
        "1": {"index": 1, "title": "Ada Lovelace", "extract": "x"},
    }}}
    r = await WikipediaProvider().search("ada")
    assert r.results[0].url == "https://en.wikipedia.org/wiki/Ada_Lovelace"


@pytest.mark.asyncio
async def test_empty_query_returns_no_results(fake_fetch):
    r = await WikipediaProvider().search("   ")
    assert r.results == []


@pytest.mark.asyncio
async def test_missing_query_block(fake_fetch):
    fake_fetch.payload = {}  # no 'query' key (zero hits)
    r = await WikipediaProvider().search("nothing matches")
    assert r.results == []


@pytest.mark.asyncio
async def test_language_is_configurable(fake_fetch):
    fake_fetch.payload = {"query": {"pages": {}}}
    await create_provider({"lang": "de"}).search("q")
    assert "de.wikipedia.org" in fake_fetch.last["url"]


@pytest.mark.asyncio
async def test_user_agent_header_sent(fake_fetch):
    fake_fetch.payload = {"query": {"pages": {}}}
    await WikipediaProvider().search("q")
    assert "PersonalClaw" in fake_fetch.last["headers"]["User-Agent"]


@pytest.mark.asyncio
async def test_search_raises_on_http_error(fake_fetch):
    fake_fetch.status = 500  # upstream error
    with pytest.raises(RuntimeError):
        await WikipediaProvider().search("q")
