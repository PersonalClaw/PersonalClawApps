"""WS7 — the Perplexity Sonar adapter: answer-first shape, depth→model mapping,
recency/domain filters, and the search_results→citations fallback.

Mocks the net.fetch egress chokepoint (the adapter routes outbound HTTP through it
instead of raw httpx). The fake records the fetched URL + decoded JSON body + headers
and returns a FetchResponse-shaped object (.status + .text).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from provider import PerplexityProvider


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


def _answer(content: str, **extra) -> dict[str, Any]:
    return {"choices": [{"message": {"role": "assistant", "content": content}}], **extra}


def test_capabilities_answer_first():
    caps = PerplexityProvider("k").capabilities()
    assert caps.returns_answer is True
    assert caps.returns_content is False
    assert caps.supports_fetch is False
    assert caps.supports_recency is True
    assert caps.supports_domains is True


@pytest.mark.asyncio
async def test_unavailable_without_key(monkeypatch):
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    assert await PerplexityProvider("").is_available() is False


def test_api_key_from_env(monkeypatch):
    monkeypatch.setenv("PERPLEXITY_API_KEY", "env-key")
    assert PerplexityProvider("")._api_key == "env-key"


@pytest.mark.asyncio
async def test_depth_maps_to_model(fake_fetch):
    fake_fetch.payload = _answer("a")
    p = PerplexityProvider("k")
    await p.search("q", depth="deep")
    assert fake_fetch.last["json"]["model"] == "sonar-pro"
    await p.search("q", depth="balanced")
    assert fake_fetch.last["json"]["model"] == "sonar"


@pytest.mark.asyncio
async def test_search_returns_answer_and_search_results(fake_fetch):
    fake_fetch.payload = _answer("The synthesized answer.", search_results=[
        {"url": "https://a.com", "title": "A", "snippet": "s", "date": "2026-01-01"},
        {"title": "no-url drop"},
    ])
    r = await PerplexityProvider("k").search("q")
    assert r.answer == "The synthesized answer."
    assert [h.url for h in r.results] == ["https://a.com"]
    assert r.results[0].published_date == "2026-01-01"
    assert r.sources == ["https://a.com"]
    assert fake_fetch.last["headers"]["Authorization"] == "Bearer k"
    assert fake_fetch.last["policy"] is not None  # CONNECTOR passed


@pytest.mark.asyncio
async def test_citations_fallback_when_no_search_results(fake_fetch):
    # Older responses carry only citations[] (bare URLs) — used as the result list.
    fake_fetch.payload = _answer("ans", citations=["https://x.com/1", "https://x.com/2"])
    r = await PerplexityProvider("k").search("q")
    assert [h.url for h in r.results] == ["https://x.com/1", "https://x.com/2"]
    assert r.answer == "ans"


@pytest.mark.asyncio
async def test_recency_and_domain_filters(fake_fetch):
    fake_fetch.payload = _answer("a")
    await PerplexityProvider("k").search("q", recency="week", domains=["arxiv.org"])
    body = fake_fetch.last["json"]
    assert body["search_recency_filter"] == "week"
    assert body["search_domain_filter"] == ["arxiv.org"]


@pytest.mark.asyncio
async def test_no_recency_omits_filter(fake_fetch):
    fake_fetch.payload = _answer("a")
    await PerplexityProvider("k").search("q")
    assert "search_recency_filter" not in fake_fetch.last["json"]


@pytest.mark.asyncio
async def test_handles_empty_choices(fake_fetch):
    fake_fetch.payload = {"search_results": [{"url": "https://a.com"}]}  # no choices
    r = await PerplexityProvider("k").search("q")
    assert r.answer == ""
    assert [h.url for h in r.results] == ["https://a.com"]


@pytest.mark.asyncio
async def test_search_raises_on_http_error(fake_fetch):
    fake_fetch.status = 500
    with pytest.raises(RuntimeError):
        await PerplexityProvider("k").search("q")


@pytest.mark.asyncio
async def test_search_raises_without_key(fake_fetch, monkeypatch):
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        await PerplexityProvider("").search("q")
