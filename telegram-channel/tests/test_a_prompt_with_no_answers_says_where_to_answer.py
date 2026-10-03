"""A prompt whose brief carries no answers says so, and where to answer it, and the log says it once.

The prompt's buttons are the answers PersonalClaw hands over in the approval brief. A PersonalClaw
older than those answers sends a brief with none, and the prompt used to return None for it: the
owner got a bare "Answer it in PersonalClaw" link with no reason, a workflow step timed out waiting
on one such approval, and the gateway log said nothing. Now the prompt still shows what will run,
with nothing to press, says why and where to answer it, and ends as every prompt does; the log
says it once.

The brief is replaced with what such a PersonalClaw hands over: the call, and no answers.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from test_delivery import _BRIEF, _ONE_CALL, _delivery, _Event

import telegram_runtime.delivery as mod
from telegram_runtime.format import to_markdown_v2

#: What a PersonalClaw from before the answers hands a prompt: the call, and nothing to offer.
_NO_ANSWERS_BRIEF = {k: v for k, v in _BRIEF.items() if k != "answers"}


def _older_core(monkeypatch, brief=_NO_ANSWERS_BRIEF):
    monkeypatch.setattr(mod, "approval_brief_for", lambda event: dict(brief))


def _said_once(caplog) -> list[str]:
    return [
        r.getMessage() for r in caplog.records
        if r.levelno == logging.WARNING and "approval-answers" in r.getMessage()
    ]


@pytest.mark.asyncio
async def test_the_prompt_has_no_buttons_and_says_where_to_answer(monkeypatch, caplog):
    _older_core(monkeypatch)
    d = _delivery(owner="42")
    prompted: list = []
    with caplog.at_level(logging.WARNING, logger="telegram_runtime.delivery"):
        task = asyncio.ensure_future(
            d.request_approval(_Event("reqA", "execute_bash"), source="workflow",
                               on_prompted=prompted.append)
        )
        await asyncio.sleep(0)

    prompt = d._api.sent[-1]
    assert prompt["chat_id"] == "42"
    assert prompt["reply_markup"] is None, "a button that answers nothing"
    # What will run is still shown, as on any prompt, and then why there is nothing to press.
    assert to_markdown_v2("ship the staging build") in prompt["text"]
    assert to_markdown_v2(mod._NO_ANSWERS) in prompt["text"]
    assert "Answer it in PersonalClaw" in mod._NO_ANSWERS
    # Core counts it as asked, so it sends no bare link of its own, and ends it however it ends.
    assert len(prompted) == 1 and not task.done()
    assert len(_said_once(caplog)) == 1

    prompted[0].future.set_result("approved")  # answered in PersonalClaw
    assert await asyncio.wait_for(task, timeout=1.0) is True
    assert any("Approved" in e["text"] for e in d._api.edits)


@pytest.mark.asyncio
async def test_the_log_says_it_once(monkeypatch, caplog):
    _older_core(monkeypatch)
    d = _delivery(owner="42")
    with caplog.at_level(logging.WARNING, logger="telegram_runtime.delivery"):
        for rid in ("reqB", "reqC"):
            prompted: list = []
            task = asyncio.ensure_future(
                d.request_approval(_Event(rid), source="tool", on_prompted=prompted.append)
            )
            await asyncio.sleep(0)
            prompted[0].future.set_result("rejected")  # denied in PersonalClaw
            assert await asyncio.wait_for(task, timeout=1.0) is False
    assert len(_said_once(caplog)) == 1
    assert [m["reply_markup"] for m in d._api.sent] == [None, None]
    assert all(to_markdown_v2(mod._NO_ANSWERS) in m["text"] for m in d._api.sent)


@pytest.mark.asyncio
async def test_an_answer_it_cannot_show_is_not_dropped_from_the_rest(monkeypatch, caplog):
    """A prompt offers every answer core hands over or none of them: one without its words would
    otherwise vanish from the keyboard, and the owner would be offered less than PersonalClaw
    offers."""
    _older_core(monkeypatch, {**_NO_ANSWERS_BRIEF, "answers": [_ONE_CALL[0], {"key": "rejected"}]})
    d = _delivery(owner="42")
    prompted: list = []
    with caplog.at_level(logging.WARNING, logger="telegram_runtime.delivery"):
        task = asyncio.ensure_future(
            d.request_approval(_Event("reqD"), source="tool", on_prompted=prompted.append)
        )
        await asyncio.sleep(0)
    assert d._api.sent[-1]["reply_markup"] is None
    assert len(_said_once(caplog)) == 1
    prompted[0].future.set_result("expired")
    assert await asyncio.wait_for(task, timeout=1.0) is False


@pytest.mark.asyncio
async def test_a_brief_with_answers_has_its_buttons_and_logs_nothing(caplog):
    """The floor: the core this app is built for hands answers over, and nothing above happens."""
    d = _delivery(owner="42")
    with caplog.at_level(logging.WARNING, logger="telegram_runtime.delivery"):
        task = asyncio.ensure_future(d.request_approval(_Event("reqE", brief=_BRIEF), source="t"))
        await asyncio.sleep(0)
    prompt = d._api.sent[-1]
    assert [row[0]["text"] for row in prompt["reply_markup"]["inline_keyboard"]] == [
        "✅ Allow once", "🚫 Deny"
    ]
    assert to_markdown_v2(mod._NO_ANSWERS) not in prompt["text"]
    assert _said_once(caplog) == []
    await d.resolve_callback(
        {"id": "o", "data": "a0:reqE", "from": {"id": 42}, **d._api.press_on_prompt()}
    )
    assert await asyncio.wait_for(task, timeout=1.0) is True
