"""Only the owner answers an approval on Slack: Allow once, Allow for this chat and Deny alike.

An allowlisted user may talk to the agent and run the "any allowed user" commands, but a press
on an approval decides what runs with the owner's authority, and a prompt in a linked channel
thread is in front of everyone in the channel. Both prompts a press can answer are covered: the
one a Slack turn posts itself (``_request_approval``) and the one core asks through
``SlackDelivery.request_approval``. They share one resolver, ``handle_interaction``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_helpers import MockSlackClient

from slack_runtime.handler import (
    _PendingApproval,
    _pending_approvals,
    handle_interaction,
    set_allowed_users,
    set_owner_id,
)

OWNER = "U_OWNER"
COLLEAGUE = "U_ALLOWED"  # on the allowlist: may talk to the agent, may not answer for the owner
CHANNEL = "C_TEAM"
PROMPT_TS = "1700000000.000100"
THREAD = "1.0"


class _Provider:
    def __init__(self) -> None:
        self.approved: list[str] = []
        self.rejected: list[str] = []

    async def approve_tool(self, request_id, option_id="allow_once"):
        self.approved.append(request_id)

    async def reject_tool(self, request_id):
        self.rejected.append(request_id)


@pytest.fixture(autouse=True)
def _owner_and_a_colleague():
    set_owner_id(OWNER)
    set_allowed_users({OWNER, COLLEAGUE})
    _pending_approvals.clear()
    yield
    _pending_approvals.clear()
    set_owner_id("")
    set_allowed_users(set())


@pytest.fixture
def kept():
    """What PersonalClaw is handed for a press on this app's own prompt (``answer_in_chat``)."""
    with patch("slack_runtime.handler.answer_in_chat", return_value=True) as answer_in_chat:
        yield answer_in_chat


#: What a turn this app runs itself offers in a DM, where PersonalClaw can hold the chat's Trust:
#: Allow once, Allow for this chat and Deny (core's brief for the event, in its conversation).
_IN_ITS_CHAT = [
    {"key": "approved", "label": "Allow once", "ends": "approved", "word": "APPROVE", "promise": ""},
    {
        "key": "trust",
        "label": "Allow for this chat",
        "ends": "approved",
        "word": "TRUST",
        "promise": "Every tool in this chat runs without asking, until you change it back.",
    },
    {"key": "rejected", "label": "Deny", "ends": "rejected", "word": "DENY", "promise": ""},
]


def _prompt(session_key: str = "thread-1") -> tuple[_PendingApproval, _Provider]:
    """A turn's approval prompt, pending on ``CHANNEL`` at ``PROMPT_TS``."""
    provider = _Provider()
    pending = _PendingApproval(provider, "req-1", session_key, answers=_IN_ITS_CHAT)  # type: ignore[arg-type]
    _pending_approvals[f"{CHANNEL}:{PROMPT_TS}"] = pending
    return pending, provider


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["approve_tool", "pc_answer_trust", "reject_tool"])
async def test_a_colleagues_press_answers_nothing(action, kept):
    pending, provider = _prompt()
    slack = MockSlackClient()
    with patch("slack_runtime.handler.sel") as sel:
        result = await handle_interaction(CHANNEL, PROMPT_TS, action, user_id=COLLEAGUE, slack=slack)

    assert result is None
    assert not pending.future.done(), "the colleague decided the owner's approval"
    assert provider.approved == [] and provider.rejected == []
    kept.assert_not_called()
    assert f"{CHANNEL}:{PROMPT_TS}" in _pending_approvals, "the owner can still answer it"
    row = sel.return_value.log_api_access.call_args.kwargs
    assert row["caller"] == COLLEAGUE and row["outcome"] == "denied"
    assert row["error"] == "not the owner"
    told = [a[1] for a in slack.actions if a[0] == "ephemeral"]
    assert [(t["user_id"], t["text"]) for t in told] == [(COLLEAGUE, "Only the owner can answer this.")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "approved", "rejected", "outcome"),
    [
        ("approve_tool", ["req-1"], [], "approved"),
        ("pc_answer_trust", ["req-1"], [], "trust"),
        ("reject_tool", [], ["req-1"], "rejected"),
    ],
)
async def test_the_owner_still_answers(action, approved, rejected, outcome, kept):
    pending, provider = _prompt()
    with patch("slack_runtime.handler.sel"):
        result = await handle_interaction(
            CHANNEL, PROMPT_TS, action, user_id=OWNER, thread_ts=THREAD
        )

    assert result == action
    assert pending.future.result() == outcome
    assert provider.approved == approved and provider.rejected == rejected
    (handed,) = kept.call_args_list
    assert handed.args == ("thread-1", outcome)
    assert handed.kwargs == {"channel": "slack", "request_id": "req-1"}


@pytest.mark.asyncio
@pytest.mark.parametrize("who", [OWNER, COLLEAGUE])
async def test_a_late_press_of_allow_for_this_chat_trusts_nothing(who, kept):
    """Once the approval has ended nothing waits on its prompt, and a press there, the owner's or
    anyone's, answers nothing and trusts nothing."""
    slack = MockSlackClient()
    with patch("slack_runtime.handler.sel") as sel:
        result = await handle_interaction(
            "D_OWNER", PROMPT_TS, "pc_answer_trust", user_id=who, thread_ts=THREAD, slack=slack
        )

    kept.assert_not_called()
    row = sel.return_value.log_api_access.call_args.kwargs
    if who == OWNER:
        assert result == "late_press"
        assert row["error"] == "no_pending_approval"
    else:
        assert result is None
        assert row["error"] == "not the owner"


@pytest.mark.asyncio
async def test_the_prompt_core_asks_through_slack_is_the_owners_to_answer():
    """``SlackDelivery.request_approval`` — core asking the owner on Slack, in the chat's linked
    channel thread here — is answered by the same resolver, so the same rule holds."""
    from personalclaw.llm.base import LLMEvent
    from slack_runtime.delivery import SlackDelivery

    client = MagicMock()
    client.post_blocks = AsyncMock(return_value=PROMPT_TS)
    client.update_message = AsyncMock()
    sessions = MagicMock()
    sessions.get_channel.return_value = CHANNEL
    sessions.get_thread.return_value = "1699999999.000001"
    event = LLMEvent(
        kind="permission_request",
        request_id="req-core",
        title="bash",
        options=[],
        tool_input='{"command": "ls"}',
    )

    with patch("slack_runtime.handler.sel"):
        asked = asyncio.ensure_future(
            SlackDelivery(client, lambda: OWNER).request_approval(
                event, source="subagent", parent_session_key="dashboard:chat-1", sessions=sessions
            )
        )
        for _ in range(100):
            if f"{CHANNEL}:{PROMPT_TS}" in _pending_approvals:
                break
            await asyncio.sleep(0.01)
        assert await handle_interaction(CHANNEL, PROMPT_TS, "approve_tool", user_id=COLLEAGUE) is None
        await asyncio.sleep(0)
        assert not asked.done(), "a colleague answered the approval core asked the owner"

        assert await handle_interaction(CHANNEL, PROMPT_TS, "approve_tool", user_id=OWNER) == "approve_tool"
        assert await asyncio.wait_for(asked, timeout=1.0) is True
