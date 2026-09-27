"""Only the owner answers an approval on Slack: Approve, Trust session and Reject alike.

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
    _trusted_sessions,
    handle_interaction,
    set_allowed_users,
    set_owner_id,
)

OWNER = "U_OWNER"
COLLEAGUE = "U_ALLOWED"  # on the allowlist: may talk to the agent, may not answer for the owner
CHANNEL = "C_TEAM"
PROMPT_TS = "1700000000.000100"


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
    _trusted_sessions.clear()
    yield
    _pending_approvals.clear()
    _trusted_sessions.clear()
    set_owner_id("")
    set_allowed_users(set())


def _prompt(session_key: str = "thread-1") -> tuple[_PendingApproval, _Provider]:
    """A turn's approval prompt, pending on ``CHANNEL`` at ``PROMPT_TS``."""
    provider = _Provider()
    pending = _PendingApproval(provider, "req-1", session_key)  # type: ignore[arg-type]
    _pending_approvals[f"{CHANNEL}:{PROMPT_TS}"] = pending
    return pending, provider


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["approve_tool", "trust_tool", "reject_tool"])
async def test_a_colleagues_press_answers_nothing(action):
    pending, provider = _prompt()
    slack = MockSlackClient()
    with patch("slack_runtime.handler.sel") as sel:
        result = await handle_interaction(CHANNEL, PROMPT_TS, action, user_id=COLLEAGUE, slack=slack)

    assert result is None
    assert not pending.future.done(), "the colleague decided the owner's approval"
    assert provider.approved == [] and provider.rejected == []
    assert "thread-1" not in _trusted_sessions
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
        ("trust_tool", ["req-1"], [], "approved"),
        ("reject_tool", [], ["req-1"], "rejected"),
    ],
)
async def test_the_owner_still_answers(action, approved, rejected, outcome):
    pending, provider = _prompt()
    with patch("slack_runtime.handler.sel"):
        result = await handle_interaction(CHANNEL, PROMPT_TS, action, user_id=OWNER)

    assert result == action
    assert pending.future.result() == outcome
    assert provider.approved == approved and provider.rejected == rejected
    assert ("thread-1" in _trusted_sessions) is (action == "trust_tool")


@pytest.mark.asyncio
async def test_a_colleagues_late_trust_trusts_nothing():
    """Trust pressed after the approval ended still grants trust to the thread, so it is an
    answer too: a colleague who started the thread cannot give it."""
    slack = MockSlackClient()
    slack.fetch_thread_replies = AsyncMock(return_value=[{"user": COLLEAGUE}])
    with patch("slack_runtime.handler.sel") as sel:
        result = await handle_interaction(
            "D_COLLEAGUE", PROMPT_TS, "trust_tool", user_id=COLLEAGUE, thread_ts="1.0", slack=slack
        )

    assert result is None
    assert not _trusted_sessions
    assert sel.return_value.log_api_access.call_args.kwargs["error"] == "not the owner"


@pytest.mark.asyncio
async def test_the_owners_late_trust_still_trusts_their_thread():
    slack = MockSlackClient()
    slack.fetch_thread_replies = AsyncMock(return_value=[{"user": OWNER}])
    with (
        patch("slack_runtime.handler.sel"),
        patch("personalclaw.sdk.channel.SessionMap") as session_map,
    ):
        session_map.return_value.get_session_for_thread.return_value = None
        result = await handle_interaction(
            "D_OWNER", PROMPT_TS, "trust_tool", user_id=OWNER, thread_ts="1.0", slack=slack
        )

    assert result == "trust_tool"
    assert _trusted_sessions == {"1.0"}


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
