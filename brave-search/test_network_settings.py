"""Brave Search: a search honours the owner's Settings → Security → Network egress.

The app's real request goes through the SDK's real guard to a server on this machine standing in
for the Brave API (``apps_testkit.egress``): reached when the owner allowed its host, and refused
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

_ANSWER = {"web": {"results": [
    {"url": "https://docs.example.com/a", "title": "A", "description": "The answer."},
]}}


@pytest.fixture
def brave_api(monkeypatch):
    with ProviderHost(json.dumps(_ANSWER)) as host:
        monkeypatch.setattr(provider, "_API", f"{host.url}/res/v1/web/search")
        yield host


@pytest.mark.asyncio
async def test_a_host_the_owner_allowed_is_searched(brave_api):
    owner_egress(allow_hosts=[HOST])
    result = await provider.BraveProvider("k").search("rust async")
    assert [(h.url, h.title) for h in result.results] == [("https://docs.example.com/a", "A")]
    assert [r["path"].split("?")[0] for r in brave_api.requests] == ["/res/v1/web/search"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("allow", "deny", "refusal"), REFUSALS, ids=REFUSAL_IDS)
async def test_a_host_the_owners_settings_refuse_is_never_asked(brave_api, allow, deny, refusal):
    owner_egress(allow_hosts=allow, deny_hosts=deny)
    with pytest.raises(RuntimeError) as refused:
        await provider.BraveProvider("k").search("rust async")
    assert str(refused.value) == refusal.said(provider._API)
    assert brave_api.requests == []
