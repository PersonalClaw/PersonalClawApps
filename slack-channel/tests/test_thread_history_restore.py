"""A fresh runtime picking up an existing Slack thread is handed that thread's history.

``handle_message`` bootstraps a NEW agent runtime (``is_new and not resumed``) from the thread's
persisted turns through core's ``compress_thread_history``. Core #3599 changed that function to
take the turns themselves (``list[dict]``) instead of the ``ConversationLog``; the handler kept
passing the log, so every fresh runtime over an existing thread raised ``TypeError:
'ConversationLog' object is not iterable`` — swallowed into "🔧 Something went wrong" — and the
model never received the message at all.

Everything here runs the REAL core: ``ContextBuilder``, ``ConversationLog``,
``compress_thread_history`` and the model view background compression reads through
(``ConversationLog.history_for_model``, #3603). Only the transport (``MockSlackClient``) and the
agent runtime are fakes, and the fake runtime records exactly what the model was sent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from slack_helpers import MockSlackClient

from personalclaw.context import ContextBuilder
from personalclaw.history import ConversationLog, summary_record
from personalclaw.llm.base import LLMEvent
from personalclaw.memory import MemoryStore
from personalclaw.session import BACKGROUND_KEY
from personalclaw.skills import SkillsLoader
from slack_runtime.handler import handle_message, set_allowed_users, set_owner_id

if TYPE_CHECKING:
    from personalclaw.session import SessionManager

THREAD = "9999.0001"
_FAILED = "Something went wrong"


class _Runtime:
    """An agent runtime that records every message it is sent and answers ``reply``."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.sent: list[str] = []

    async def stream(self, message, timeout=120.0):
        self.sent.append(message)
        yield LLMEvent(kind="text_chunk", text=self.reply)
        yield LLMEvent(kind="complete")

    async def approve_tool(self, rid, option_id="allow_once"):
        pass

    async def reject_tool(self, rid):
        pass

    async def start(self):
        pass

    async def shutdown(self):
        pass

    def context_usage_pct(self):
        return 0.0


class _Sessions:
    """A SessionManager whose thread runtime is always FRESH (not resumed) — the path under test —
    and whose background key serves the compression model."""

    def __init__(self) -> None:
        self.chat = _Runtime("ok")
        self.background = _Runtime("COMPRESSED-MIDDLE-OF-THE-THREAD")
        self.background_agents: list[str | None] = []

    async def get_or_create(self, key, agent=None, channel_id=None, approval_policy=None):
        if key == BACKGROUND_KEY:
            self.background_agents.append(agent)
            return self.background, False, False
        return self.chat, True, False

    async def recycle_background(self):
        pass

    def check_context_usage(self, key, provider):
        return 0.0

    def record_success(self, key):
        pass

    async def record_failure(self, key):
        return False

    def release(self, key):
        pass

    async def set_channel(self, key, channel_id):
        pass

    def get_pid(self, key):
        return None

    def set_channel_link(self, key, thread_ts, channel_id):
        pass

    def get_session_for_thread(self, thread_ts):
        return None

    def is_cancelled(self, key, msg_ts):
        return False


def _builder(tmp_path, log: ConversationLog) -> ContextBuilder:
    builder = ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
    )
    builder.conversation_log = log
    return builder


def _thread(tmp_path, turns: list[tuple[str, str]]) -> ConversationLog:
    log = ConversationLog(base_dir=tmp_path / "conv")
    for role, content in turns:
        log.append(THREAD, role, content)
    return log


async def _send(tmp_path, log: ConversationLog, text: str = "follow up") -> tuple[_Sessions, MockSlackClient]:
    set_owner_id("U001")
    set_allowed_users([{"slack_id": "U001"}])
    slack = MockSlackClient()
    sessions = _Sessions()
    await handle_message(
        slack,
        cast("SessionManager", sessions),
        "C123",
        text,
        thread_ts=THREAD,
        msg_ts="9999.0002",
        user_id="U001",
        context_builder=_builder(tmp_path, log),
        conversation_log=log,
    )
    return sessions, slack


def _posted(slack: MockSlackClient) -> str:
    return "\n".join(str(detail.get("text", "")) for _kind, detail in slack.actions)


@pytest.mark.asyncio
async def test_a_fresh_runtime_is_handed_the_threads_earlier_turns(tmp_path):
    log = _thread(tmp_path, [("user", "what port does the gateway use"), ("assistant", "10000 by default")])

    sessions, slack = await _send(tmp_path, log)

    assert sessions.chat.sent, "the model never received the turn"
    sent = sessions.chat.sent[0]
    assert "what port does the gateway use" in sent
    assert "10000 by default" in sent
    # The in-flight message is the request, once — never replayed as its own history.
    assert sent.count("follow up") == 1
    assert _FAILED not in _posted(slack)


@pytest.mark.asyncio
async def test_a_long_thread_is_compressed_by_the_background_model(tmp_path):
    """Past the compression cap the middle of the thread goes to the background model, and its
    summary — not the raw middle — is what the fresh runtime is handed."""
    turns = []
    for i in range(12):
        turns.append(("user", f"question {i} " + "q" * 3000))
        turns.append(("assistant", f"answer {i} " + "a" * 3000))
    log = _thread(tmp_path, turns)

    sessions, slack = await _send(tmp_path, log)

    assert sessions.background.sent, "the background model was never asked to compress the thread"
    assert sessions.background_agents == ["personalclaw-lite"]
    sent = sessions.chat.sent[0]
    assert "COMPRESSED-MIDDLE-OF-THE-THREAD" in sent
    assert "question 0" in sent  # the verbatim head
    assert "answer 11" in sent  # the verbatim tail
    assert "question 6" not in sent  # the middle arrives only as the summary
    assert _FAILED not in _posted(slack)


@pytest.mark.asyncio
async def test_a_background_summary_stands_in_for_the_span_it_covers(tmp_path):
    """#3603: background compression writes a summary BESIDE the thread and never rewrites it. The
    model view applies it, so a fresh runtime reads the summary instead of the turns it covers —
    which only happens if the app reads the thread through that view."""
    log = _thread(
        tmp_path,
        [
            ("user", "old question one"),
            ("assistant", "old answer one"),
            ("user", "old question two"),
            ("assistant", "old answer two"),
            ("user", "recent question"),
            ("assistant", "recent answer"),
        ],
    )
    messages = log.read_messages(THREAD)
    stamp = log.transcript_stamp(THREAD)
    assert stamp is not None
    log.write_summary(
        THREAD,
        summary_record(
            messages,
            stamp,
            summary="EARLIER: the user asked two old questions",
            summarized=4,
            reduced=4,
            reduced_cap=600,
        ),
    )

    sessions, slack = await _send(tmp_path, log)

    sent = sessions.chat.sent[0]
    assert "EARLIER: the user asked two old questions" in sent
    assert "old question one" not in sent
    assert "recent question" in sent and "recent answer" in sent
    # The thread itself is untouched: every turn is still on disk.
    assert [m["content"] for m in log.read_messages(THREAD)][:2] == ["old question one", "old answer one"]
    assert _FAILED not in _posted(slack)


@pytest.mark.asyncio
async def test_a_brand_new_thread_restores_nothing_and_still_answers(tmp_path):
    log = ConversationLog(base_dir=tmp_path / "conv")

    sessions, slack = await _send(tmp_path, log, text="first message")

    assert sessions.chat.sent and sessions.chat.sent[0].count("first message") == 1
    assert not sessions.background.sent
    assert _FAILED not in _posted(slack)
