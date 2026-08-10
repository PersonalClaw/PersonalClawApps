"""discord-channel app: the DiscordTransport declares its real channel capabilities.

Loads the transport from the app's own ``discord_runtime`` package (app dir on
sys.path) — the whole Discord integration lives in the bundle, importing core only
through ``personalclaw.sdk.*``."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# App dir on sys.path so this root-level test imports the app's discord_runtime
# package the way the gateway's app loader does (tests/conftest.py does the same for
# the tests/ subdir; inlined here since this file lives at the app root).
_APP_DIR = Path(__file__).resolve().parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

from cli_setup import INVITE_PERMISSIONS, invite_url  # noqa: E402
from discord_runtime.gateway import INTENTS  # noqa: E402
from discord_runtime.settings import (  # noqa: E402
    CRED_DISCORD_BOT_TOKEN,
    DiscordSettings,
    _validate_activation,
)
from discord_runtime.transport import DiscordTransport, create_provider  # noqa: E402


def test_discord_capabilities():
    c = DiscordTransport().capabilities()
    assert c.inbound and c.threads and c.attachments and c.edits and c.rich_text
    # Both declared True because both are implemented (add_reaction / show_typing) —
    # the atom's "honest capabilities" bar.
    assert c.reactions is True
    assert c.typing_indicator is True
    assert c.max_text_len == 2000


def test_connected_derives_from_shared_creds(monkeypatch):
    """A live integration (token in the SHARED credential store the gateway
    propagates into the environment) must report ready even when THIS instance's
    config carries no token — otherwise the Channels surface lies 'offline'."""
    monkeypatch.setenv(CRED_DISCORD_BOT_TOKEN, "shared.token.value")
    t = DiscordTransport({})  # empty instance config — token only in the environment
    assert t.connected is True
    assert asyncio.run(t.health())["state"] == "ready"


def test_offline_when_no_token_anywhere(monkeypatch):
    monkeypatch.delenv(CRED_DISCORD_BOT_TOKEN, raising=False)
    t = DiscordTransport({})
    assert t.connected is False
    assert asyncio.run(t.health())["state"] == "offline"


def test_instance_config_overrides_shared(monkeypatch):
    monkeypatch.setenv(CRED_DISCORD_BOT_TOKEN, "shared.token.value")
    t = DiscordTransport({"bot_token": "instance.token.value"})
    assert t._token == "instance.token.value"


def test_info_exposes_caps():
    info = DiscordTransport().info()
    assert info["capabilities"]["inbound"] is True
    assert info["display_name"] == "Discord"


def test_create_provider_returns_transport():
    assert type(create_provider({})).__name__ == "DiscordTransport"


def test_settings_default_and_validation():
    # Unknown activation coerces to the safe default.
    assert _validate_activation("bogus") == "always"
    assert _validate_activation("off") == "off"
    assert DiscordSettings().dm_activation == "always"
    assert DiscordSettings().application_id == ""


def test_intents_bitfield_is_the_documented_sum():
    """Pinned here too (not just in tests/): a wrong bitfield fails silently."""
    assert INTENTS == 37377 == (1 << 0) | (1 << 9) | (1 << 12) | (1 << 15)


def test_invite_url_carries_the_computed_permissions():
    url = invite_url("123456789")
    assert url.startswith("https://discord.com/oauth2/authorize")
    assert "client_id=123456789" in url
    assert "scope=bot" in url
    assert f"permissions={INVITE_PERMISSIONS}" in url


def test_invite_url_empty_without_an_application_id():
    """Better no URL than a Discord error page from a blank client_id."""
    assert invite_url("") == ""
