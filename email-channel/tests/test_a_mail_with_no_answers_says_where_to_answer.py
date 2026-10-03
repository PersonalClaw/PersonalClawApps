"""An approval mail whose brief carries no answers says so, and where to answer it, and the log
says it once.

The mail lists the answers PersonalClaw hands over in the approval brief, each with the word a reply
gives it. A PersonalClaw older than those answers sends a brief with none, and the prompt used to
return None for it: the owner got a bare "Answer it in PersonalClaw" link with no reason, and the
gateway log said nothing. Now the mail still says what will run, lists nothing to reply with, says
why and where to answer it, and ends as every prompt does; the log says it once.

The brief is replaced with what such a PersonalClaw hands over: the call, and no answers.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from test_delivery import _ONE_CALL

from personalclaw.sdk.channel import owner_id_credential

import email_runtime.delivery as mod
from email_runtime.delivery import EmailDelivery, ThreadStore
from _fakes import FakeSmtpServer

AGENT = "agent@example.com"
#: The owner's own address: this channel's owner id, the one address an approval goes to.
OWNER = "me@example.com"


class _Event:
    def __init__(self, request_id="req-1", brief=None):
        self.request_id = request_id
        self.title = "execute_bash"
        self.tool_input = ""
        self.tool_purpose = ""
        self.tool_meta = {} if brief is None else {"approval_brief": brief}


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """A delivery over a fake SMTP sink, with thread state in a tmp file and an owner."""
    smtp = FakeSmtpServer()
    monkeypatch.setenv(owner_id_credential("email"), OWNER)
    store = ThreadStore(path_provider=lambda: tmp_path / "threads.json")
    return EmailDelivery(smtp, AGENT, owner_id=AGENT, threads=store), smtp
_BRIEF = {
    "tool": "execute_bash",
    "input": '{"command": "rm -rf build"}',
    "purpose": "clear the old build",
    "risk": "destructive",
    "summary": "Can: runs a command · Risk: Destructive",
}


def _older_core(monkeypatch, brief=_BRIEF):
    monkeypatch.setattr(mod, "approval_brief_for", lambda event: dict(brief))


async def _waits_for(ready) -> None:
    """Wait until *ready()*: a send crosses to a thread, so one turn of the loop does not see it
    land, nor the prompt then report itself asked."""
    for _ in range(400):
        if ready():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("the approval mail was not sent and asked")


def _said_once(caplog) -> list[str]:
    return [
        r.getMessage() for r in caplog.records
        if r.levelno == logging.WARNING and "approval-answers" in r.getMessage()
    ]


@pytest.mark.asyncio
async def test_the_mail_lists_nothing_to_reply_with_and_says_where_to_answer(
    wired, monkeypatch, caplog
):
    delivery, smtp = wired
    _older_core(monkeypatch)
    prompted: list = []
    with caplog.at_level(logging.WARNING, logger="email_runtime.delivery"):
        task = asyncio.ensure_future(
            delivery.request_approval(_Event(), source="workflow", on_prompted=prompted.append)
        )
        await _waits_for(lambda: prompted)

    assert smtp.header("Subject") == "[PersonalClaw] Approval needed: execute_bash"
    body = smtp.body_text()
    # What will run is still said, as in any approval mail, and then why there is no reply to give.
    assert "clear the old build" in body
    assert mod._NO_ANSWERS in body
    assert "Answer it in PersonalClaw" in mod._NO_ANSWERS
    assert "Reply to this message with" not in body
    token = next(iter(delivery._pending))
    assert token not in body, "a token with no word to send it with"
    # Core counts it as asked, so it sends no bare link of its own, and ends it however it ends.
    assert len(prompted) == 1 and not task.done()
    assert len(_said_once(caplog)) == 1

    prompted[0].future.set_result("approved")  # answered in PersonalClaw
    assert await asyncio.wait_for(task, timeout=1.0) is True


@pytest.mark.asyncio
async def test_the_log_says_it_once(wired, monkeypatch, caplog):
    delivery, smtp = wired
    _older_core(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="email_runtime.delivery"):
        for rid in ("req-b", "req-c"):
            prompted: list = []
            task = asyncio.ensure_future(
                delivery.request_approval(_Event(rid), source="tool", on_prompted=prompted.append)
            )
            await _waits_for(lambda: prompted)
            assert mod._NO_ANSWERS in smtp.body_text()
            prompted[0].future.set_result("rejected")  # denied in PersonalClaw
            assert await asyncio.wait_for(task, timeout=1.0) is False
    assert len(_said_once(caplog)) == 1


@pytest.mark.asyncio
async def test_an_answer_it_cannot_list_is_not_dropped_from_the_rest(
    wired, monkeypatch, caplog
):
    """A mail offers every answer core hands over or none of them: one with no word to reply with
    would otherwise vanish from the list, and the owner would be offered less than PersonalClaw
    offers."""
    delivery, smtp = wired
    _older_core(monkeypatch, {**_BRIEF, "answers": [_ONE_CALL[0], {**_ONE_CALL[1], "word": ""}]})
    prompted: list = []
    with caplog.at_level(logging.WARNING, logger="email_runtime.delivery"):
        task = asyncio.ensure_future(
            delivery.request_approval(_Event(), source="tool", on_prompted=prompted.append)
        )
        await _waits_for(lambda: prompted)
    assert mod._NO_ANSWERS in smtp.body_text()
    assert len(_said_once(caplog)) == 1
    prompted[0].future.set_result("expired")
    assert await asyncio.wait_for(task, timeout=1.0) is False


@pytest.mark.asyncio
async def test_a_brief_with_answers_lists_them_and_logs_nothing(wired, caplog):
    """The floor: the core this app is built for hands answers over, and nothing above happens."""
    delivery, smtp = wired
    with caplog.at_level(logging.WARNING, logger="email_runtime.delivery"):
        task = asyncio.ensure_future(
            delivery.request_approval(_Event(brief={**_BRIEF, "answers": _ONE_CALL}), source="t")
        )
        await _waits_for(lambda: smtp.sent)
    token = next(iter(delivery._pending))
    body = smtp.body_text()
    assert f"APPROVE {token} — Allow once" in body and mod._NO_ANSWERS not in body
    assert _said_once(caplog) == []
    assert delivery.resolve_reply_token(f"APPROVE {token}", OWNER) is True
    assert await asyncio.wait_for(task, timeout=1.0) is True
