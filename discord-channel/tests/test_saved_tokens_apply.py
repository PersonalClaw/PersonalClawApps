"""A bot token saved on the Apps page reaches Test, Connect, health and send without a restart.

The same defect Slack and Telegram carried: the registry builds the transport once, at enable, and
the Apps page's Configure → Save (``PUT /api/apps/{name}/config``) writes the store and re-cycles
nothing, while the transport read its token in ``__init__``.

Saves go through core's own route handler; ``HTTPDiscordAPI`` is replaced so nothing opens a
socket.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import discord_runtime.transport as transport_mod
from discord_runtime.settings import CRED_DISCORD_BOT_TOKEN, get_settings
from discord_runtime.transport import create_provider
from personalclaw.dashboard.handlers.apps import api_app_config_put
from personalclaw.providers.settings import ProviderSettings
from personalclaw.sdk.channel import OutboundMessage

_APP = "discord-channel"
_BUNDLE = Path(__file__).resolve().parents[1]


@pytest.fixture
def installed(_isolate_home, monkeypatch):
    """This app installed in the (conftest-isolated) home, with nothing configured."""
    monkeypatch.delenv(CRED_DISCORD_BOT_TOKEN, raising=False)
    home_app = _isolate_home / "apps" / _APP
    home_app.mkdir(parents=True, exist_ok=True)
    shutil.copy(_BUNDLE / "app.json", home_app / "app.json")
    return _isolate_home


async def _configure_save(values: dict) -> None:
    """The Apps page's Configure → Save: core's PUT /api/apps/{name}/config handler."""

    class _Request(dict):
        match_info = {"name": _APP}

        async def json(self):
            return values

    resp = await api_app_config_put(_Request())
    assert resp.status == 200, resp.text


class _FakeAPI:
    """``HTTPDiscordAPI`` stand-in: records the token it was built with, what it sent, closes."""

    built_with: list[str] = []
    sent: list[tuple[str, str]] = []
    closed = 0

    def __init__(self, token: str, **_kw) -> None:
        self.token = token
        _FakeAPI.built_with.append(token)

    async def get_gateway_bot(self):
        return {"url": "wss://gateway.example", "session_start_limit": {"remaining": 999}}

    async def create_message(self, channel_id, text, **_kw):
        _FakeAPI.sent.append((self.token, channel_id))
        return {"id": "1"}

    async def close(self):
        _FakeAPI.closed += 1


@pytest.fixture
def fake_api(monkeypatch):
    _FakeAPI.built_with, _FakeAPI.sent, _FakeAPI.closed = [], [], 0
    monkeypatch.setattr(transport_mod, "HTTPDiscordAPI", _FakeAPI)
    return _FakeAPI


def _registry_built():
    """What ``ChannelTypeHandler.create`` does when the app is enabled."""
    return create_provider(ProviderSettings.load(_APP))


@pytest.mark.asyncio
async def test_a_token_saved_after_enable_reaches_connect_and_health(installed):
    transport = _registry_built()
    assert (await transport.health())["detail"] == "No bot token configured"

    await _configure_save({"bot_token": "saved.token.value"})

    assert transport.connected is True
    assert await transport.connect() is True
    health = await transport.health()
    assert health["state"] != "offline" and "No bot token" not in health["detail"], health
    # Never driven by the gateway (built at enable, after boot): honest about inbound.
    assert "NOT STARTED" in health["detail"], health


@pytest.mark.asyncio
async def test_test_probes_the_gateway_with_the_saved_token(installed, fake_api):
    transport = _registry_built()
    await _configure_save({"bot_token": "saved.token.value"})

    probe = await transport.test()

    assert fake_api.built_with == ["saved.token.value"]
    assert probe["detail"].startswith("Gateway reachable at wss://gateway.example"), probe
    assert probe["ok"] is False and "NOT STARTED" in probe["detail"], probe  # never driven


@pytest.mark.asyncio
async def test_send_goes_out_on_the_saved_token(installed, fake_api):
    transport = _registry_built()
    await _configure_save({"bot_token": "saved.token.value"})

    assert await transport.send(OutboundMessage(channel_id="C42", text="hi")) is True
    assert fake_api.sent == [("saved.token.value", "C42")]
    assert fake_api.closed == 1


@pytest.mark.asyncio
async def test_a_rotated_token_sends_on_the_new_one_not_the_receivers(installed, fake_api):
    await _configure_save({"bot_token": "old.token.value"})
    transport = _registry_built()
    transport._inbound_token = "old.token.value"
    transport._api = _FakeAPI("old.token.value")

    await _configure_save({"bot_token": "new.token.value"})
    await transport.send(OutboundMessage(channel_id="C42", text="hi"))

    assert fake_api.sent[-1] == ("new.token.value", "C42")


@pytest.mark.asyncio
async def test_a_token_saved_after_boot_says_inbound_starts_on_the_next_restart(installed, fake_api):
    transport = _registry_built()
    await transport.start_inbound(object())  # no token: returns before touching services

    await _configure_save({"bot_token": "saved.token.value"})

    health = await transport.health()
    assert health["state"] == "error", health
    assert "next restart" in health["detail"], health
    probe = await transport.test()
    assert probe["ok"] is False and "next restart" in probe["detail"], probe


@pytest.mark.asyncio
async def test_a_setting_saved_on_configure_is_read_without_a_restart(installed):
    assert get_settings().dm_activation == "always"

    await _configure_save({"dm_activation": "off"})

    assert get_settings().dm_activation == "off"
