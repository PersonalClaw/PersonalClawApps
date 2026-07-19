"""WS7 — the Exa Search adapter: capability disclosure (neural + highlights + fetch),
depth→type mapping, recency→startPublishedDate, highlight folding, and /contents fetch.

Mocks the net.fetch egress chokepoint (the adapter routes outbound HTTP through it
instead of raw httpx). The fake records the fetched URL + decoded JSON body + headers
and returns a FetchResponse-shaped object (.status + .text).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from provider import ExaProvider


class _FakeFetchResponse:
    def __init__(self, status: int, payload: dict[str, Any]) -> None:
        self.status = status
        self.text = json.dumps(payload)


class _FakeFetch:
    last: dict[str, Any] = {}
    payload: dict[str, Any] = {}
    status: int = 200

    async def __call__(self, url: str, *, policy=None, method: str = "GET",
                       headers: dict | None = None, data: bytes | None = None) -> _FakeFetchResponse:
        body = json.loads(data.decode()) if data else {}
        type(self).last = {"url": url, "json": body, "headers": headers or {},
                           "method": method, "policy": policy}
        return _FakeFetchResponse(type(self).status, type(self).payload)


@pytest.fixture
def fake_fetch(monkeypatch: pytest.MonkeyPatch):
    fake = _FakeFetch()
    _FakeFetch.last = {}
    _FakeFetch.payload = {}
    _FakeFetch.status = 200
    monkeypatch.setattr("personalclaw.net.client.fetch", fake, raising=False)
    monkeypatch.setattr("personalclaw.sdk.net.fetch", fake, raising=False)
    monkeypatch.setattr("personalclaw.net.fetch", fake, raising=False)
    return _FakeFetch


def test_capabilities_neural_highlights_fetch():
    caps = ExaProvider("k").capabilities()
    assert caps.returns_highlights is True
    assert caps.returns_content is True
    assert caps.supports_fetch is True
    assert caps.supports_domains is True
    assert caps.returns_answer is False
    assert set(caps.depths) == {"quick", "balanced", "deep"}


@pytest.mark.asyncio
async def test_unavailable_without_key(monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    assert await ExaProvider("").is_available() is False


def test_api_key_from_env(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "env-key")
    assert ExaProvider("")._api_key == "env-key"


@pytest.mark.asyncio
async def test_depth_maps_to_type(fake_fetch):
    fake_fetch.payload = {"results": []}
    p = ExaProvider("k")
    await p.search("q", depth="deep")
    assert fake_fetch.last["json"]["type"] == "neural"
    assert fake_fetch.last["json"]["contents"]["text"] is True  # text only at deep
    await p.search("q", depth="quick")
    assert fake_fetch.last["json"]["type"] == "fast"
    assert fake_fetch.last["json"]["contents"]["text"] is False


@pytest.mark.asyncio
async def test_search_folds_highlights_into_snippet(fake_fetch):
    fake_fetch.payload = {"results": [
        {"url": "https://a.com", "title": "A", "score": 0.8,
         "highlights": ["passage one", "passage two"], "publishedDate": "2026-01-01", "text": "body"},
        {"title": "no-url drop"},
    ]}
    r = await ExaProvider("k").search("q")
    assert r.provider == "exa"
    assert [h.url for h in r.results] == ["https://a.com"]
    assert "passage one" in r.results[0].snippet and "passage two" in r.results[0].snippet
    assert r.results[0].raw_content == "body"
    assert fake_fetch.last["headers"]["x-api-key"] == "k"
    assert fake_fetch.last["policy"] is not None  # CONNECTOR passed


@pytest.mark.asyncio
async def test_recency_sets_start_published_date(fake_fetch):
    fake_fetch.payload = {"results": []}
    await ExaProvider("k").search("q", recency="month")
    assert "startPublishedDate" in fake_fetch.last["json"]


@pytest.mark.asyncio
async def test_no_recency_omits_start_date(fake_fetch):
    fake_fetch.payload = {"results": []}
    await ExaProvider("k").search("q")
    assert "startPublishedDate" not in fake_fetch.last["json"]


@pytest.mark.asyncio
async def test_domains_passed_through(fake_fetch):
    fake_fetch.payload = {"results": []}
    await ExaProvider("k").search("q", domains=["arxiv.org"])
    assert fake_fetch.last["json"]["includeDomains"] == ["arxiv.org"]


@pytest.mark.asyncio
async def test_fetch_extracts_and_paginates(fake_fetch):
    fake_fetch.payload = {"results": [{"text": "x" * 100, "title": "T"}]}
    fr = await ExaProvider("k").fetch("https://x.com", max_tokens=10)  # ~40 char budget
    assert fr.title == "T"
    assert fr.truncated is True
    assert fr.char_count == 40
    assert fr.next_index == 40
    assert fake_fetch.last["url"].endswith("/contents")


@pytest.mark.asyncio
async def test_search_raises_on_http_error(fake_fetch):
    fake_fetch.status = 500
    with pytest.raises(RuntimeError):
        await ExaProvider("k").search("q")


@pytest.mark.asyncio
async def test_search_raises_without_key(fake_fetch, monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        await ExaProvider("").search("q")
