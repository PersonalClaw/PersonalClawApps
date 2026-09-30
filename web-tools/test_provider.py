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
    monkeypatch.setattr(reg, "_provider_app", {})
    monkeypatch.setattr(reg, "_checks", {})
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
    # This said to "Enable SearXNG or Tavily in Settings → Providers", but search providers
    # are Store apps, one registers only while its app is on, and one that is on is used
    # whether or not it is chosen.
    res = await WebToolProvider().invoke("web_search", {"query": "q"})
    assert res.success is False
    assert res.error == (
        "No search provider app is installed and turned on, so there is nothing to search with."
    )
    assert res.recovery_hints == [
        "Install a search provider app from the Store on the Apps page, or turn on one you "
        "already have there; if it asks for an API key or an endpoint, add it in Settings → "
        "Providers. Once one is on, web_search uses it; to choose which one each kind of search "
        "uses, go to Settings → Search."
    ]


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


@pytest.mark.asyncio
async def test_a_search_that_fell_back_says_so_to_the_agent():
    """The bound provider refused the search and the keyless engine answered. The result says
    that, in PersonalClaw's words, and carries the refused provider's own words as data."""
    class _Refused(_Fake):
        async def search(self, *a, **k):
            raise RuntimeError("Brave refused the API key (HTTP 401).")

    class _Keyless(_Fake):
        def capabilities(self):
            return SearchCapabilities(keyless=True)

    reg.register_provider(_Refused("brave"))
    reg.register_provider(_Keyless("duckduckgo"))
    uc.set_active_search_provider("search-general", "brave")

    res = await WebToolProvider().invoke("web_search", {"query": "q"})

    assert res.success is True, res.error
    payload = json.loads(res.output)
    assert payload["provider"] == "duckduckgo"
    notice = payload["fallback"]["notice"]
    assert "Brave" in notice and "Duckduckgo" in notice, notice
    assert "<untrusted_content" not in notice, "the notice is PersonalClaw's own sentence"
    # The refused provider's words may carry a remote server's text: data, never instructions.
    assert "<untrusted_content" in payload["fallback"]["reason"]
    assert "refused the API key" in payload["fallback"]["reason"]
    assert res.metadata["fallback_from"] == "brave"


@pytest.mark.asyncio
async def test_a_search_that_did_not_fall_back_carries_no_notice():
    reg.register_provider(_Fake("tavily"))
    res = await WebToolProvider().invoke("web_search", {"query": "q"})
    payload = json.loads(res.output)
    assert "fallback" not in payload
    assert res.metadata["fallback_from"] == ""
