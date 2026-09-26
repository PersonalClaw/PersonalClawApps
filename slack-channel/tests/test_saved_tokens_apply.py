"""Tokens saved on the Apps page reach Test, Connect, health and send without a restart.

The registry builds the transport ONCE, from the app store as it is at enable time, and the Apps
page's Configure → Save (``PUT /api/apps/{name}/config``) writes the store and re-cycles nothing.
The transport resolved its tokens in ``__init__``, so after a successful Save every surface kept
answering "No bot token configured" until the app was reinstalled or the gateway restarted.

Saves here go through core's own route handler, so the test writes exactly what the dashboard
writes. Nothing opens a socket: ``RealSlackClient`` is replaced wherever a probe would use it.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import slack_runtime.transport as transport_mod
from personalclaw.dashboard.handlers.apps import api_app_config_put
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
    """The Apps page's Configure → Save: core's PUT /api/apps/{name}/config handler."""

    class _Request(dict):
        match_info = {"name": _APP}

        async def json(self):
            return values

    resp = await api_app_config_put(_Request())
    assert resp.status == 200, resp.text


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

    await _configure_save({"bot_token": "xoxb-saved", "app_token": "xapp-1-saved"})

    assert transport.connected is True
    assert await transport.connect() is True
    health = await transport.health()
    assert health["state"] != "offline", health
    assert "No bot token" not in health["detail"], health


@pytest.mark.asyncio
async def test_test_authenticates_with_the_saved_token(installed, fake_client):
    transport = _registry_built()
    await _configure_save({"bot_token": "xoxb-saved", "app_token": "xapp-1-saved"})

    probe = await transport.test()

    assert fake_client.built_with == ["xoxb-saved"]
    assert "Authenticated to Acme" in probe["detail"], probe


@pytest.mark.asyncio
async def test_send_goes_out_on_the_saved_token(installed, fake_client):
    transport = _registry_built()
    await _configure_save({"bot_token": "xoxb-saved"})

    from personalclaw.sdk.channel import OutboundMessage

    assert await transport.send(OutboundMessage(channel_id="C1", text="hi")) is True
    assert fake_client.posted == [{"token": "xoxb-saved", "channel": "C1", "text": "hi", "thread_ts": None}]


@pytest.mark.asyncio
async def test_a_rotated_token_sends_on_the_new_one_not_the_receivers(installed, fake_client):
    """The receiver's client carries the token inbound started with. Once the owner rotates the
    bot token, outbound must stop borrowing that client."""
    await _configure_save({"bot_token": "xoxb-old", "app_token": "xapp-1-old"})
    transport = _registry_built()
    transport._inbound_tokens = ("xoxb-old", "xapp-1-old")
    transport._runtime = type("_Rt", (), {"slack": _FakeClient("xoxb-old")})()

    await _configure_save({"bot_token": "xoxb-new", "app_token": "xapp-1-old"})
    from personalclaw.sdk.channel import OutboundMessage

    await transport.send(OutboundMessage(channel_id="C1", text="hi"))
    assert fake_client.posted[-1]["token"] == "xoxb-new"


@pytest.mark.asyncio
async def test_the_boot_reason_does_not_outlive_the_token_it_was_about(installed, fake_client):
    """Booted with nothing configured, inbound stayed offline for "no Bot Token". After a save that
    sentence is false; what is true is that the receiver takes the saved tokens at the next start."""
    transport = _registry_built()
    await transport.start_inbound(_Services())
    assert "no Bot Token" in transport._inbound_offline_reason

    await _configure_save({"bot_token": "xoxb-saved", "app_token": "xapp-1-saved"})

    health = await transport.health()
    assert health["state"] == "error", health
    assert "no Bot Token" not in health["detail"]
    assert "next restart" in health["detail"], health
    probe = await transport.test()
    assert probe["ok"] is False and "next restart" in probe["detail"], probe


@pytest.mark.asyncio
async def test_a_setting_saved_on_configure_is_read_without_a_restart(installed):
    assert get_settings().dm_activation == "always"

    await _configure_save({"dm_activation": "mention"})

    assert get_settings().dm_activation == "mention"


def test_an_explicit_config_is_not_replaced_by_an_unchanged_store(installed):
    """The conformance kit builds ``SlackTransport({})``; a store it never wrote must not leak in."""
    (installed / "apps" / _APP / "data").mkdir(parents=True, exist_ok=True)
    (installed / "apps" / _APP / "data" / "config.json").write_text(
        json.dumps({"bot_token": "xoxb-machine"}), encoding="utf-8"
    )
    transport = create_provider({})
    assert transport.connected is False
