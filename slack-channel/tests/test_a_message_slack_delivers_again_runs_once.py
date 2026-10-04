"""A message Slack delivers again is answered once.

Slack delivers an event again when it is not sure its acknowledgement arrived (a socket that
dropped, a gateway stopped before the acknowledgement went out), and it announces a message that
mentions the bot twice, as a ``message`` and an ``app_mention``. This app kept the timestamps it
had handled in memory, one set per start, so a delivery made again after a restart ran its turn a
second time. Each message is now claimed with PersonalClaw (``claim_message``), whose record of
messages already received is kept in its home, by the channel and the timestamp Slack names it
with: a delivery made again changes nothing, however it came. A message in a thread linked to a
dashboard chat crosses PersonalClaw's guarded door, which takes it from the claim, once.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from slack_runtime.events import _route_message
from slack_runtime.settings import ACTIVATION_ALWAYS, SlackSettings


def _orch() -> MagicMock:
    """A gateway's Slack runtime, as one start of it holds it."""
    orch = MagicMock()
    settings = SlackSettings(channels={}, dm_activation=ACTIVATION_ALWAYS)
    import slack_runtime.settings as _st

    _st._current = settings
    orch.settings = settings
    orch.channel_history = MagicMock()
    orch.slack = MagicMock()
    orch.sessions = AsyncMock()
    orch.sessions.enqueue = MagicMock(return_value=False)
    orch.sessions.is_cancelled = MagicMock(return_value=False)
    orch.sessions.dequeue = MagicMock(return_value=None)
    orch.sessions.clear_queue = MagicMock()
    orch.ctx_builder = None
    orch.conv_log = None
    orch.consolidator = None
    orch.subagent_mgr = None
    orch._handler_tasks = set()
    orch._session_tasks = {}
    orch._pending_queue = {}
    return orch


def _dm(ts: str = "1712793600.000100", *, channel: str = "D0OWNER", text: str = "hello") -> dict:
    return {"user": "U0OWNER", "channel": channel, "text": text, "ts": ts, "team": "TTEST"}


async def _deliver(orch, event: dict, *, is_mention: bool = False) -> None:
    await _route_message(orch, dict(event), is_mention=is_mention)
    await asyncio.sleep(0)
    await asyncio.gather(*list(orch._handler_tasks), return_exceptions=True)


@pytest.mark.asyncio
async def test_a_message_delivered_twice_is_answered_once():
    orch = _orch()
    with (
        patch("slack_runtime.events.handle_message", new_callable=AsyncMock) as handled,
        patch("slack_runtime.events.is_allowed_user", return_value=True),
    ):
        await _deliver(orch, _dm())
        await _deliver(orch, _dm())

    assert handled.call_count == 1


@pytest.mark.asyncio
async def test_a_message_delivered_again_after_a_restart_is_answered_once():
    """A new start of the receiver holds nothing of the last one: what knows the message is
    PersonalClaw's record, in its home."""
    with (
        patch("slack_runtime.events.handle_message", new_callable=AsyncMock) as handled,
        patch("slack_runtime.events.is_allowed_user", return_value=True),
    ):
        await _deliver(_orch(), _dm())
        await _deliver(_orch(), _dm())

    assert handled.call_count == 1


@pytest.mark.asyncio
async def test_a_mention_slack_announces_twice_is_answered_once():
    orch = _orch()
    mention = _dm(channel="D0OWNER", text="<@U0BOT> hello")
    with (
        patch("slack_runtime.events.handle_message", new_callable=AsyncMock) as handled,
        patch("slack_runtime.events.is_allowed_user", return_value=True),
    ):
        await _deliver(orch, mention, is_mention=False)
        await _deliver(orch, mention, is_mention=True)

    assert handled.call_count == 1


@pytest.mark.asyncio
async def test_messages_slack_names_apart_are_each_answered():
    """The partner of the tests above: another timestamp is another message, and so is the same
    timestamp in another channel, since Slack's timestamps name a message only in its channel."""
    orch = _orch()
    with (
        patch("slack_runtime.events.handle_message", new_callable=AsyncMock) as handled,
        patch("slack_runtime.events.is_allowed_user", return_value=True),
    ):
        await _deliver(orch, _dm("1712793600.000100", text="yes"))
        await _deliver(orch, _dm("1712793600.000200", text="yes"))
        await _deliver(orch, _dm("1712793600.000100", channel="D0OTHER", text="yes"))

    assert handled.call_count == 3


@pytest.mark.asyncio
async def test_a_linked_threads_message_crosses_the_door_once():
    """Claimed here, then handed to the guarded door by the real handler: the door takes it from
    the claim and runs one turn, and the delivery made again reaches neither."""
    from personalclaw.channel_inbound import deliver_inbound
    from personalclaw.sdk.channel import TrustVerdict

    from slack_runtime import handler

    session = MagicMock()
    type(session).running = PropertyMock(return_value=False)
    session.key = "session1"
    state = MagicMock()
    state.get_linked_session = MagicMock(return_value=session)
    state._background_tasks = set()
    turns: list[str] = []

    class _Services:
        dashboard_state = state

        async def deliver_channel_inbound(self, provider, msg, *, is_dm=True):
            async def turn_runner(st, chat, text):
                turns.append(text)

            return await deliver_inbound(self, provider, msg, is_dm=is_dm, turn_runner=turn_runner)

    def _allowed(*a, **kw):
        return TrustVerdict(allowed=True, reason="allowed")

    orch = _orch()
    with (
        patch.object(handler, "_dashboard_state", state),
        patch.object(handler, "_gateway_services", _Services()),
        patch.object(handler, "is_allowed_user", return_value=True),
        patch("slack_runtime.events.is_allowed_user", return_value=True),
        patch("personalclaw.channel_inbound.admit", _allowed),
    ):
        await _deliver(orch, _dm(text="what's on today?"))
        await asyncio.sleep(0)
        await _deliver(orch, _dm(text="what's on today?"))
        await asyncio.sleep(0)

    assert turns == ["what's on today?"]
