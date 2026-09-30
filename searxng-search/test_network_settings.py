"""SearXNG: a search honours the owner's Settings → Security → Network egress, and says so.

An instance usually listens on the owner's own network, so the refusal the owner meets first is a
private address, and it names the one setting that lets it through. The app's real request goes
through the SDK's real guard to a server on this machine standing in for the instance
(``apps_testkit.egress``).
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

_ANSWER = {"results": [{"url": "https://docs.example.com/a", "title": "A", "content": "The answer."}]}


@pytest.fixture
def instance():
    with ProviderHost(json.dumps(_ANSWER)) as host:
        yield host


@pytest.mark.asyncio
async def test_an_instance_the_owner_allowed_is_searched(instance):
    owner_egress(allow_hosts=[HOST])
    result = await provider.SearxngProvider(instance.url).search("rust async")
    assert [(h.url, h.title) for h in result.results] == [("https://docs.example.com/a", "A")]
    assert [r["path"].split("?")[0] for r in instance.requests] == ["/search"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("allow", "deny", "refusal"), REFUSALS, ids=REFUSAL_IDS)
async def test_an_instance_the_owners_settings_refuse_is_never_asked(instance, allow, deny, refusal):
    owner_egress(allow_hosts=allow, deny_hosts=deny)
    with pytest.raises(RuntimeError) as refused:
        await provider.SearxngProvider(instance.url).search("rust async")
    assert str(refused.value) == refusal.said(f"{instance.url}/search")
    assert instance.requests == []
