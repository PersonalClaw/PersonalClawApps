"""A prompt a Slack thread's own turn raises waits as long as PersonalClaw does, and ends as core's do.

This app runs some turns itself, in its own threads, and asks the owner about their calls with a
prompt of its own. That prompt gave up after 120 seconds of its own, whatever the owner's approval
wait said (it can be a week). And an approval nobody answered was audited as a rejection and
told in the thread as "Tool use rejected": a Deny nobody gave.

It now waits PersonalClaw's window, read when it asks. An unanswered prompt ends ``expired``: the
call does not run, the thread says nobody answered in time, and the audit log records it as
decided by nobody. A press is the owner's decision, and a wait the turn's stop cancels ends
``cancelled``, decided by nobody too.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from slack_helpers import MockSlackClient
from test_slack_handler import FakeSessionManager

import slack_runtime.handler as H
from personalclaw.llm.base import LLMEvent

OWNER = "U_OWNER"
DM = "D_OWNER"
EXPIRED_LINE = "Nobody answered in time, so it did not run"


@pytest.fixture(autouse=True)
def _an_owner():
    H.set_owner_id(OWNER)
    H.set_allowed_users({OWNER})
    for state in (H._pending_approvals, H._ended_prompts, H._trusted_sessions, H._thread_agents):
        state.clear()
    yield
    for state in (H._pending_approvals, H._ended_prompts, H._trusted_sessions, H._thread_agents):
        state.clear()
    H.set_owner_id("")
    H.set_allowed_users(set())


class _AsksOnce:
    """A turn that asks about one call, then says it ran; told the answer by the prompt."""

    def __init__(self) -> None:
        self.approved: list = []
        self.rejected: list = []
        self._answered = asyncio.Event()

    async def stream(self, message, timeout=120.0):
        yield LLMEvent(kind="permission_request", request_id="req-1", title="Write File", options=[])
        await self._answered.wait()
        yield LLMEvent(kind="text_chunk", text="RAN")
        yield LLMEvent(kind="complete")

    async def approve_tool(self, request_id, option_id="allow_once"):
        self.approved.append(request_id)
        self._answered.set()

    async def reject_tool(self, request_id):
        self.rejected.append(request_id)
        self._answered.set()

    async def start(self):
        pass

    async def shutdown(self):
        pass

    def context_usage_pct(self):
        return 0.0


def _window(monkeypatch, seconds: float) -> list[int]:
    """PersonalClaw's approval window, as the SDK reports it; returns a record of each read.
    ``raising=False`` so that without the fix the prompt keeps its own clock and the test fails
    on what it does, not on a missing name."""
    reads: list[int] = []

    def window() -> float:
        reads.append(1)
        return seconds

    monkeypatch.setattr(H, "approval_window_secs", window, raising=False)
    return reads


async def _turn(slack, provider) -> None:
    await asyncio.wait_for(
        H.handle_message(
            slack, FakeSessionManager(provider), DM, "write it", None, "msg1", OWNER,
            approval_mode="interactive",
        ),
        timeout=5,
    )


def _asked(sel_mock) -> list[dict]:
    """The audit rows for the asked call."""
    return [
        c.kwargs
        for c in sel_mock.return_value.log_tool_invocation.call_args_list
        if c.kwargs.get("request_id") == "req-1"
    ]


def _prompts(slack: MockSlackClient) -> list[str]:
    """The ts of each posted message carrying the Approve and Reject buttons: the prompts, not
    the turn's other messages (its footer is Block Kit too)."""
    return [
        a[1]["ts"]
        for a in slack.actions
        if a[0] == "blocks" and any(b.get("type") == "actions" for b in a[1]["blocks"])
    ]


def _thread_text(slack: MockSlackClient) -> str:
    return " ".join(
        str(a[1].get("text") or "")
        for a in slack.actions
        if a[0] in ("post", "update", "append_stream", "stop_stream")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True], ids=["edited", "streamed"])
async def test_a_prompt_nobody_answers_waits_personalclaws_window_then_ends_expired(
    monkeypatch, streamed
):
    """🔴 Before: the prompt waited 120 s of its own (this turn would not end in 5 s), audited
    the call `rejected` and told the thread it was rejected."""
    reads = _window(monkeypatch, 0.2)
    slack = MockSlackClient()
    slack._stream_enabled = streamed
    provider = _AsksOnce()

    with patch("slack_runtime.handler.sel") as sel:
        await _turn(slack, provider)

    assert reads == [1], "the window was not read when the prompt asked"
    assert provider.rejected == ["req-1"] and provider.approved == [], "the call ran"
    (row,) = _asked(sel)
    assert row["outcome"] == "expired"
    assert row["metadata"] == {"reason": "interactive", "decided_by": "nobody"}
    said = _thread_text(slack)
    assert EXPIRED_LINE in said, "the thread never said why the call did not run"
    assert "rejected" not in said.lower(), "an approval nobody refused read as rejected"
    assert "RAN" not in said


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "outcome", "thread_says"),
    [
        ("approve_tool", "approved", "RAN"),
        ("reject_tool", "rejected", "Tool use rejected"),
    ],
)
async def test_the_owners_press_decides_and_is_audited_as_theirs(
    monkeypatch, action, outcome, thread_says
):
    _window(monkeypatch, 60.0)
    slack = MockSlackClient()
    provider = _AsksOnce()

    async def press() -> None:
        for _ in range(300):
            prompts = _prompts(slack)
            if prompts and H._pending_approvals:
                await H.handle_interaction(DM, prompts[-1], action, user_id=OWNER)
                return
            await asyncio.sleep(0.01)
        raise AssertionError("the prompt was never posted")

    with patch("slack_runtime.handler.sel") as sel:
        await asyncio.gather(_turn(slack, provider), press())

    (row,) = _asked(sel)
    assert row["outcome"] == outcome
    assert row["metadata"] == {"reason": "interactive", "decided_by": "you"}
    assert thread_says in _thread_text(slack)


@pytest.mark.asyncio
async def test_a_wait_the_turns_stop_cancels_ends_cancelled():
    """🔴 Before: a cancelled wait wrote no audit row at all."""
    slack = MockSlackClient()
    provider = _AsksOnce()
    event = LLMEvent(kind="permission_request", request_id="req-1", title="Write File", options=[])

    with patch("slack_runtime.handler.sel") as sel:
        wait = asyncio.ensure_future(
            H._request_approval(slack, provider, DM, "1.0", event, "thread-1")
        )
        for _ in range(300):
            if H._pending_approvals:
                break
            await asyncio.sleep(0.01)
        (key,) = H._pending_approvals
        wait.cancel()
        with pytest.raises(asyncio.CancelledError):
            await wait
        for _ in range(100):  # the prompt is closed on its own, off the cancelled wait
            if any(a[0] == "update" for a in slack.actions):
                break
            await asyncio.sleep(0.01)

    (row,) = _asked(sel)
    assert row["outcome"] == "cancelled"
    assert row["metadata"] == {"reason": "interactive", "decided_by": "nobody"}
    assert H.ended_prompt(key) == "cancelled"
    assert provider.approved == [] and provider.rejected == []


@pytest.mark.asyncio
async def test_a_press_after_the_prompt_expired_is_told_so_and_changes_nothing(monkeypatch):
    """At the handler: the prompt is gone once it ends, so only a stale client could still show
    its buttons, and a press there is told how the approval ended."""
    _window(monkeypatch, 0.1)
    slack = MockSlackClient()
    provider = _AsksOnce()
    with patch("slack_runtime.handler.sel"):
        await _turn(slack, provider)
        (prompt_ts,) = _prompts(slack)
        got = await H.handle_interaction(
            DM, prompt_ts, "approve_tool", user_id=OWNER, slack=slack
        )

    assert got == H.LATE_PRESS
    told = [a[1]["text"] for a in slack.actions if a[0] == "ephemeral"]
    assert told == [f"{EXPIRED_LINE}. This press changes nothing."]
    assert provider.approved == [], "a press after the end approved the call"
