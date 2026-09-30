"""Exa: a search and a page fetch honour the owner's Settings → Security → Network egress.

The app's real requests go through the SDK's real guard to a server on this machine standing in
for the Exa API (``apps_testkit.egress``): reached when the owner allowed its host, and refused
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

_ANSWER = {"results": [{"url": "https://docs.example.com/a", "title": "A", "text": "The answer."}]}

#: Each call the app makes, and what it says when the guard refuses it.
_CALLS = {
    "search": (lambda p: p.search("rust async"), "Exa search blocked by egress guard"),
    "fetch": (lambda p: p.fetch("https://docs.example.com/a"), "Exa fetch blocked by egress guard"),
}


@pytest.fixture
def exa_api(monkeypatch):
    with ProviderHost(json.dumps(_ANSWER)) as host:
        monkeypatch.setattr(provider, "_API", host.url)
        yield host


@pytest.mark.asyncio
async def test_a_host_the_owner_allowed_is_searched_and_fetched(exa_api):
    owner_egress(allow_hosts=[HOST])
    found = await provider.ExaProvider("k").search("rust async")
    page = await provider.ExaProvider("k").fetch("https://docs.example.com/a")
    assert [(h.url, h.title) for h in found.results] == [("https://docs.example.com/a", "A")]
    assert (page.title, page.content) == ("A", "The answer.")
    assert [r["path"] for r in exa_api.requests] == ["/search", "/contents"]


@pytest.mark.asyncio
@pytest.mark.parametrize("call", sorted(_CALLS))
@pytest.mark.parametrize(("allow", "deny", "reason"), REFUSALS, ids=REFUSAL_IDS)
async def test_a_host_the_owners_settings_refuse_is_never_asked(
    exa_api, call, allow, deny, reason
):
    owner_egress(allow_hosts=allow, deny_hosts=deny)
    make, said = _CALLS[call]
    with pytest.raises(RuntimeError) as refused:
        await make(provider.ExaProvider("k"))
    assert str(refused.value) == f"{said}: {reason}"
    assert exa_api.requests == []
