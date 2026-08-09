"""telegram-channel app: the TelegramTransport declares its real channel capabilities.

Loads the transport from the app's own ``telegram_runtime`` package (app dir on
sys.path) — the whole Telegram integration lives in the bundle, importing core only
through ``personalclaw.sdk.*``."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# App dir on sys.path so this root-level test imports the app's telegram_runtime
# package the way the gateway's app loader does (tests/conftest.py does the same for
# the tests/ subdir; inlined here since this file lives at the app root).
_APP_DIR = Path(__file__).resolve().parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

from telegram_runtime.settings import (  # noqa: E402
    CRED_TELEGRAM_BOT_TOKEN,
    TelegramSettings,
    _validate_activation,
)
from telegram_runtime.transport import TelegramTransport, create_provider  # noqa: E402


def test_telegram_capabilities():
    c = TelegramTransport().capabilities()
    assert c.inbound and c.threads and c.attachments and c.edits and c.rich_text
    assert c.reactions is False
    assert c.max_text_len == 4096


def test_connected_derives_from_shared_creds(monkeypatch):
    """A live integration (token in the SHARED credential store the gateway
    propagates into the environment) must report ready even when THIS instance's
    config carries no token — otherwise the Channels surface lies 'offline'."""
    monkeypatch.setenv(CRED_TELEGRAM_BOT_TOKEN, "123:shared")
    t = TelegramTransport({})  # empty instance config — token only in the environment
    assert t.connected is True
    assert asyncio.run(t.health())["state"] == "ready"


def test_offline_when_no_token_anywhere(monkeypatch):
    monkeypatch.delenv(CRED_TELEGRAM_BOT_TOKEN, raising=False)
    t = TelegramTransport({})
    assert t.connected is False
    assert asyncio.run(t.health())["state"] == "offline"


def test_instance_config_overrides_shared(monkeypatch):
    monkeypatch.setenv(CRED_TELEGRAM_BOT_TOKEN, "123:shared")
    t = TelegramTransport({"bot_token": "456:instance"})
    assert t._token == "456:instance"


def test_info_exposes_caps():
    info = TelegramTransport().info()
    assert info["capabilities"]["inbound"] is True
    assert info["display_name"] == "Telegram"


def test_create_provider_returns_transport():
    assert type(create_provider({})).__name__ == "TelegramTransport"


def test_settings_default_and_validation():
    # Unknown activation coerces to the safe default.
    assert _validate_activation("bogus") == "always"
    assert _validate_activation("off") == "off"
    assert TelegramSettings().dm_activation == "always"
