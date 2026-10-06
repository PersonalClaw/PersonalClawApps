"""OpenAI tools: the connection probe honours the owner's Settings → Security → Network egress.

``connected`` is synchronous, so its probe is sent with the SDK's synchronous guarded client, which
asks the guard before the request is sent. The probe goes to a server on this machine standing in
for the tool server (``apps_testkit.egress``): sent when the owner allowed its host, and never sent
when they denied it or when the guard itself could not judge it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # the repo root: apps_testkit

import provider  # noqa: E402
from apps_testkit.egress import HOST, ProviderHost, owner_egress  # noqa: E402


@pytest.fixture
def tool_server():
    with ProviderHost("{}") as host:
        yield host


def test_a_tool_server_the_owner_allowed_is_probed(tool_server):
    owner_egress(allow_hosts=[HOST])
    assert provider.OpenAIToolProvider(tool_server.url).connected is True
    assert [r["method"] for r in tool_server.requests] == ["HEAD"]


def test_a_tool_server_the_owner_denied_is_never_probed(tool_server):
    owner_egress(allow_hosts=[HOST], deny_hosts=[HOST])
    assert provider.OpenAIToolProvider(tool_server.url).connected is False
    assert tool_server.requests == []


def test_a_check_that_cannot_run_is_not_connected_and_probes_nothing(tool_server, monkeypatch):
    """The guard looks the host up before it judges it; a lookup that cannot run judged nothing,
    so the probe is not sent."""
    owner_egress(allow_hosts=[HOST])

    def cannot_run(*args, **kwargs):
        raise OSError("the resolver is unavailable")

    monkeypatch.setattr("personalclaw.net.guard._resolve", cannot_run)
    assert provider.OpenAIToolProvider(tool_server.url).connected is False
    assert tool_server.requests == []


def test_a_redirect_to_a_host_the_owner_denied_is_never_followed():
    """🔴 Red before: the probe was sent with urllib, which follows a redirect by itself, and only
    its first host was asked: the tool server the owner allowed could send it on to a host she
    denied. Each hop is asked now, before it is sent, and the one to the denied host is refused."""
    with ProviderHost("{}") as elsewhere:
        port = elsewhere.url.rsplit(":", 1)[1]
        with ProviderHost("{}", moved_to=f"http://localhost:{port}") as tool_server:
            owner_egress(allow_hosts=[HOST], deny_hosts=["localhost"])
            assert provider.OpenAIToolProvider(tool_server.url).connected is False

    assert [r["path"] for r in tool_server.requests] == ["/"]
    assert elsewhere.requests == [], "the redirect to a denied host was followed"
