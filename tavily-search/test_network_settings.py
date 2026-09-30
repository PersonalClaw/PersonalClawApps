"""Tavily: a search and a page extract honour the owner's Settings → Security → Network egress.

The app's real requests go through the SDK's real guard to a server on this machine standing in
for the Tavily API (``apps_testkit.egress``): reached when the owner allowed its host, and refused
before anything is sent when they denied it or did not allow it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # the repo root: apps_testkit

import provider  # noqa: E402
from apps_testkit.egress import (  # noqa: E402
    HOST,
    REFUSAL_IDS,
    REFUSALS,
    ProviderHost,
    owner_egress,
)

_ANSWER = {
    "answer": "The answer.",
    "results": [{"url": "https://docs.example.com/a", "title": "A", "raw_content": "The page."}],
}

#: Each call the app makes, and what it says when the guard refuses it.
_CALLS = {
    "search": (lambda p: p.search("rust async"), "Tavily search blocked by egress guard"),
    "extract": (
        lambda p: p.fetch("https://docs.example.com/a"), "Tavily extract blocked by egress guard",
    ),
}


@pytest.fixture
def tavily_api(monkeypatch):
    with ProviderHost(json.dumps(_ANSWER)) as host:
        monkeypatch.setattr(provider, "_API", host.url)
        yield host


@pytest.mark.asyncio
async def test_a_host_the_owner_allowed_is_searched_and_extracted(tavily_api):
    owner_egress(allow_hosts=[HOST])
    found = await provider.TavilyProvider("k").search("rust async")
    page = await provider.TavilyProvider("k").fetch("https://docs.example.com/a")
    assert found.answer == "The answer."
    assert [(h.url, h.title) for h in found.results] == [("https://docs.example.com/a", "A")]
    assert (page.title, page.content) == ("A", "The page.")
    assert [r["path"] for r in tavily_api.requests] == ["/search", "/extract"]


@pytest.mark.asyncio
@pytest.mark.parametrize("call", sorted(_CALLS))
@pytest.mark.parametrize(("allow", "deny", "reason"), REFUSALS, ids=REFUSAL_IDS)
async def test_a_host_the_owners_settings_refuse_is_never_asked(
    tavily_api, call, allow, deny, reason
):
    owner_egress(allow_hosts=allow, deny_hosts=deny)
    make, said = _CALLS[call]
    with pytest.raises(RuntimeError) as refused:
        await make(provider.TavilyProvider("k"))
    assert str(refused.value) == f"{said}: {reason}"
    assert tavily_api.requests == []
