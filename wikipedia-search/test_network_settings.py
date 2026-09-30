"""Wikipedia: a search honours the owner's Settings → Security → Network egress.

The app's real request goes through the SDK's real guard to a server on this machine standing in
for the Wikipedia API (``apps_testkit.egress``): reached when the owner allowed its host, and
refused before anything is sent when they denied it or did not allow it.
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

_ANSWER = {"query": {"pages": {"1": {
    "index": 1, "title": "Rust", "fullurl": "https://en.wikipedia.example/wiki/Rust",
    "extract": "The answer.",
}}}}


@pytest.fixture
def wikipedia_api(monkeypatch):
    with ProviderHost(json.dumps(_ANSWER)) as host:
        monkeypatch.setattr(provider, "_API", f"{host.url}/w/api.php")
        yield host


@pytest.mark.asyncio
async def test_a_host_the_owner_allowed_is_searched(wikipedia_api):
    owner_egress(allow_hosts=[HOST])
    result = await provider.WikipediaProvider().search("rust")
    assert [(h.url, h.title) for h in result.results] == [
        ("https://en.wikipedia.example/wiki/Rust", "Rust")
    ]
    assert [r["path"].split("?")[0] for r in wikipedia_api.requests] == ["/w/api.php"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("allow", "deny", "reason"), REFUSALS, ids=REFUSAL_IDS)
async def test_a_host_the_owners_settings_refuse_is_never_asked(
    wikipedia_api, allow, deny, reason
):
    owner_egress(allow_hosts=allow, deny_hosts=deny)
    with pytest.raises(RuntimeError) as refused:
        await provider.WikipediaProvider().search("rust")
    assert str(refused.value) == f"Wikipedia search blocked by egress guard: {reason}"
    assert wikipedia_api.requests == []
