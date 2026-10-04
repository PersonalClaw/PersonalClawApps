"""The thread's next message goes out at once after a turn this app stopped reading.

This app runs some turns itself, in its own threads (``client.stream``), and stops reading one
before its end: at its terminal event, after a call whose approval was not given (the owner
pressed Reject, or nobody answered in time), and when a Slack call fails part way. An agent CLI
sends one prompt at a time: the turn's event stream holds its session until the stream is
closed, and closing the stream part way is what tells the agent to stop. The handler left each
stream open, so the agent went on with a turn nobody read, and the session went back only when
the interpreter collected the stream: at once on one Python, seconds later or not at all on
another. Until then the thread's next message waited, unsent.

Each turn, and each ``!compact``, is now read inside ``closing_stream``: leaving it by any way out
closes the stream at once, which tells the agent to stop and gives the session back.

Driven over a real agent CLI session (``AcpSession``, behind the session-backed provider core
runs one with) on a stand-in connection, so the session's own turn lock decides when a prompt is
sent. Every stream the provider hands out is kept by the test, so no collection can close one and
what the test sees is the same on every Python.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from slack_helpers import MockSlackClient, set_owner
from slack_sdk.errors import SlackApiError
from test_slack_handler import FakeSessionManager

import slack_runtime.handler as H
from personalclaw.acp.session import AcpSession
from personalclaw.acp.types import (
    METHOD_COMMANDS_EXECUTE,
    METHOD_COMPACTION_STATUS,
    METHOD_PROMPT,
    METHOD_REQUEST_PERMISSION,
    METHOD_SESSION_UPDATE,
    JsonRpcMessage,
)
from personalclaw.llm.acp_session_provider import AcpSessionProvider

OWNER = "U_OWNER"
DM = "D_OWNER"
#: The thread: the ts of its first message, which the next message replies under.
THREAD = "1712793600.000100"
NEXT = "1712793600.000200"
SESSION = "stand-in-session"
#: How long the thread's next prompt may take to reach the agent. The stand-in answers in
#: milliseconds, so anything near this is a prompt that never went.
AT_ONCE = 5.0
EXPIRED_LINE = "Nobody answered in time, so it did not run"
REJECTED_LINE = "Tool use rejected"


@pytest.fixture(autouse=True)
def _an_owner():
    set_owner(OWNER)
    H.set_allowed_users({OWNER})
    for state in (H._pending_approvals, H._ended_prompts, H._thread_agents):
        state.clear()
    yield
    for state in (H._pending_approvals, H._ended_prompts, H._thread_agents):
        state.clear()
    set_owner("")
    H.set_allowed_users(set())


class _Agent:
    """A stand-in agent CLI with one session, behind the provider core runs a session with.

    *script* says what it does with each prompt, in order: ``"asks"`` asks about one call and
    goes on working until it is told to stop, ``"works"`` writes a line and goes on working until
    it is told to stop, and any other text is its whole answer. Told to stop
    (``session/cancel``), it answers the prompt it is on as cancelled. A ``!compact`` it answers
    at once, saying how much it freed. ``kept`` holds every stream the provider handed out."""

    def __init__(self, *script: str) -> None:
        self.script = list(script)
        self.queue: asyncio.Queue[JsonRpcMessage] = asyncio.Queue()
        self.requests: list[tuple[str, dict, asyncio.Future]] = []
        self.cancels = 0
        self.kept: list = []
        session = AcpSession(
            SESSION,
            self.queue,
            send_request=self._send_request,
            send_response=self._send_response,
            cancel_session=self._told_to_stop,
            is_process_alive=lambda: True,
        )
        connection = SimpleNamespace(
            supports_native_commands=True,
            agent_capabilities={},
            _transport=None,
            close_session=self._close_session,
        )
        self.provider = AcpSessionProvider(connection, session, runtime_id="acp:stand-in")
        for name in ("stream", "stream_command"):
            setattr(self.provider, name, self._keeping(getattr(self.provider, name)))

    def _keeping(self, read: Callable) -> Callable:
        def keep(text: str):
            events = read(text)
            self.kept.append(events)
            return events

        return keep

    async def _send_request(self, method: str, params: dict):
        answer = asyncio.get_running_loop().create_future()
        self.requests.append((method, params, answer))
        n = len(self.requests)
        if method == METHOD_PROMPT:
            what = self.script.pop(0)
            if what == "asks":
                self.queue.put_nowait(
                    JsonRpcMessage(
                        id=900 + n,
                        method=METHOD_REQUEST_PERMISSION,
                        params={
                            "sessionId": SESSION,
                            "toolCall": {
                                "title": "Write File",
                                "toolCallId": f"call-{n}",
                                "kind": "edit",
                            },
                            "options": [
                                {"id": "allow", "label": "Allow", "kind": "allow_once"},
                                {"id": "reject", "label": "Reject", "kind": "reject_once"},
                            ],
                        },
                    )
                )
            elif what == "works":
                self._says("Drafting the release notes.")
            else:
                self._says(what)
                answer.set_result(JsonRpcMessage(id=n, result={"stopReason": "end_turn"}))
        elif method == METHOD_COMMANDS_EXECUTE:
            self.queue.put_nowait(
                JsonRpcMessage(
                    method=METHOD_COMPACTION_STATUS,
                    params={
                        "sessionId": SESSION,
                        "status": {"type": "completed"},
                        "summary": "freed 40% of the context",
                    },
                )
            )
            answer.set_result(JsonRpcMessage(id=n, result={}))
        return n, answer

    def _says(self, text: str) -> None:
        self.queue.put_nowait(
            JsonRpcMessage(
                method=METHOD_SESSION_UPDATE,
                params={
                    "sessionId": SESSION,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": text},
                    },
                },
            )
        )

    async def _send_response(self, req_id, result) -> None:
        return None

    async def _told_to_stop(self) -> None:
        self.cancels += 1
        for n, (method, _params, answer) in enumerate(self.requests, start=1):
            if method == METHOD_PROMPT and not answer.done():
                answer.set_result(JsonRpcMessage(id=n, result={"stopReason": "cancelled"}))

    async def _close_session(self, session_id: str) -> None:
        return None

    def prompts(self) -> list[str]:
        """The text of every prompt the agent was sent, in order."""
        return [
            "".join(block.get("text", "") for block in params["prompt"])
            for method, params, _answer in self.requests
            if method == METHOD_PROMPT
        ]

    async def let_go(self) -> None:
        """Close what the test kept, so nothing is left waiting once it ends."""
        for events in self.kept:
            await events.aclose()


class _SlackThatRefusesTheFirstReply(MockSlackClient):
    """Slack refuses the first message of the reply (``chat.postMessage``), as it does when it
    rate-limits the app, and takes every message after it."""

    def __init__(self) -> None:
        super().__init__()
        self.refused = 0

    async def post_message(self, channel, text, thread_ts=None, **kwargs):
        if text == H._THINKING and not self.refused:
            self.refused += 1
            raise SlackApiError("ratelimited", {"ok": False, "error": "ratelimited"})
        return await super().post_message(channel, text, thread_ts, **kwargs)


async def _until(ready: Callable[[], bool], what: str, within: float = AT_ONCE) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while not ready():
        if loop.time() > deadline:
            raise AssertionError(what)
        await asyncio.sleep(0.01)


def _prompts(slack: MockSlackClient) -> list[str]:
    """The ts of each posted message carrying the approval's buttons."""
    return [
        a[1]["ts"]
        for a in slack.actions
        if a[0] == "blocks" and any(b.get("type") == "actions" for b in a[1]["blocks"])
    ]


async def _press(slack: MockSlackClient, action: str) -> None:
    for _ in range(300):
        prompts = _prompts(slack)
        if prompts and H._pending_approvals:
            await H.handle_interaction(DM, prompts[-1], action, user_id=OWNER)
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the prompt was never posted")


def _thread_text(slack: MockSlackClient) -> str:
    return " ".join(
        str(a[1].get("text") or "")
        for a in slack.actions
        if a[0] in ("post", "update", "append_stream", "stop_stream")
    )


async def _thread(slack, agent: _Agent, first, then: str = "and the summary?") -> str:
    """The owner's first message in the DM (*first*), then the next one in its thread, which
    must reach the agent at once. Returns the state the first turn's stream was in when the
    handling of the first message returned."""
    sessions = FakeSessionManager(agent.provider)
    second = None
    try:
        await asyncio.wait_for(first(sessions), timeout=AT_ONCE)
        left = inspect.getasyncgenstate(agent.kept[0])
        second = asyncio.ensure_future(
            H.handle_message(slack, sessions, DM, then, THREAD, NEXT, OWNER)
        )
        await _until(
            lambda: len(agent.prompts()) == 2,
            "the thread's next message was never sent: the turn before still held the session",
        )
        await asyncio.wait_for(second, timeout=AT_ONCE)
        return left
    finally:
        if second is not None and not second.done():
            second.cancel()
            await asyncio.gather(second, return_exceptions=True)
        await agent.let_go()


#: Each way the handler stops reading a turn, with what the agent does with the prompt and what
#: the thread says about it.
STOPS = {
    "the agent finished": ("The notes are drafted.", "The notes are drafted."),
    "nobody answered": ("asks", EXPIRED_LINE),
    "rejected": ("asks", REJECTED_LINE),
    "slack refused": ("works", "Drafting the release notes."),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("stopped_by", sorted(STOPS))
async def test_a_turn_it_stops_reading_is_closed_and_the_threads_next_message_goes_at_once(
    monkeypatch, stopped_by
):
    """🔴 Before: the turn's stream was left open where the handler stopped reading it. Stopped
    part way, the agent was never told to stop and the session was still held, so the next
    message was never sent."""
    monkeypatch.setattr(
        H, "approval_window_secs", lambda: 0.2 if stopped_by == "nobody answered" else 60.0
    )
    does, thread_says = STOPS[stopped_by]
    agent = _Agent(does, "Here is the summary.")
    slack = _SlackThatRefusesTheFirstReply() if stopped_by == "slack refused" else MockSlackClient()

    async def first(sessions) -> None:
        turn = H.handle_message(slack, sessions, DM, "write the release notes", None, THREAD, OWNER)
        if stopped_by == "rejected":
            await asyncio.gather(turn, _press(slack, "reject_tool"))
        else:
            await turn

    with patch("slack_runtime.handler.sel"):
        left = await _thread(slack, agent, first)

    assert agent.prompts() == ["write the release notes", "and the summary?"]
    assert left == inspect.AGEN_CLOSED, "the turn's stream was left open"
    if stopped_by == "the agent finished":
        assert agent.cancels == 0, "a turn the agent finished was told to stop"
    else:
        assert agent.cancels == 1, "the agent was never told to stop the turn nobody read"
    said = _thread_text(slack)
    assert thread_says in said
    assert "Here is the summary." in said, "the next message was never answered"
    if stopped_by == "slack refused":
        assert slack.refused == 1


@pytest.mark.asyncio
async def test_a_turn_read_to_its_end_answers_as_before_and_the_next_message_goes_at_once():
    """The control: the agent answers the turn whole, the thread shows the answer, and its next
    message goes out at once, with the fix as without it."""
    agent = _Agent("The notes are drafted.", "Here is the summary.")
    slack = MockSlackClient()

    async def first(sessions) -> None:
        await H.handle_message(slack, sessions, DM, "write the release notes", None, THREAD, OWNER)

    with patch("slack_runtime.handler.sel"):
        await _thread(slack, agent, first)

    assert agent.prompts() == ["write the release notes", "and the summary?"]
    assert agent.cancels == 0, "a turn the agent finished was told to stop"
    said = _thread_text(slack)
    assert "The notes are drafted." in said and "Here is the summary." in said


@pytest.mark.asyncio
async def test_a_compact_the_agent_has_answered_closes_its_stream():
    """🔴 Before: the handler stopped reading the command's stream at its terminal event and left
    it open."""
    agent = _Agent("Here is the summary.")
    slack = MockSlackClient()
    sessions = FakeSessionManager(agent.provider)
    sessions._sessions = {THREAD: SimpleNamespace(provider=agent.provider)}
    try:
        with patch("slack_runtime.handler.sel"):
            await asyncio.wait_for(
                H.handle_message(slack, sessions, DM, "!compact", THREAD, NEXT, OWNER),
                timeout=AT_ONCE,
            )
        (command,) = agent.kept
        assert inspect.getasyncgenstate(command) == inspect.AGEN_CLOSED, (
            "the command's stream was left open"
        )
        assert "Compacted: freed 40% of the context" in _thread_text(slack)
    finally:
        await agent.let_go()
