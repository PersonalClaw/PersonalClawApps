"""A Telegram approval prompt says how its approval ended, and a late press is told.

Core ends every approval it asks here — Approve or Deny pressed in the chat, an answer in
PersonalClaw, nobody answering in the owner's window, the work that asked stopping first — and
resolves the prompt's record with how it ended. The prompt said "Rejected" for everything but an
approval, so an approval that expired, or whose turn was stopped, read as a Deny nobody gave; a
cancelled wait left the buttons; a press after the end was answered "Recorded", when nothing was;
and the prompt stopped waiting on a two-hour clock of its own while PersonalClaw still waited
(its window can be a week). A chat's progress line read "done" for every call but one still
running, whatever had happened to it.
"""

from __future__ import annotations

import asyncio

import pytest
from test_delivery import FakeAPI

from telegram_runtime.delivery import TelegramDelivery

OWNER = "42"


class _API(FakeAPI):
    """The fake Bot API, recording whether each edit still carries the buttons."""

    async def edit_message_text(self, chat_id, message_id, text, *, parse_mode=None,
                                reply_markup=None, disable_web_page_preview=None):
        await super().edit_message_text(chat_id, message_id, text, parse_mode=parse_mode)
        self.edits[-1]["reply_markup"] = reply_markup
        return {"message_id": message_id}


class _Event:
    def __init__(self, request_id: str = "req-1") -> None:
        self.request_id = request_id
        self.title = "bash"
        self.tool_input = '{"command": "make test"}'
        self.tool_purpose = ""
        self.tool_meta = {}


def _delivery() -> TelegramDelivery:
    return TelegramDelivery(_API(), lambda: OWNER)


class _Asked:
    def __init__(self, d: TelegramDelivery, request_id: str = "req-1") -> None:
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


def _press(request_id: str, button: str = "approve", who: str = OWNER) -> dict:
    return {"id": "cq-1", "data": f"{button}:{request_id}", "from": {"id": who}}


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
    assert last["reply_markup"] is None, "the buttons stayed on an approval that has ended"
    assert line in last["text"]
    if ended != "rejected":
        assert "Rejected" not in last["text"], "an approval nobody refused read as rejected"


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
    assert "Cancelled" in d._api.edits[-1]["text"]


@pytest.mark.asyncio
async def test_a_press_after_the_approval_ended_is_told_how_it_ended():
    d = _delivery()
    asked = await _Asked(d).prompted()
    asked.pending["it"].future.set_result("approved")  # answered in PersonalClaw
    await asyncio.wait_for(asked.wait, timeout=2)

    await d.resolve_callback(_press("req-1", "deny"))

    assert d._api.answers[-1]["text"] == "Already approved. This press changes nothing."


@pytest.mark.asyncio
async def test_a_press_on_a_prompt_from_before_a_restart_is_told_it_is_no_longer_waiting():
    d = _delivery()
    await d.resolve_callback(_press("req-old"))
    assert d._api.answers[-1]["text"] == (
        "This approval is no longer waiting. This press changes nothing."
    ), "a press that decided nothing was told Recorded"


@pytest.mark.asyncio
async def test_the_owner_s_press_on_a_waiting_prompt_is_still_the_answer():
    """The floor: a live prompt still takes the owner's press."""
    d = _delivery()
    asked = await _Asked(d).prompted()
    await d.resolve_callback(_press("req-1", "deny"))
    assert await asyncio.wait_for(asked.wait, timeout=2) is False
    assert d._api.answers[-1]["text"] == "Recorded"


@pytest.mark.asyncio
async def test_the_wait_keeps_no_timer_of_its_own():
    """Core's window can be a week; a prompt that gave up on a timer of its own said the approval
    was rejected while PersonalClaw still waited on it. Any timer here gives up at once."""
    import telegram_runtime.delivery as delivery

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
    sts = await d.start_stream("123", initial_text="…")
    await d.append_stream_task("123", sts, "t1", "Run the tests", status)
    st = d._streams[f"123:{sts}"]
    assert st.tasks["t1"] == line
