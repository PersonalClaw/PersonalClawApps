"""WS4 — the `web` tool provider's web_search: resolves the use-case → bound search
provider and returns the normalized shape; graceful recovery when none is bound.

Uses a fake SearchProvider registered in the search registry (no network).
"""

from __future__ import annotations

import json

import pytest

from personalclaw.search_providers import registry as reg
from personalclaw.search_providers import use_cases as uc
from personalclaw.search_providers.base import SearchCapabilities, SearchHit, SearchProvider, SearchResult
from provider import WebToolProvider


class _Fake(SearchProvider):
    def __init__(self, name="fake"):
        self._name = name
        self.calls: list[dict] = []

    @property
    def name(self): return self._name
    @property
    def display_name(self): return self._name.title()
    async def is_available(self): return True
    def capabilities(self): return SearchCapabilities(returns_answer=True, returns_content=True)

    async def search(self, query, *, depth="balanced", recency=None, domains=None, max_results=10):
        self.calls.append({"query": query, "depth": depth, "recency": recency,
                           "domains": domains, "max_results": max_results})
        return SearchResult(
            results=[SearchHit(url="https://a.com", title="A", snippet="s")],
            answer="the answer", provider=self._name, query=query, depth=depth,
        )


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(reg, "_providers", {})
    monkeypatch.setattr(uc, "_active_path", lambda: tmp_path / "active_search_providers.json")
    yield


@pytest.mark.asyncio
async def test_lists_web_search_tool():
    tools = await WebToolProvider().list_tools()
    by = {t.name: t for t in tools}
    assert "web_search" in by  # web_fetch is covered in test_web_fetch.py
    assert by["web_search"].requires_approval is False


@pytest.mark.asyncio
async def test_web_search_returns_normalized_payload():
    fake = _Fake("tavily")
    reg.register_provider(fake)
    res = await WebToolProvider().invoke("web_search", {"query": "hello", "depth": "deep"})
    assert res.success is True
    payload = json.loads(res.output)  # payload stays valid JSON (fields fenced in place)
    # Free-text fields (answer/title/snippet) are wrapped in <untrusted_content> so an
    # injection in a scraped result is data, not instructions — the ANSWER text is still
    # present, just fenced. Structural fields (sources) are trusted + untouched.
    assert "the answer" in payload["answer"]
    assert "<untrusted_content" in payload["answer"]
    assert payload["sources"] == ["https://a.com"]
    assert res.metadata["provider"] == "tavily"
    assert res.metadata["result_count"] == 1
    # args threaded through to the provider
    assert fake.calls[-1]["depth"] == "deep"


@pytest.mark.asyncio
async def test_web_search_uses_bound_provider_for_use_case():
    general, news = _Fake("general"), _Fake("news")
    reg.register_provider(general)
    reg.register_provider(news)
    uc.set_active_search_provider("search-news", "news")
    res = await WebToolProvider().invoke("web_search", {"query": "q", "use_case": "search-news"})
    assert res.metadata["provider"] == "news"


@pytest.mark.asyncio
async def test_web_search_no_provider_gives_recovery_hint():
    res = await WebToolProvider().invoke("web_search", {"query": "q"})
    assert res.success is False
    assert any("Settings" in h for h in res.recovery_hints)


@pytest.mark.asyncio
async def test_web_search_requires_query():
    reg.register_provider(_Fake())
    res = await WebToolProvider().invoke("web_search", {"query": "   "})
    assert res.success is False
    assert "query" in res.error


@pytest.mark.asyncio
async def test_web_search_rejects_unknown_use_case():
    reg.register_provider(_Fake())
    res = await WebToolProvider().invoke("web_search", {"query": "q", "use_case": "bogus"})
    assert res.success is False


@pytest.mark.asyncio
async def test_invoke_unknown_tool():
    res = await WebToolProvider().invoke("web_nonsense", {"foo": "bar"})
    assert res.success is False
    assert "Unknown tool" in res.error


@pytest.mark.asyncio
async def test_web_search_clamps_max_results():
    fake = _Fake()
    reg.register_provider(fake)
    await WebToolProvider().invoke("web_search", {"query": "q", "max_results": 999})
    assert fake.calls[-1]["max_results"] == 25  # clamped


@pytest.mark.asyncio
async def test_web_search_surfaces_provider_error():
    class _Boom(_Fake):
        async def search(self, *a, **k):
            raise RuntimeError("upstream 500")
    reg.register_provider(_Boom("boom"))
    res = await WebToolProvider().invoke("web_search", {"query": "q"})
    assert res.success is False
    assert "upstream 500" in res.error
    assert res.recovery_hints  # offers next steps
