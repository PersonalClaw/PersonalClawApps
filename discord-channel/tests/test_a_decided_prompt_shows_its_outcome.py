"""A Discord approval prompt says how its approval ended, and a press that decides nothing says so.

Core ends every approval it asks here — Approve or Deny pressed in the chat, an answer in
PersonalClaw, nobody answering in the owner's window, the work that asked stopping first — and
resolves the prompt's record with how it ended. The prompt said "Rejected" for everything but an
approval, so an approval that expired, or whose turn was stopped, read as a Deny nobody gave; a
cancelled wait left the buttons; a press after the end, or anyone's but the owner's, was
acknowledged in silence, as if it had worked; and the prompt stopped waiting on a two-hour clock
of its own while PersonalClaw still waited. A chat's progress line read "done" for every call but
one still running, whatever had happened to it.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from test_delivery import FakeAPI

from discord_runtime.api import INTERACTION_CALLBACK_MESSAGE, MESSAGE_FLAG_EPHEMERAL
from discord_runtime.delivery import INTERACTION_TYPE_COMPONENT, DiscordDelivery

OWNER = "42"


class _Event:
    def __init__(self, request_id: str = "req-1") -> None:
        self.request_id = request_id
        self.title = "bash"
        self.tool_input = '{"command": "make test"}'
        self.tool_purpose = ""
        self.tool_meta = {}


def _delivery() -> DiscordDelivery:
    return DiscordDelivery(FakeAPI(), lambda: OWNER)


class _Asked:
    def __init__(self, d: DiscordDelivery, request_id: str = "req-1") -> None:
        self.pending: dict = {}
        self.wait = asyncio.ensure_future(
            d.request_approval(
                _Event(request_id),
                source="chat",
                on_prompted=lambda p: self.pending.setdefault("it", p),
            )
        )

    async def prompted(self) -> "_Asked":
        for _ in range(200):
            if self.pending:
                return self
            await asyncio.sleep(0.01)
        raise AssertionError("the prompt was never posted")


#: The buttons an approval with no chat of its own offers, by their place: Allow once, Deny.
_BUTTONS = {"approve": "a0", "deny": "a1"}


def _press(request_id: str, button: str = "approve", who: str = OWNER) -> dict:
    return {
        "id": "i-1",
        "token": "t-1",
        "type": INTERACTION_TYPE_COMPONENT,
        "data": {"custom_id": f"{_BUTTONS[button]}:{request_id}"},
        "user": {"id": who},
    }


def _told(d: DiscordDelivery) -> str:
    """What the last press was answered with, in a message only the presser sees."""
    ack = d._api.acks[-1]
    assert ack["type"] == INTERACTION_CALLBACK_MESSAGE, "the press was acknowledged in silence"
    assert ack["data"]["flags"] == MESSAGE_FLAG_EPHEMERAL
    return ack["data"]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ended", "line"),
    [
        ("approved", "✅ Approved"),
        ("rejected", "🚫 Rejected"),
        ("expired", "⌛ Nobody answered in time, so it did not run"),
        ("cancelled", "⏹️ Cancelled: the work that asked for it stopped first, so it did not run"),
    ],
)
async def test_the_prompt_says_how_its_approval_ended_and_loses_its_buttons(ended, line):
    d = _delivery()
    asked = await _Asked(d).prompted()
    asked.pending["it"].future.set_result(ended)
    assert await asyncio.wait_for(asked.wait, timeout=2) is (ended == "approved")
    last = d._api.edits[-1]
    assert last["components"] == []
    assert line in last["content"]
    if ended != "rejected":
        assert "Rejected" not in last["content"], "an approval nobody refused read as rejected"


@pytest.mark.asyncio
async def test_a_wait_that_is_cancelled_closes_its_prompt():
    d = _delivery()
    asked = await _Asked(d).prompted()
    asked.wait.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asked.wait
    for _ in range(100):
        if d._api.edits:
            break
        await asyncio.sleep(0.01)
    assert d._api.edits, "the cancelled prompt kept its buttons"
    assert d._api.edits[-1]["components"] == []
    assert "Cancelled" in d._api.edits[-1]["content"]


@pytest.mark.asyncio
async def test_a_press_after_the_approval_ended_is_told_how_it_ended():
    d = _delivery()
    asked = await _Asked(d).prompted()
    asked.pending["it"].future.set_result("expired")
    await asyncio.wait_for(asked.wait, timeout=2)

    await d.resolve_interaction(_press("req-1"))

    assert _told(d) == "Nobody answered in time, so it did not run. This press changes nothing."


@pytest.mark.asyncio
async def test_a_press_on_a_prompt_from_before_a_restart_is_told_it_is_no_longer_waiting():
    d = _delivery()
    await d.resolve_interaction(_press("req-old"))
    assert _told(d) == "This approval is no longer waiting. This press changes nothing."


@pytest.mark.asyncio
async def test_someone_else_s_press_is_told_it_answers_nothing():
    d = _delivery()
    asked = await _Asked(d).prompted()
    with patch("discord_runtime.delivery.sel"):
        await d.resolve_interaction(_press("req-1", who="999"))
    assert _told(d) == "Only the owner can answer this."
    assert not asked.wait.done(), "someone who is not the owner answered it"
    asked.wait.cancel()


@pytest.mark.asyncio
async def test_the_owner_s_press_on_a_waiting_prompt_is_still_the_answer():
    """The floor: a live prompt takes the owner's press, acknowledged without a message."""
    d = _delivery()
    asked = await _Asked(d).prompted()
    await d.resolve_interaction(_press("req-1", "deny"))
    assert await asyncio.wait_for(asked.wait, timeout=2) is False
    assert d._api.acks[-1]["type"] == 6 and d._api.acks[-1]["data"] is None


@pytest.mark.asyncio
async def test_the_wait_keeps_no_timer_of_its_own():
    """Core's window can be a week; a prompt that gave up on a timer of its own said the approval
    was rejected while PersonalClaw still waited on it. Any timer here gives up at once."""
    import discord_runtime.delivery as delivery

    async def gives_up_at_once(awaitable, timeout=None):
        raise asyncio.TimeoutError

    # Scoped: the patch reaches every module's asyncio, this test's own included.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(delivery.asyncio, "wait_for", gives_up_at_once)
        d = _delivery()
        asked = await _Asked(d).prompted()
        await asyncio.sleep(0.05)
        assert not asked.wait.done(), "the prompt stopped waiting on its own clock"
    asked.pending["it"].future.set_result("approved")
    assert await asyncio.wait_for(asked.wait, timeout=2) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "line"),
    [
        ("in_progress", "⏳ Run the tests"),
        ("complete", "✅ Run the tests"),
        ("failed", "❌ Run the tests (failed)"),
        ("rejected", "🚫 Run the tests (rejected)"),
        ("expired", "⌛ Run the tests (no answer in time, not run)"),
        ("cancelled", "⏹️ Run the tests (cancelled, not run)"),
        ("a-new-ending", "• Run the tests (a-new-ending)"),
    ],
)
async def test_a_progress_line_says_how_its_call_ended(status, line):
    d = _delivery()
    sts = await d.start_stream("500", initial_text="…")
    await d.append_stream_task("500", sts, "t1", "Run the tests", status)
    st = d._streams[f"500:{sts}"]
    assert st.tasks["t1"] == line
