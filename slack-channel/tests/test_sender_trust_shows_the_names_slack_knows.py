"""Settings › Sender trust names Slack's people and channels the way the owner named them.

The Slack form takes a name beside each member ID in Allowed Users and each channel ID in Tracking
Channels, and the app writes its owner and its tracked channels through to core's sender-trust
store, which the Sender trust page lists. That write-through carried the IDs alone, and so did the
first-contact claim that makes the first person to DM the bot its owner, so the page listed
``U0ROBIN · You allowed them`` and ``C0ALERTS · tracked`` beside a Telegram row reading "Robin". The
names go through now: the one the owner gave, else the display name the app already knows. A name
given after the first write reaches the page on the next start, with the date the person or channel
was first let in kept.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personalclaw.sdk import channel as sdk_channel
from slack_runtime import handler as H
from slack_runtime.allowlist import sync_channel_trust
from slack_runtime.settings import SlackSettings


def _trust() -> dict:
    import personalclaw.channel_trust as ct

    return ct.provider_trust("slack")


def _names(rows: list[dict], id_key: str) -> dict[str, str]:
    return {r[id_key]: r["name"] for r in rows}


def _settings(**kw) -> SlackSettings:
    return SlackSettings(
        allowed_users=kw.get("users", [{"slack_id": "U0ROBIN", "name": "Robin"}]),
        tracking_channels=kw.get("channels", [{"channel_id": "C0ALERTS", "name": "alerts"}]),
    )


def test_the_start_writes_the_owner_and_the_channels_through_by_name():
    sync_channel_trust("U0ROBIN", _settings())

    trust = _trust()
    assert _names(trust["allowed_senders"], "sender_id") == {"U0ROBIN": "Robin"}
    assert _names(trust["tracked_channels"], "channel_id") == {"C0ALERTS": "alerts"}


def test_names_given_after_the_first_write_reach_the_page_on_the_next_start():
    """The persona's store: both were written through with no name before she named them."""
    sdk_channel.allow_sender("slack", "U0ROBIN", via="owner")
    sdk_channel.track("slack", "C0ALERTS")
    first = _trust()

    sync_channel_trust("U0ROBIN", _settings())

    trust = _trust()
    assert _names(trust["allowed_senders"], "sender_id") == {"U0ROBIN": "Robin"}
    assert _names(trust["tracked_channels"], "channel_id") == {"C0ALERTS": "alerts"}
    assert trust["allowed_senders"][0]["added_at"] == first["allowed_senders"][0]["added_at"]
    assert trust["tracked_channels"][0]["added_at"] == first["tracked_channels"][0]["added_at"]


def test_an_unnamed_entry_keeps_the_name_the_page_already_has():
    sdk_channel.track("slack", "C0ALERTS", "alerts")
    sync_channel_trust("", _settings(channels=[{"channel_id": "C0ALERTS"}]))
    assert _names(_trust()["tracked_channels"], "channel_id") == {"C0ALERTS": "alerts"}


def _orch(settings: SlackSettings, known: dict[str, str] | None = None) -> MagicMock:
    orch = MagicMock()
    import slack_runtime.settings as st

    st._current = settings
    orch.settings = settings
    orch.channel_history = MagicMock()
    orch.channel_history._user_names = dict(known or {})
    orch.slack = MagicMock()
    orch.slack.get_user_info = AsyncMock(return_value={})
    orch.sessions = AsyncMock()
    orch.sessions.enqueue = MagicMock(return_value=False)
    orch.sessions.is_cancelled = MagicMock(return_value=False)
    orch.sessions.dequeue = MagicMock(return_value=None)
    orch.ctx_builder = orch.cron_svc = orch.conv_log = orch.consolidator = None
    orch.subagent_mgr = orch.task_runner = None
    orch._handler_tasks, orch._session_tasks, orch._pending_queue = set(), {}, {}
    return orch


async def _first_dm(orch, sender: str) -> None:
    from slack_runtime.events import _route_message

    event = {"user": sender, "channel": "D0ROBIN", "channel_type": "im", "text": "hi", "ts": "9.0", "team": "TTEST"}
    with patch("slack_runtime.events.handle_message", new_callable=AsyncMock):
        await _route_message(orch, event, is_mention=False)
        await asyncio.sleep(0)
        await asyncio.gather(*list(orch._handler_tasks), return_exceptions=True)


@pytest.fixture
def unclaimed(monkeypatch):
    monkeypatch.setattr(H, "_owner_id", "")
    monkeypatch.setattr(H, "_allowed_users", set())
    monkeypatch.setattr("personalclaw.sdk.channel.save_credential", lambda *a, **kw: None)


@pytest.mark.asyncio
async def test_the_first_dm_claims_the_owner_under_the_name_she_gave(unclaimed):
    await _first_dm(_orch(_settings()), "U0ROBIN")

    assert H.get_owner_id() == "U0ROBIN"
    assert _names(_trust()["allowed_senders"], "sender_id") == {"U0ROBIN": "Robin"}


@pytest.mark.asyncio
async def test_with_no_name_given_the_claim_takes_the_name_slack_already_gave(unclaimed):
    orch = _orch(_settings(users=[]), known={"U0ROBIN": "Robin Ash"})
    await _first_dm(orch, "U0ROBIN")

    assert _names(_trust()["allowed_senders"], "sender_id") == {"U0ROBIN": "Robin Ash"}


@pytest.mark.asyncio
async def test_the_slack_config_panel_keeps_the_names_of_the_channels_it_keeps(monkeypatch):
    """The in-Slack config panel rewrites the tracked list from the channels picked there."""
    from personalclaw.sdk.channel import ProviderSettings
    import slack_runtime.interactions as I
    from slack_runtime.settings import reload_settings

    ProviderSettings.update("slack-channel", {"tracking_channels": [
        {"channel_id": "C0ALERTS", "name": "alerts"}, {"channel_id": "C0ENG", "name": "eng"},
    ]})
    reload_settings()
    monkeypatch.setattr(H, "_owner_id", "U0ROBIN")
    monkeypatch.setattr(I, "_orch", SimpleNamespace(_tracking_channels={"C0ALERTS", "C0ENG"}))
    picked = {"channels_block": {"pc_config_channels": {"selected_channels": ["C0ALERTS", "C0NEW"]}}}

    await I._handle_config_submission({"user": {"id": "U0ROBIN"}, "view": {"state": {"values": picked}}})

    assert ProviderSettings.load("slack-channel")["tracking_channels"] == [
        {"channel_id": "C0ALERTS", "name": "alerts"}, {"channel_id": "C0NEW"},
    ]
