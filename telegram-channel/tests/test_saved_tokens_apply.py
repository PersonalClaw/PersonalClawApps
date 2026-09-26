"""A bot token saved on the Apps page reaches Test, Connect, health and send without a restart.

The registry builds the transport ONCE, from the app store as it is at enable time, and the Apps
page's Configure → Save (``PUT /api/apps/{name}/config``) writes the store and re-cycles nothing.
The transport read its token in ``__init__``, so after a successful Save every surface kept
answering "No bot token configured".

Saves go through core's own route handler; ``HTTPTelegramAPI`` is replaced so nothing opens a
socket.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import telegram_runtime.transport as transport_mod
from personalclaw.dashboard.handlers.apps import api_app_config_put
from personalclaw.providers.settings import ProviderSettings
from personalclaw.sdk.channel import OutboundMessage
from telegram_runtime.settings import CRED_TELEGRAM_BOT_TOKEN, get_settings
from telegram_runtime.transport import create_provider

_APP = "telegram-channel"
_BUNDLE = Path(__file__).resolve().parents[1]


@pytest.fixture
def installed(_isolate_home, monkeypatch):
    """This app installed in the (conftest-isolated) home, with nothing configured."""
    monkeypatch.delenv(CRED_TELEGRAM_BOT_TOKEN, raising=False)
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
    """``HTTPTelegramAPI`` stand-in: records the token it was built with, what it sent, closes."""

    built_with: list[str] = []
    sent: list[tuple[str, str]] = []
    closed = 0

    def __init__(self, token: str, **_kw) -> None:
        self.token = token
        _FakeAPI.built_with.append(token)

    async def get_me(self):
        return {"username": "claw_bot"}

    async def send_message(self, chat_id, text, **_kw):
        _FakeAPI.sent.append((self.token, chat_id))
        return {"message_id": 1}

    async def close(self):
        _FakeAPI.closed += 1


@pytest.fixture
def fake_api(monkeypatch):
    _FakeAPI.built_with, _FakeAPI.sent, _FakeAPI.closed = [], [], 0
    monkeypatch.setattr(transport_mod, "HTTPTelegramAPI", _FakeAPI)
    return _FakeAPI


def _registry_built():
    """What ``ChannelTypeHandler.create`` does when the app is enabled."""
    return create_provider(ProviderSettings.load(_APP))


@pytest.mark.asyncio
async def test_a_token_saved_after_enable_reaches_connect_and_health(installed):
    transport = _registry_built()
    assert (await transport.health())["detail"] == "No bot token configured"

    await _configure_save({"bot_token": "123:saved"})

    assert transport.connected is True
    assert await transport.connect() is True
    health = await transport.health()
    assert health["state"] != "offline" and "No bot token" not in health["detail"], health
    # Never driven by the gateway (built at enable, after boot): honest about inbound.
    assert "NOT STARTED" in health["detail"], health


@pytest.mark.asyncio
async def test_test_authenticates_with_the_saved_token(installed, fake_api):
    transport = _registry_built()
    await _configure_save({"bot_token": "123:saved"})

    probe = await transport.test()

    assert fake_api.built_with == ["123:saved"]
    assert probe["detail"].startswith("Authenticated as @claw_bot"), probe
    # Authenticated, yet not ok: this transport was built at enable and never driven, so no
    # receiver is listening (the channel contract: test() is not ok while health() is not ready).
    assert probe["ok"] is False and "NOT STARTED" in probe["detail"], probe


@pytest.mark.asyncio
async def test_send_goes_out_on_the_saved_token_and_closes_its_client(installed, fake_api):
    transport = _registry_built()
    await _configure_save({"bot_token": "123:saved"})

    assert await transport.send(OutboundMessage(channel_id="42", text="hi")) is True
    assert fake_api.sent == [("123:saved", "42")]
    assert fake_api.closed == 1  # a client made for one send is not leaked


@pytest.mark.asyncio
async def test_a_rotated_token_sends_on_the_new_one_not_the_receivers(installed, fake_api):
    await _configure_save({"bot_token": "123:old"})
    transport = _registry_built()
    transport._inbound_token = "123:old"
    transport._api = _FakeAPI("123:old")

    await _configure_save({"bot_token": "123:new"})
    await transport.send(OutboundMessage(channel_id="42", text="hi"))

    assert fake_api.sent[-1] == ("123:new", "42")


@pytest.mark.asyncio
async def test_a_token_saved_after_boot_says_inbound_starts_on_the_next_restart(installed, fake_api):
    """Booted without a token, the long-poll receiver never started. Saving one makes outbound
    work at once; saying "ready" would claim a receiver that does not exist."""
    transport = _registry_built()
    await transport.start_inbound(object())  # no token: returns before touching services

    await _configure_save({"bot_token": "123:saved"})

    health = await transport.health()
    assert health["state"] == "error", health
    assert "next restart" in health["detail"], health
    probe = await transport.test()
    assert probe["ok"] is False
    assert probe["detail"].startswith("Authenticated as @claw_bot, but "), probe


@pytest.mark.asyncio
async def test_a_setting_saved_on_configure_is_read_without_a_restart(installed):
    assert get_settings().dm_activation == "always"

    await _configure_save({"dm_activation": "off"})

    assert get_settings().dm_activation == "off"
