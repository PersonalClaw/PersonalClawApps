"""skills.sh: a request to its API honours the owner's Settings → Security → Network egress.

The marketplace is synchronous, so it asks the SDK's guard (``evaluate``) before its request, and
sends the request with the SDK's synchronous guarded client, which asks the guard again about
every request it sends, each redirect hop included. The app's real request goes to a server on
this machine standing in for the skills.sh API (``apps_testkit.egress``): reached when the owner
allowed its host, and refused before anything is sent when they denied it, did not allow it, or
when the check itself could not run, and a redirect to a host they denied is never followed. A
refused search is not tried again through the ``skills`` CLI, which the guard never sees.
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


@pytest.fixture
def cli(monkeypatch):
    """The ``skills`` CLI search a failed API search falls back to, recorded instead of run."""
    ran: list[str] = []

    def search_via_cli(self, query, limit=20):
        ran.append(query)
        return []

    monkeypatch.setattr(provider.SkillsShMarketplace, "_search_via_cli", search_via_cli)
    monkeypatch.setenv("SKILLS_SH_API_KEY", "k")
    return ran


def _endpoint(host: ProviderHost) -> str:
    return f"{host.url}/api/v1/skills/search"


def test_a_host_the_owner_allowed_is_asked(skills_api):
    owner_egress(allow_hosts=[HOST])
    assert provider.SkillsShMarketplace()._get(_PATH) == _ANSWER
    assert [r["path"] for r in skills_api.requests] == [f"/api/v1{_PATH}"]


@pytest.mark.parametrize(("allow", "deny", "refusal"), REFUSALS, ids=REFUSAL_IDS)
def test_a_host_the_owners_settings_refuse_is_never_asked(skills_api, allow, deny, refusal):
    owner_egress(allow_hosts=allow, deny_hosts=deny)
    with pytest.raises(RuntimeError) as refused:
        provider.SkillsShMarketplace()._get(_PATH)
    assert str(refused.value) == refusal.said(_endpoint(skills_api))
    assert skills_api.requests == []


@pytest.mark.parametrize("step", ["evaluate", "egress_policy_for"])
def test_a_check_that_cannot_run_refuses_the_request(skills_api, monkeypatch, step):
    """A check that raises has judged nothing, so the request is refused as a denied host is,
    and never sent."""
    owner_egress(allow_hosts=[HOST])

    def cannot_run(*args, **kwargs):
        raise OSError("the resolver is unavailable")

    monkeypatch.setattr(f"personalclaw.sdk.net.{step}", cannot_run)
    with pytest.raises(RuntimeError) as refused:
        provider.SkillsShMarketplace()._get(_PATH)
    assert str(refused.value) == (
        f"{_endpoint(skills_api)} was not reached: PersonalClaw could not check it against "
        "Settings → Security → Network egress (OSError: the resolver is unavailable)."
    )
    assert skills_api.requests == []


def test_an_allowed_search_answers_from_the_api(skills_api, cli):
    owner_egress(allow_hosts=[HOST])
    found = provider.SkillsShMarketplace().search("pdf", limit=1)
    assert [(s.id, s.name) for s in found] == [("pdf-tools", "PDF tools")]
    assert cli == []


def test_a_refused_search_is_not_tried_again_through_the_cli(skills_api, cli):
    """The CLI reaches skills.sh from a child process the guard never sees, so a search the
    owner's settings refused ends there."""
    owner_egress(allow_hosts=[HOST], deny_hosts=[HOST])
    with pytest.raises(RuntimeError) as refused:
        provider.SkillsShMarketplace().search("pdf", limit=1)
    assert "is on Denied hosts in Settings → Security → Network egress" in str(refused.value)
    assert cli == []
    assert skills_api.requests == []


def test_a_redirect_to_a_host_the_owner_denied_is_never_followed(monkeypatch):
    """🔴 Red before: the request was sent with urllib, which follows a redirect by itself, and only
    the API's own host was asked: the API the owner allowed could send it on to a host she denied.
    Each hop is asked now, before it is sent, and the one to the denied host is refused in the
    words that name it."""
    with ProviderHost(json.dumps(_ANSWER)) as elsewhere:
        port = elsewhere.url.rsplit(":", 1)[1]
        with ProviderHost("{}", moved_to=f"http://localhost:{port}") as api:
            monkeypatch.setattr(provider, "_API_BASE", f"{api.url}/api/v1")
            owner_egress(allow_hosts=[HOST], deny_hosts=["localhost"])
            with pytest.raises(RuntimeError) as refused:
                provider.SkillsShMarketplace()._get(_PATH)

    assert str(refused.value) == (
        f"http://localhost:{port}/api/v1/skills/search was not reached: localhost is on Denied "
        "hosts in Settings → Security → Network egress."
    )
    assert [r["path"] for r in api.requests] == [f"/api/v1{_PATH}"]
    assert elsewhere.requests == [], "the redirect to a denied host was followed"
