"""FAL: a generation honours the owner's Settings → Security → Network egress.

The app's real requests go through the SDK's real guard to a server on this machine standing in
for FAL's queue (``apps_testkit.egress``): reached when the owner allowed its host, and refused
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
from personalclaw.sdk.net import EgressBlocked  # noqa: E402

#: FAL answers a quick job inline, with no request id to poll.
_ANSWER = {"images": [{"url": "https://files.example.com/a.png"}]}


@pytest.fixture
def fal_queue(monkeypatch):
    with ProviderHost(json.dumps(_ANSWER)) as host:
        monkeypatch.setattr(provider, "_QUEUE_BASE", host.url)
        yield host


@pytest.mark.asyncio
async def test_a_host_the_owner_allowed_is_asked(fal_queue):
    owner_egress(allow_hosts=[HOST])
    answer = await provider._submit_and_poll("fal-ai/flux/schnell", {"prompt": "a bicycle"},
                                             api_key="k")
    assert answer == _ANSWER
    assert [r["path"] for r in fal_queue.requests] == ["/fal-ai/flux/schnell"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("allow", "deny", "refusal"), REFUSALS, ids=REFUSAL_IDS)
async def test_a_host_the_owners_settings_refuse_is_never_asked(fal_queue, allow, deny, refusal):
    owner_egress(allow_hosts=allow, deny_hosts=deny)
    with pytest.raises(EgressBlocked) as refused:
        await provider._submit_and_poll("fal-ai/flux/schnell", {"prompt": "a bicycle"},
                                        api_key="k")
    assert str(refused.value) == refusal.reason
    assert fal_queue.requests == []
