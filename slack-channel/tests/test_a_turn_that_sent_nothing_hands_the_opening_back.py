"""A thread's turn that sent its new runtime nothing hands the session's opening back.

A session's opening, its instructions and safety rules, goes with the first message its runtime is
sent. On a new thread session the app took it when the turn started, and when the message was
deleted before it was sent, the turn ended without handing it back. The thread's next message then
reached the runtime without them, the rule that marks outside content as untrusted included. The
turn now hands the opening back through core's ``SessionManager.hand_back_opening``, as the
dashboard's chat does, so the next message carries it.

Driven through the app's handler, with a session manager that keeps the opening as core's does.
"""

from __future__ import annotations

import pytest
from slack_helpers import MockSlackClient, set_owner
from test_slack_handler import FakeProvider, FakeSessionManager

import slack_runtime.handler as H
from personalclaw.config.credentials import owner_id_credential
from personalclaw.config.loader import CRED_OWNER_ID

OWNER = "U0MIRAOWNR"
CHANNEL = "C0EXAMPLE02"
THREAD = "1712793600.000500"


@pytest.fixture(autouse=True)
def _an_owner(monkeypatch):
    monkeypatch.delenv(CRED_OWNER_ID, raising=False)
    monkeypatch.setenv(owner_id_credential("slack"), OWNER)
    set_owner(OWNER)
    H.set_allowed_users({OWNER})
    yield
    set_owner("")
    H.set_allowed_users(set())


class _Agent(FakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.sent: list[str] = []

    async def stream(self, message, timeout=120.0):
        self.sent.append(message)
        async for event in super().stream(message, timeout):
            yield event


class _Sessions(FakeSessionManager):
    """Tells a turn its runtime is new until a message is sent, and takes the opening back as
    core's ``hand_back_opening`` does: for the runtime the turn held."""

    def __init__(self, agent, *, deleted: set[str] = frozenset()) -> None:
        super().__init__(agent)
        self._deleted = set(deleted)
        self.handed_back: list[str] = []
        self.told_new: list[bool] = []

    async def get_or_create(self, key, agent=None, channel_id=None):
        provider, is_new, resumed = await super().get_or_create(key, agent, channel_id)
        self.told_new.append(is_new)
        return provider, is_new, resumed

    def is_cancelled(self, key, msg_ts):
        return msg_ts in self._deleted

    def hand_back_opening(self, key, provider):
        self.handed_back.append(key)
        if provider is self._provider:
            self._is_new = True


class _OlderCore(_Sessions):
    """A core from before the hand-back: no ``hand_back_opening`` at all."""

    hand_back_opening = property(lambda self: (_ for _ in ()).throw(AttributeError("old core")))


async def _send(sessions, msg_ts: str) -> None:
    await H.handle_message(MockSlackClient(), sessions, CHANNEL, "hello", THREAD, msg_ts, OWNER)


@pytest.mark.asyncio
async def test_a_message_deleted_before_it_was_sent_hands_the_opening_back():
    """🔴 Red before: the next message was sent as the session's second, with no opening."""
    agent = _Agent()
    sessions = _Sessions(agent, deleted={"m1"})

    await _send(sessions, "m1")
    await _send(sessions, "m2")

    assert sessions.handed_back == [THREAD]
    assert agent.sent == ["hello"], "only the second message was sent"
    assert sessions.told_new == [True, True], "the second turn carries the opening"


@pytest.mark.asyncio
async def test_a_turn_that_sent_its_message_keeps_the_opening_spent():
    agent = _Agent()
    sessions = _Sessions(agent)

    await _send(sessions, "m1")
    await _send(sessions, "m2")

    assert sessions.handed_back == []
    assert sessions.told_new == [True, False]


@pytest.mark.asyncio
async def test_a_core_without_the_hand_back_ends_the_turn_as_before():
    agent = _Agent()
    sessions = _OlderCore(agent, deleted={"m1"})

    await _send(sessions, "m1")

    assert agent.sent == []
