"""A reply to the Morning triage digest in the owner's DM is PersonalClaw's to answer.

PersonalClaw's digest tells the owner, in the DM it reached, to answer an item with ``3 yes``. A DM
here is a conversation this app runs itself, so ``3 yes`` became a turn of it, and the digest was
never answered. Now the app offers the owner's DM message to PersonalClaw before it would run a
turn (``services.answer_channel_reply``): one PersonalClaw takes as an answer runs no turn here, and
anything else is this conversation's as before. Whether a message IS an answer (the owner, the DM
that digest reached, the digest's own grammar) is PersonalClaw's to decide, and its own suite holds
that; this file holds what the app does with the decision.
"""

from __future__ import annotations

from typing import Any

import pytest
from slack_helpers import MockSlackClient
from test_slack_handler import FakeSessionManager

import slack_runtime.handler as H

OWNER = "U0OWNER"
DM = "D0OWNER"


class _Services:
    """The gateway's services handle, as far as the offer goes: who was offered, and its answer."""

    def __init__(self, *, takes: bool) -> None:
        self.takes = takes
        self.offered: list[dict[str, Any]] = []

    async def answer_channel_reply(self, provider: str, msg: Any, *, is_dm: bool = True) -> bool:
        self.offered.append(
            {
                "provider": provider,
                "channel": msg.channel_id,
                "text": msg.text,
                "sender": msg.sender,
                "thread": msg.thread_id,
                "is_dm": is_dm,
            }
        )
        return self.takes


@pytest.fixture(autouse=True)
def _the_owner(monkeypatch):
    H.set_owner_id(OWNER)
    H.set_allowed_users({OWNER})
    monkeypatch.setattr(H, "_dashboard_state", None)
    yield
    H.set_owner_id("")
    H.set_allowed_users(set())


@pytest.mark.asyncio
async def test_her_answer_in_her_dm_is_personalclaws_and_runs_no_turn(monkeypatch):
    services = _Services(takes=True)
    monkeypatch.setattr(H, "_gateway_services", services)
    slack, sessions = MockSlackClient(), FakeSessionManager()

    await H.handle_message(slack, sessions, DM, "3 yes", None, "1700000400.000100", OWNER)

    assert services.offered == [
        {
            "provider": "slack",
            "channel": DM,
            "text": "3 yes",
            "sender": OWNER,
            "thread": "1700000400.000100",
            "is_dm": True,
        }
    ]
    assert sessions.keys_seen == [], "a turn ran for an answer PersonalClaw took"
    assert slack.actions == [], "PersonalClaw says what the answer did, not this app"


@pytest.mark.asyncio
async def test_a_message_personalclaw_does_not_take_is_this_conversations(monkeypatch):
    """The pair: the same offer, refused, and the turn runs as it always did."""
    services = _Services(takes=False)
    monkeypatch.setattr(H, "_gateway_services", services)
    slack, sessions = MockSlackClient(), FakeSessionManager()

    await H.handle_message(slack, sessions, DM, "what is on today?", None, "1700000400.000200", OWNER)

    assert [offer["text"] for offer in services.offered] == ["what is on today?"]
    assert sessions.keys_seen == ["1700000400.000200"]


@pytest.mark.asyncio
async def test_a_message_in_a_channel_is_never_offered(monkeypatch):
    """The digest reaches the owner's DM, so only a DM can answer it."""
    services = _Services(takes=True)
    monkeypatch.setattr(H, "_gateway_services", services)
    slack, sessions = MockSlackClient(), FakeSessionManager()

    await H.handle_message(slack, sessions, "C0TEAM", "3 yes", None, "1700000400.000300", OWNER)

    assert services.offered == []
    assert sessions.keys_seen == ["1700000400.000300"]
