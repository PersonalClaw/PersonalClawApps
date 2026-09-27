"""Tokens saved on the Apps page reach Test, Connect, health and send without a restart.

The registry built the transport ONCE, from the app store as it was at enable time, and the Apps
page's Configure → Save (``PUT /api/apps/{name}/config``) wrote the store and re-cycled nothing.
The transport resolved its tokens in ``__init__``, so after a successful Save every surface kept
answering "No bot token configured" until the app was reinstalled or the gateway restarted. A
save now also rebuilds the transport, and the gateway moves its receiver onto the new instance
(core #3628); these tests hold one instance, so they also cover what it says when its tokens
change under it.

Saves here go through core's own routes, served on a loopback test server and made the way the
dashboard makes them (read the settings, then save over the revision that read reported), so the
test writes exactly what the dashboard writes. Nothing reaches Slack: ``RealSlackClient`` is
replaced wherever a probe would use it.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import slack_runtime.transport as transport_mod
from personalclaw.dashboard.handlers.apps import register_app_routes
from personalclaw.providers.settings import ProviderSettings
from slack_runtime.settings import get_settings
from slack_runtime.transport import create_provider

_APP = "slack-channel"
_BUNDLE = Path(__file__).resolve().parents[1]


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """A scratch home with this app installed and nothing configured."""
    monkeypatch.setenv("PERSONALCLAW_HOME", str(tmp_path))
    owner_keys = ("PERSONALCLAW_OWNER_ID", "PERSONALCLAW_OWNER_ID_SLACK")
    for key in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", *owner_keys):
        monkeypatch.delenv(key, raising=False)
    home_app = tmp_path / "apps" / _APP
    home_app.mkdir(parents=True)
    shutil.copy(_BUNDLE / "app.json", home_app / "app.json")
    return tmp_path


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


class _FakeClient:
    """``RealSlackClient`` stand-in: records the token it was built with and what it sent."""

    built_with: list[str] = []
    posted: list[dict] = []

    def __init__(self, token: str) -> None:
        self.token = token
        _FakeClient.built_with.append(token)

    async def auth_test(self):
        return {"team": "Acme"}

    async def post_message(self, **kwargs):
        _FakeClient.posted.append({"token": self.token, **kwargs})


@pytest.fixture
def fake_client(monkeypatch):
    _FakeClient.built_with, _FakeClient.posted = [], []
    monkeypatch.setattr(transport_mod, "RealSlackClient", _FakeClient)
    return _FakeClient


class _Services:
    """GatewayServices stand-in for ``start_inbound``: a config handle, no live services."""

    sessions = ctx_builder = conv_log = consolidator = None
    subagent_mgr = channel_history = dashboard_state = None
    owner_id = ""

    def __init__(self) -> None:
        from personalclaw.sdk.channel import AppConfig

        self.config = AppConfig.load()


def _registry_built():
    """What ``ChannelTypeHandler.create`` does when the app is enabled."""
    return create_provider(ProviderSettings.load(_APP))


@pytest.mark.asyncio
async def test_tokens_saved_after_enable_reach_connect_and_health(installed):
    transport = _registry_built()
    assert (await transport.health())["detail"] == "No bot token configured"

    await _configure_save({"bot_token": "fake-bot-token-saved", "app_token": "fake-app-token-1"})

    assert transport.connected is True
    assert await transport.connect() is True
    health = await transport.health()
    assert health["state"] != "offline", health
    assert "No bot token" not in health["detail"], health
    # Never driven (nothing runs its receiver here): honest about inbound, and not a restart.
    assert "NOT STARTED" in health["detail"] and "restart" not in health["detail"], health


@pytest.mark.asyncio
async def test_test_authenticates_with_the_saved_token(installed, fake_client):
    transport = _registry_built()
    await _configure_save({"bot_token": "fake-bot-token-saved", "app_token": "fake-app-token-1"})

    probe = await transport.test()

    assert fake_client.built_with == ["fake-bot-token-saved"]
    assert "Authenticated to Acme" in probe["detail"], probe


@pytest.mark.asyncio
async def test_send_goes_out_on_the_saved_token(installed, fake_client):
    transport = _registry_built()
    await _configure_save({"bot_token": "fake-bot-token-saved"})

    from personalclaw.sdk.channel import OutboundMessage

    assert await transport.send(OutboundMessage(channel_id="C1", text="hi")) is True
    assert fake_client.posted == [{"token": "fake-bot-token-saved", "channel": "C1", "text": "hi", "thread_ts": None}]


@pytest.mark.asyncio
async def test_a_rotated_token_sends_on_the_new_one_not_the_receivers(installed, fake_client):
    """The receiver's client carries the token inbound started with. Once the owner rotates the
    bot token, outbound must stop borrowing that client."""
    await _configure_save({"bot_token": "fake-bot-token-old", "app_token": "fake-app-token-2"})
    transport = _registry_built()
    transport._inbound_tokens = ("fake-bot-token-old", "fake-app-token-2")
    transport._runtime = type("_Rt", (), {"slack": _FakeClient("fake-bot-token-old")})()

    await _configure_save({"bot_token": "fake-bot-token-new", "app_token": "fake-app-token-2"})
    from personalclaw.sdk.channel import OutboundMessage

    await transport.send(OutboundMessage(channel_id="C1", text="hi"))
    assert fake_client.posted[-1]["token"] == "fake-bot-token-new"


@pytest.mark.asyncio
async def test_the_boot_reason_does_not_outlive_the_token_it_was_about(installed, fake_client):
    """Started with nothing configured, inbound stayed offline for "no Bot Token". After a save that
    sentence is false; what is true is that the receiver takes the saved tokens at its next start,
    and a save or a toggle starts it (since core #3628 a restart is not the only thing that does)."""
    transport = _registry_built()
    await transport.start_inbound(_Services())
    assert "no Bot Token" in transport._inbound_offline_reason

    await _configure_save({"bot_token": "fake-bot-token-saved", "app_token": "fake-app-token-1"})

    health = await transport.health()
    assert health["state"] == "error", health
    assert "no Bot Token" not in health["detail"]
    assert "restart" not in health["detail"], health
    assert "Configure → Save, or turning the channel off and on" in health["detail"], health
    probe = await transport.test()
    assert probe["ok"] is False and "restart" not in probe["detail"], probe
    assert "Configure → Save" in probe["detail"], probe


@pytest.mark.asyncio
async def test_socket_mode_still_on_older_tokens_says_how_to_move_it(installed, fake_client):
    """Socket Mode keeps the tokens it connected with. Tokens changed under it (here, a save no
    registry rebuilt it for) reach outbound at once, and the status says how inbound follows."""
    await _configure_save({"bot_token": "bot-token-old", "app_token": "app-token-old"})
    transport = _registry_built()
    transport._inbound_tokens = ("bot-token-old", "app-token-old")
    transport._inbound_started = True

    await _configure_save({"bot_token": "bot-token-new", "app_token": "app-token-old"})

    health = await transport.health()
    assert health["state"] == "error", health
    assert "still connected with the ones it started with" in health["detail"], health
    assert "restart" not in health["detail"], health
    assert "Configure → Save, or turning the channel off and on" in health["detail"], health


@pytest.mark.asyncio
async def test_a_setting_saved_on_configure_is_read_without_a_restart(installed):
    assert get_settings().dm_activation == "always"

    await _configure_save({"dm_activation": "mention"})

    assert get_settings().dm_activation == "mention"


def test_an_explicit_config_is_not_replaced_by_an_unchanged_store(installed):
    """The conformance kit builds ``SlackTransport({})``; a store it never wrote must not leak in."""
    (installed / "apps" / _APP / "data").mkdir(parents=True, exist_ok=True)
    (installed / "apps" / _APP / "data" / "config.json").write_text(
        json.dumps({"bot_token": "fake-bot-token-machine"}), encoding="utf-8"
    )
    transport = create_provider({})
    assert transport.connected is False
