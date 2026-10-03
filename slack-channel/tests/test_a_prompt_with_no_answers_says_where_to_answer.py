"""A prompt whose brief carries no answers says so, and where to answer it, and the log says it once.

The prompt's buttons are the answers PersonalClaw hands over in the approval brief. A PersonalClaw
older than those answers sends a brief with none. A prompt core asked for returned None for it, so
the owner got a bare "Answer it in PersonalClaw" link with no reason; a prompt for a thread this app
runs itself was posted with nothing to press (in a channel, an actions block Slack refuses), and
waited out the approval window. The gateway log said nothing either way.

Now a prompt core asks for still shows what will run, with no buttons, says why and where to answer
it, and ends as every prompt does. A call in a thread this app runs, which nothing else asks the
owner about, does not run, and the thread says why. The log says it once.

The brief is replaced with what such a PersonalClaw hands over: the call, and no answers.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_helpers import MockSlackClient
from test_a_thread_prompt_waits_as_personalclaw_does import _AsksOnce, _thread_text, _turn

import slack_runtime.delivery as D
import slack_runtime.handler as H
from personalclaw.llm.base import LLMEvent
from slack_runtime.delivery import SlackDelivery
from slack_runtime.format import escape_mrkdwn

OWNER = "U_OWNER"
_ONE_CALL = [
    {"key": "approved", "label": "Allow once", "ends": "approved", "word": "APPROVE", "promise": ""},
    {"key": "rejected", "label": "Deny", "ends": "rejected", "word": "DENY", "promise": ""},
]
#: What a PersonalClaw from before the answers hands a prompt: the call, and nothing to offer.
_NO_ANSWERS_BRIEF = {
    "tool": "bash",
    "input": '{"command": "rm -rf build"}',
    "purpose": "clear the old build",
    "risk": "destructive",
    "summary": "Can: writes files, runs a command · Risk: Destructive",
}


@pytest.fixture(autouse=True)
def _an_owner(monkeypatch):
    H.set_owner_id(OWNER)
    H.set_allowed_users({OWNER})
    monkeypatch.setattr(H, "_said_no_answers", False)
    for state in (H._pending_approvals, H._ended_prompts, H._thread_agents):
        state.clear()
    yield
    for state in (H._pending_approvals, H._ended_prompts, H._thread_agents):
        state.clear()
    H.set_owner_id("")
    H.set_allowed_users(set())


def _older_core(monkeypatch, brief=_NO_ANSWERS_BRIEF):
    """Both modules read the brief by name: the prompt's renderer and the delivery. The brief is
    the one a core without the answers hands over, however it is asked (``chat=`` included)."""
    monkeypatch.setattr(H, "approval_brief_for", lambda event, **_asked: dict(brief))
    monkeypatch.setattr(D, "approval_brief_for", lambda event, **_asked: dict(brief))


def _said_once(caplog) -> list[str]:
    return [
        r.getMessage() for r in caplog.records
        if r.levelno == logging.WARNING and "approval-answers" in r.getMessage()
    ]


def _event(request_id="req-1"):
    return LLMEvent(kind="permission_request", request_id=request_id, title="bash", options=[])


def _client():
    client = MagicMock()
    client.open_dm = AsyncMock(return_value="D1")
    client.post_blocks = AsyncMock(return_value="1.1")
    client.update_message = AsyncMock()
    return client


def _contexts(blocks) -> list[str]:
    return [e["text"] for b in blocks if b["type"] == "context" for e in b["elements"]]


# ── a prompt core asks for ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_prompt_has_no_buttons_and_says_where_to_answer(monkeypatch, caplog):
    _older_core(monkeypatch)
    client = _client()
    prompted: list = []
    with caplog.at_level(logging.WARNING, logger="slack_runtime.handler"):
        task = asyncio.ensure_future(
            SlackDelivery(client, lambda: OWNER).request_approval(
                _event(), source="workflow", on_prompted=prompted.append
            )
        )
        for _ in range(50):
            if prompted:
                break
            await asyncio.sleep(0)

    blocks = client.post_blocks.call_args[0][1]
    assert not [b for b in blocks if b["type"] == "actions"], "a button that answers nothing"
    # What will run is still shown, as on any prompt, and then why there is nothing to press.
    assert "clear the old build" in _contexts(blocks)
    assert escape_mrkdwn(H._NO_ANSWERS) in _contexts(blocks)
    assert "Answer it in PersonalClaw" in H._NO_ANSWERS
    # Core counts it as asked, so it sends no bare link of its own, and ends it however it ends.
    assert len(prompted) == 1 and not task.done()
    assert len(_said_once(caplog)) == 1

    prompted[0].future.set_result("approved")  # answered in PersonalClaw
    assert await asyncio.wait_for(task, timeout=1.0) is True
    closed = client.update_message.call_args.kwargs["blocks"]
    assert not [b for b in closed if b["type"] == "actions"]
    assert any("Approved" in t for t in _contexts(closed))


@pytest.mark.asyncio
async def test_the_log_says_it_once(monkeypatch, caplog):
    _older_core(monkeypatch)
    delivery = SlackDelivery(_client(), lambda: OWNER)
    with caplog.at_level(logging.WARNING, logger="slack_runtime.handler"):
        for rid, ending in (("req-b", "rejected"), ("req-c", "expired")):
            got = await delivery.request_approval(
                _event(rid), source="tool",
                on_prompted=lambda pending, e=ending: pending.future.set_result(e),
            )
            assert got is False
    assert len(_said_once(caplog)) == 1


@pytest.mark.asyncio
async def test_an_answer_it_cannot_show_is_not_dropped_from_the_rest(monkeypatch, caplog):
    """A prompt offers every answer core hands over or none of them: one without its words would
    otherwise vanish from the row, and the owner would be offered less than PersonalClaw offers."""
    _older_core(monkeypatch, {**_NO_ANSWERS_BRIEF, "answers": [_ONE_CALL[0], {"key": "rejected"}]})
    client = _client()
    with caplog.at_level(logging.WARNING, logger="slack_runtime.handler"):
        await SlackDelivery(client, lambda: OWNER).request_approval(
            _event(), source="tool", on_prompted=lambda p: p.future.set_result("rejected")
        )
    blocks = client.post_blocks.call_args[0][1]
    assert not [b for b in blocks if b["type"] == "actions"]
    assert escape_mrkdwn(H._NO_ANSWERS) in _contexts(blocks)
    assert len(_said_once(caplog)) == 1


@pytest.mark.asyncio
async def test_a_brief_with_answers_has_its_buttons_and_logs_nothing(monkeypatch, caplog):
    """The floor: the core this app is built for hands answers over, and nothing above happens."""
    _older_core(monkeypatch, {**_NO_ANSWERS_BRIEF, "answers": _ONE_CALL})
    client = _client()
    with caplog.at_level(logging.WARNING, logger="slack_runtime.handler"):
        got = await SlackDelivery(client, lambda: OWNER).request_approval(
            _event(), source="t", on_prompted=lambda p: p.future.set_result("approved")
        )
    assert got is True
    blocks = client.post_blocks.call_args[0][1]
    (actions,) = [b for b in blocks if b["type"] == "actions"]
    assert [b["text"]["text"] for b in actions["elements"]] == ["Allow once", "Deny"]
    assert escape_mrkdwn(H._NO_ANSWERS) not in _contexts(blocks)
    assert _said_once(caplog) == []


# ── a call in a thread this app runs itself ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_call_in_its_own_thread_it_cannot_ask_about_does_not_run(monkeypatch, caplog):
    """Nothing else asks the owner about a turn this app runs, so with nothing to offer the call is
    not approved by anyone: it does not run, at once rather than after the approval window, the
    thread says why, and the audit log records it as decided by nobody."""
    _older_core(monkeypatch)
    slack = MockSlackClient()
    provider = _AsksOnce()
    with (
        patch("slack_runtime.handler.sel") as sel,
        caplog.at_level(logging.WARNING, logger="slack_runtime.handler"),
    ):
        await _turn(slack, provider)

    assert provider.rejected == ["req-1"] and provider.approved == [], "the call ran"
    assert not [a for a in slack.actions if a[0] == "blocks"
                and any(b.get("type") == "actions" for b in a[1]["blocks"])], "a prompt was posted"
    said = _thread_text(slack)
    assert "could not ask you here and the call did not run" in said
    assert "RAN" not in said
    (row,) = [c.kwargs for c in sel.return_value.log_tool_invocation.call_args_list
              if c.kwargs.get("request_id") == "req-1"]
    assert row["outcome"] == "unasked"
    assert row["metadata"] == {"reason": "interactive", "decided_by": "nobody"}
    assert len(_said_once(caplog)) == 1
