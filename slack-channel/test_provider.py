"""slack-channel app: the SlackTransport declares its real channel capabilities.

Loads the transport from the app's own ``slack_runtime`` package (app dir on
sys.path) — the full Slack integration moved out of core in the core/app split.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# App dir on sys.path so this root-level test imports the app's slack_runtime
# package the way the gateway's app loader does (also handled by tests/conftest.py
# for the tests/ subdir; inlined here since this file lives at the app root).
_APP_DIR = Path(__file__).resolve().parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

from slack_runtime.transport import SlackTransport, create_provider  # noqa: E402


def test_slack_capabilities():
    c = SlackTransport().capabilities()
    assert c.inbound and c.threads and c.attachments and c.reactions and c.edits
    assert c.max_text_len == 40000


def test_connected_derives_from_shared_creds(monkeypatch):
    """A live Slack integration (tokens in the SHARED credential store the gateway
    propagates into the environment) must report connected/ready even when THIS
    app's instance config carries no tokens — otherwise the Channels surface lies
    'offline' for a working channel."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-shared")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-shared")
    t = SlackTransport({})  # empty instance config — tokens only in the environment
    assert t.connected is True
    assert asyncio.run(t.health())["state"] == "ready"


def test_offline_when_no_tokens_anywhere(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    t = SlackTransport({})
    assert t.connected is False
    assert asyncio.run(t.health())["state"] == "offline"


def test_instance_config_overrides_shared(monkeypatch):
    """A user-supplied per-instance token wins over the shared store."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-shared")
    t = SlackTransport({"bot_token": "xoxb-instance"})
    assert t._bot_token == "xoxb-instance"


def test_slack_info_exposes_caps():
    info = SlackTransport().info()
    assert info["capabilities"]["inbound"] is True


def test_create_provider_returns_transport():
    assert type(create_provider({})).__name__ == "SlackTransport"
