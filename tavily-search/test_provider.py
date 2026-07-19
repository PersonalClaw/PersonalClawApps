"""Tavily search adapter (separated app): capability disclosure, depth-dial
mapping, request shaping, and response normalization.

Mocks the net.fetch egress chokepoint (the adapter routes outbound HTTP through
it instead of raw httpx). The fake records the fetched URL + decoded JSON body +
headers and returns a FetchResponse-shaped object (.status + .text). Imports the
provider from the app's own ``provider`` module (loaded with the app dir on
sys.path), not core.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from provider import TavilyProvider


# ── Fake net.fetch ─────────────────────────────────────────────────────────────

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


# ── Tavily ───────────────────────────────────────────────────────────────────

def test_tavily_capabilities_full():
    caps = TavilyProvider("k").capabilities()
    assert caps.returns_answer is True
    assert caps.returns_content is True
    assert caps.supports_fetch is True
    assert caps.supports_domains is True
    assert set(caps.depths) == {"quick", "balanced", "deep"}


@pytest.mark.asyncio
async def test_tavily_api_key_from_env(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "env-key")
    # No explicit key → falls back to the env var, so it reports available.
    assert await TavilyProvider("").is_available() is True


@pytest.mark.asyncio
async def test_tavily_depth_maps_to_search_depth(fake_fetch):
    fake_fetch.payload = {"answer": "the answer", "results": []}
    p = TavilyProvider("k")
    await p.search("q", depth="deep")
    body = fake_fetch.last["json"]
    assert body["search_depth"] == "advanced"
    assert body["include_raw_content"] is True  # only at deep
    # balanced → basic, no raw_content
    await p.search("q", depth="balanced")
    body = fake_fetch.last["json"]
    assert body["search_depth"] == "basic"
    assert body["include_raw_content"] is False


@pytest.mark.asyncio
async def test_tavily_search_normalizes_answer_and_results(fake_fetch):
    fake_fetch.payload = {"answer": "A.", "results": [
        {"url": "https://x.com", "title": "X", "content": "c", "score": 0.5, "raw_content": "body"},
    ]}
    r = await TavilyProvider("k").search("q")
    assert r.answer == "A."
    assert r.results[0].raw_content == "body"
    assert r.results[0].url == "https://x.com"
    assert fake_fetch.last["headers"]["Authorization"] == "Bearer k"
    assert fake_fetch.last["policy"] is not None  # CONNECTOR passed


@pytest.mark.asyncio
async def test_tavily_recency_sets_news_topic(fake_fetch):
    fake_fetch.payload = {"results": []}
    await TavilyProvider("k").search("q", recency="week")
    body = fake_fetch.last["json"]
    assert body["topic"] == "news"
    assert body["days"] == 7


@pytest.mark.asyncio
async def test_tavily_domains_passed_through(fake_fetch):
    fake_fetch.payload = {"results": []}
    await TavilyProvider("k").search("q", domains=["arxiv.org"])
    assert fake_fetch.last["json"]["include_domains"] == ["arxiv.org"]


@pytest.mark.asyncio
async def test_tavily_fetch_extracts_and_paginates(fake_fetch):
    fake_fetch.payload = {"results": [{"raw_content": "x" * 100, "title": "T"}]}
    p = TavilyProvider("k")
    fr = await p.fetch("https://x.com", max_tokens=10)  # ~40 char budget
    assert fr.title == "T"
    assert fr.truncated is True
    assert fr.char_count == 40
    assert fr.next_index == 40
    assert fake_fetch.last["url"].endswith("/extract")


@pytest.mark.asyncio
async def test_tavily_search_raises_on_http_error(fake_fetch):
    fake_fetch.status = 500
    with pytest.raises(RuntimeError):
        await TavilyProvider("k").search("q")


@pytest.mark.asyncio
async def test_tavily_search_raises_without_key(fake_fetch, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        await TavilyProvider("").search("q")
