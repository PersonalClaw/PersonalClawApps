"""The Bedrock streaming client must tolerate quiet-but-healthy reasoning streams.

botocore's default 60s read timeout fires on the GAP BETWEEN streamed events
during ``converse_stream`` — so an Opus loop turn that reasons (or waits on a
slow tool) for >60s mid-stream dies with a bare ``Read timed out`` even though
the turn is live. ``BedrockProvider.start`` therefore builds the client with a
long read timeout and botocore retries OFF (PersonalClaw owns retry/stall
recovery at the loop/watchdog layer). These pin that contract so a future edit
can't silently restore the 60s default that broke long loop turns.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import provider as B


def _run(coro):
    return asyncio.run(coro)


def _install_fake_boto3(monkeypatch):
    """Stub boto3 so start() builds a client without touching AWS; capture Config."""
    captured: dict = {}

    fake_client = MagicMock(name="bedrock-runtime")

    def _client(name, region_name=None, config=None):
        captured["name"] = name
        captured["region"] = region_name
        captured["config"] = config
        return fake_client

    fake_session = MagicMock()
    fake_session.client.side_effect = _client

    fake_boto3 = SimpleNamespace(Session=lambda *a, **k: fake_session)
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    return captured


def test_stream_client_uses_long_read_timeout_and_no_retries(monkeypatch):
    captured = _install_fake_boto3(monkeypatch)

    p = B.BedrockProvider(model="Bedrock:global.anthropic.claude-opus-4-8",
                          region="us-west-2")
    _run(p.start())

    cfg = captured["config"]
    assert cfg is not None, "client must be built with an explicit botocore Config"
    # Generous read headroom — must be far above botocore's 60s default that
    # killed long loop turns mid-stream.
    assert cfg.read_timeout == B._STREAM_READ_TIMEOUT >= 300
    assert cfg.connect_timeout == B._CONNECT_TIMEOUT
    # Retries OFF: a botocore retry of a streaming call would replay a partially
    # consumed turn; the loop/watchdog owns retry instead.
    assert cfg.retries.get("max_attempts") == 0
    assert captured["name"] == "bedrock-runtime"
    assert captured["region"] == "us-west-2"
