"""A bot token saved on the Apps page reaches Test, Connect, health and send without a restart.

The same defect Slack and Telegram carried: the registry built the transport once, at enable, and
the Apps page's Configure → Save (``PUT /api/apps/{name}/config``) wrote the store and re-cycled
nothing, while the transport read its token in ``__init__``. A save now also rebuilds the
transport, and PersonalClaw moves its receiver onto the new instance (core #3628); these tests
hold one instance, so they also cover what it says when its token changes under it.

Saves go through core's own routes, served on a loopback test server and made the way the page
makes them: read the settings, then save over the revision that read reported. ``HTTPDiscordAPI``
is replaced so nothing reaches Discord.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import discord_runtime.transport as transport_mod
from discord_runtime.settings import CRED_DISCORD_BOT_TOKEN, get_settings
from discord_runtime.transport import create_provider
from personalclaw.dashboard.handlers.apps import register_app_routes
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
    """The Apps page's Configure → Save, over core's own routes: read the settings, then save
    ``values`` over the revision that read reported. The save replaces the whole file, so it
    names the copy it replaces (``If-Match``), as the page does."""
    app = web.Application()
    register_app_routes(app)
    async with TestClient(TestServer(app)) as client:
        read = await client.get(f"/api/apps/{_APP}/config")
        assert read.status == 200, await read.text()
        revision = (await read.json())["revision"]
        resp = await client.put(
            f"/api/apps/{_APP}/config", json=values, headers={"If-Match": f'"{revision}"'}
        )
        assert resp.status == 200, await resp.text()


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
    # Never driven (nothing runs its receiver here): honest about inbound.
    assert "NOT STARTED" in health["detail"], health
    assert "restart" not in health["detail"], health


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
async def test_a_token_saved_after_the_receiver_started_says_how_inbound_takes_it(
    installed, fake_api
):
    """Started without a token, the gateway session never connected. The way to start it on the
    saved one is a save or a toggle: since core #3628 a restart is not the only thing that does."""
    transport = _registry_built()
    await transport.start_inbound(object())  # no token: returns before touching services

    await _configure_save({"bot_token": "saved.token.value"})

    health = await transport.health()
    assert health["state"] == "error", health
    assert "restart" not in health["detail"], health
    assert "Configure → Save, or turning the channel off and on" in health["detail"], health
    probe = await transport.test()
    assert probe["ok"] is False and "restart" not in probe["detail"], probe
    assert "Configure → Save" in probe["detail"], probe


@pytest.mark.asyncio
async def test_a_session_still_on_an_older_token_says_how_to_move_it(installed, fake_api):
    """The Discord gateway session keeps the token it started with. A token changed under it (here,
    a save no registry rebuilt it for) reaches outbound at once, and the status says how inbound
    follows."""
    await _configure_save({"bot_token": "old.token.value"})
    transport = _registry_built()
    transport._inbound_token = "old.token.value"
    transport._gateway_task = asyncio.get_running_loop().create_future()  # a session still up
    try:
        await _configure_save({"bot_token": "new.token.value"})

        health = await transport.health()
        assert health["state"] == "error", health
        assert "still runs on the one it started with" in health["detail"], health
        assert "restart" not in health["detail"], health
        assert "Configure → Save, or turning the channel off and on" in health["detail"], health
    finally:
        transport._gateway_task.cancel()


@pytest.mark.asyncio
async def test_a_setting_saved_on_configure_is_read_without_a_restart(installed):
    assert get_settings().dm_activation == "always"

    await _configure_save({"dm_activation": "off"})

    assert get_settings().dm_activation == "off"
