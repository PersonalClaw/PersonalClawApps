"""OpenAI tools: the connection probe honours the owner's Settings → Security → Network egress.

``connected`` is synchronous, so it asks the SDK's guard (``evaluate``) before its own probe. The
probe goes to a server on this machine standing in for the tool server (``apps_testkit.egress``):
sent when the owner allowed its host, and never sent when they denied it or when the check itself
could not run, which has judged nothing.
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


@pytest.mark.parametrize("step", ["evaluate", "egress_policy_for"])
def test_a_check_that_cannot_run_is_not_connected_and_probes_nothing(tool_server, monkeypatch, step):
    owner_egress(allow_hosts=[HOST])

    def cannot_run(*args, **kwargs):
        raise OSError("the resolver is unavailable")

    monkeypatch.setattr(f"personalclaw.sdk.net.{step}", cannot_run)
    assert provider.OpenAIToolProvider(tool_server.url).connected is False
    assert tool_server.requests == []
