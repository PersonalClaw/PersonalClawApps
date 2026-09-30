"""skills.sh: a request to its API honours the owner's Settings → Security → Network egress.

The marketplace is synchronous, so it asks the SDK's guard (``evaluate``) before its request. The
app's real request goes to a server on this machine standing in for the skills.sh API
(``apps_testkit.egress``): reached when the owner allowed its host, and refused before anything
is sent when they denied it or did not allow it.
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

_ANSWER = {"results": [{"id": "pdf-tools", "name": "PDF tools"}]}
_PATH = "/skills/search?q=pdf&limit=1"


@pytest.fixture
def skills_api(monkeypatch):
    with ProviderHost(json.dumps(_ANSWER)) as host:
        monkeypatch.setattr(provider, "_API_BASE", f"{host.url}/api/v1")
        yield host


def test_a_host_the_owner_allowed_is_asked(skills_api):
    owner_egress(allow_hosts=[HOST])
    assert provider.SkillsShMarketplace()._get(_PATH) == _ANSWER
    assert [r["path"] for r in skills_api.requests] == [f"/api/v1{_PATH}"]


@pytest.mark.parametrize(("allow", "deny", "reason"), REFUSALS, ids=REFUSAL_IDS)
def test_a_host_the_owners_settings_refuse_is_never_asked(skills_api, allow, deny, reason):
    owner_egress(allow_hosts=allow, deny_hosts=deny)
    with pytest.raises(RuntimeError) as refused:
        provider.SkillsShMarketplace()._get(_PATH)
    assert str(refused.value) == f"skills.sh request to {_PATH} blocked by egress guard: {reason}"
    assert skills_api.requests == []
