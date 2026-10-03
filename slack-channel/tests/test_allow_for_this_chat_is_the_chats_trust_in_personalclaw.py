"""Allow for this chat, on a prompt of a thread this app runs itself, is the chat's Trust.

The prompt for a DM thread this app runs itself offered "Trust session", which trusted the thread
inside this app: the chat PersonalClaw lists for the thread neither said it was trusted nor could
switch it off, the security log had no row for the grant, and only YOLO turning off or a restart
ended it.

Now the prompt offers what the chat's approval card offers for the call (PersonalClaw's brief for
it, in that conversation), and Allow for this chat pressed there is the Trust of PersonalClaw's
chat for the thread: the chat shows it, the thread's next calls run on it, and switching the chat
to Normal in the dashboard makes the next call ask here again. The app keeps no trust of its own.

Driven where Slack hands this app a press (``interactions.dispatch``) and where a thread's turn
asks (``handle_message``), with this app's Slack double and a model runtime that asks about one
call each turn, against PersonalClaw's own dashboard state and routes, in the test's scratch home.
"""

from __future__ import annotations

import asyncio
import itertools
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from slack_helpers import MockSlackClient
from test_slack_handler import FakeSessionManager

import slack_runtime.handler as H
import slack_runtime.interactions as interactions
from personalclaw.llm.base import LLMEvent

OWNER = "U0OWNER"
DM = "D0OWNER"
THREAD = "1700000100.000100"
THIS_CHAT = "Every tool in this chat runs without asking, until you change it back."
#: The ts of each later message of the owner's in the thread.
_LATER = (f"1700000100.{n:06d}" for n in itertools.count(200))


@pytest.fixture(autouse=True)
def _the_owners_dm(monkeypatch):
    H.set_owner_id(OWNER)
    H.set_allowed_users({OWNER})
    for state in (H._pending_approvals, H._ended_prompts, H._thread_agents):
        state.clear()
    monkeypatch.setattr(H, "approval_window_secs", lambda: 60.0)
    yield
    for state in (H._pending_approvals, H._ended_prompts, H._thread_agents):
        state.clear()
    H.set_owner_id("")
    H.set_allowed_users(set())


@pytest.fixture
def links() -> dict:
    """The channel links of the gateway's one session manager, which this app writes (a thread's
    conversation is linked to the thread before its turn runs) and PersonalClaw reads."""
    return {}


class _Sessions(FakeSessionManager):
    """This app's session manager, keeping the links it is given where PersonalClaw reads them."""

    def __init__(self, provider, links: dict):
        super().__init__(provider)
        self._links = links

    def set_channel_link(self, key, thread_ts, channel_id):
        self._links[key] = (thread_ts, channel_id)

    def get_channel_link(self, key):
        return self._links.get(key, (None, None))


@pytest.fixture
def dashboard(tmp_path, links):
    """PersonalClaw's dashboard state, as the gateway runs it: the chats it holds, the
    conversation log both it and this app write, and the channel links this app makes."""
    from personalclaw.dashboard.state import DashboardState
    from personalclaw.history import ConversationLog
    from personalclaw.inbox_providers import native_source

    sessions = MagicMock()
    sessions.get_channel_link.side_effect = lambda key: links.get(key, (None, None))
    state = DashboardState(
        sessions=sessions, start_time=0.0, conversation_log=ConversationLog(base_dir=tmp_path)
    )
    state.push_sessions_update = MagicMock()
    before = native_source.get_dashboard_state()
    native_source.set_dashboard_state(state)
    yield state
    native_source.set_dashboard_state(before)


@pytest_asyncio.fixture
async def chat_page(dashboard):
    """The dashboard's own routes for a chat: what it shows, and its Permission mode switch, as
    the signed-in owner uses them."""
    from personalclaw.dashboard.chat import api_chat_mode, api_chat_session_detail
    from personalclaw.dashboard.request_boundary import request_boundary_middleware

    @web.middleware
    async def _the_owner(request, handler):
        request["user"] = "owner"
        return await handler(request)

    app = web.Application(middlewares=[_the_owner, request_boundary_middleware()])
    app["state"] = dashboard
    app.router.add_get("/api/chat/sessions/{session}", api_chat_session_detail)
    app.router.add_post("/api/chat/mode", api_chat_mode)
    async with TestClient(TestServer(app)) as client:
        yield client


class _OwnersDm(MockSlackClient):
    """Slack as it is in the owner's DM with the app: each thread opens with the owner's message."""

    async def fetch_thread_replies(self, channel, thread_ts, limit=200):
        return [{"user": OWNER, "ts": thread_ts, "text": "tidy the notes folder"}]


@pytest.fixture
def slack(monkeypatch):
    slack = _OwnersDm()
    monkeypatch.setattr(interactions, "_orch", SimpleNamespace(slack=slack))
    return slack


class _AsksEachTurn:
    """A model runtime whose every turn asks about one call, runs it once it is approved, and
    says so; it hears how each call was answered."""

    def __init__(self) -> None:
        self.approved: list[str] = []
        self.rejected: list[str] = []
        self._turns = 0
        self._answered = asyncio.Event()

    async def stream(self, message, timeout=120.0):
        self._turns += 1
        self._answered.clear()
        yield LLMEvent(
            kind="permission_request",
            request_id=f"req-{self._turns}",
            title="write_file",
            options=[],
            tool_input='{"path": "notes/index.md"}',
        )
        await asyncio.wait_for(self._answered.wait(), timeout=5)
        yield LLMEvent(kind="text_chunk", text=f"Turn {self._turns} ran.")
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


def _prompts(slack) -> list[dict]:
    """Each approval prompt posted: a message with buttons and a tool approval on it."""
    return [
        a[1]
        for a in slack.actions
        if a[0] == "blocks"
        and any(b["type"] == "actions" for b in a[1]["blocks"])
        and "Tool approval requested" in str(a[1]["blocks"])
    ]


def _buttons(prompt: dict) -> list[str]:
    actions = next(b for b in prompt["blocks"] if b["type"] == "actions")
    return [e["action_id"] for e in actions["elements"]]


async def _posted(slack, count: int) -> dict:
    for _ in range(500):
        prompts = _prompts(slack)
        if len(prompts) >= count and f"{DM}:{prompts[count - 1]['ts']}" in H._pending_approvals:
            return prompts[count - 1]
        await asyncio.sleep(0.01)
    raise AssertionError(f"prompt {count} was never posted")


async def _press(prompt: dict, action_id: str, *, as_user: str = OWNER) -> None:
    """A press, as Slack sends it, on one of the prompt's buttons."""
    await interactions.dispatch(
        {
            "type": "block_actions",
            "user": {"id": as_user},
            "channel": {"id": DM},
            "message": {"ts": prompt["ts"], "thread_ts": THREAD, "blocks": prompt["blocks"]},
            "actions": [{"action_id": action_id, "value": "req"}],
            "response_url": "",
        }
    )


def _turn(slack, sessions, dashboard, text: str, *, first: bool = False):
    """One message of the owner's in the DM thread, run by this app."""
    return asyncio.ensure_future(
        H.handle_message(
            slack,
            sessions,
            DM,
            text,
            None if first else THREAD,
            THREAD if first else next(_LATER),
            OWNER,
            approval_mode="interactive",
            conversation_log=dashboard.conversation_log,
        )
    )


def _rows(sel, operation: str) -> list[dict]:
    return [
        c.kwargs
        for c in sel.return_value.log_api_access.call_args_list
        if c.kwargs.get("operation") == operation
    ]


@pytest.mark.asyncio
async def test_allow_for_this_chat_is_the_chats_trust_shown_and_switched_off_in_personalclaw(
    slack, dashboard, chat_page, links
):
    turn = _AsksEachTurn()
    sessions = _Sessions(turn, links)
    with patch("slack_runtime.handler.sel") as said, patch(
        "personalclaw.dashboard.approval_state.sel"
    ) as granted:
        first = _turn(slack, sessions, dashboard, "tidy the notes folder", first=True)
        prompt = await _posted(slack, 1)
        assert _buttons(prompt) == ["approve_tool", "pc_answer_trust", "reject_tool"]
        assert f"Allow for this chat: {THIS_CHAT}" in str(prompt["blocks"])

        await _press(prompt, "pc_answer_trust")
        await asyncio.wait_for(first, timeout=5)
        assert turn.approved == ["req-1"]

        # The chat PersonalClaw holds for the thread is trusted, and it holds the turn too.
        chat = dashboard._sessions[THREAD]
        assert chat._trust is True
        assert [m["content"] for m in chat.messages] == ["tidy the notes folder", "Turn 1 ran."]
        (row,) = _rows(granted, "tool_approval:trust")
        assert row["outcome"] == "approved" and row["resources"] == "req-1"
        assert row["metadata"] == {"decided_by": "channel:slack"}
        shown = await (await chat_page.get(f"/api/chat/sessions/{THREAD}")).json()
        assert shown["approval"] == "trust"

        # The thread's next call runs on that Trust, asking nobody.
        await asyncio.wait_for(_turn(slack, sessions, dashboard, "and the drafts"), timeout=5)
        assert turn.approved == ["req-1", "req-2"]
        assert len(_prompts(slack)) == 1, "a trusted thread's call was asked about"
        (ran,) = [
            c.kwargs
            for c in said.return_value.log_tool_invocation.call_args_list
            if c.kwargs.get("request_id") == "req-2"
        ]
        assert ran["outcome"] == "auto_approved"
        assert ran["metadata"] == {"reason": "trust", "decided_by": "trust"}

        # Switched to Normal in the chat, the thread's next call asks here again.
        with patch("personalclaw.dashboard.chat_handlers.sel") as switched:
            off = await chat_page.post("/api/chat/mode", json={"mode": "normal", "session": THREAD})
            assert off.status == 200
        assert _rows(switched, "mode_change:normal")[0]["resources"] == THREAD
        shown = await (await chat_page.get(f"/api/chat/sessions/{THREAD}")).json()
        assert shown["approval"] == "normal"

        third = _turn(slack, sessions, dashboard, "and the archive")
        prompt = await _posted(slack, 2)
        assert turn.approved == ["req-1", "req-2"], "a call ran after the Trust was switched off"
        await _press(prompt, "approve_tool")
        await asyncio.wait_for(third, timeout=5)
    assert turn.approved == ["req-1", "req-2", "req-3"]
    assert not hasattr(H, "_trusted_sessions"), "the app keeps a trust of its own"


@pytest.mark.asyncio
async def test_allow_once_trusts_nothing_and_a_late_allow_for_this_chat_changes_nothing(
    slack, dashboard, links
):
    turn = _AsksEachTurn()
    with patch("slack_runtime.handler.sel"):
        first = _turn(slack, _Sessions(turn, links), dashboard, "tidy", first=True)
        prompt = await _posted(slack, 1)
        await _press(prompt, "approve_tool")
        await asyncio.wait_for(first, timeout=5)
        await _press(prompt, "pc_answer_trust")

    assert THREAD not in dashboard._sessions or dashboard._sessions[THREAD]._trust is False
    told = [a[1]["text"] for a in slack.actions if a[0] == "ephemeral"]
    assert told[-1] == "Already approved. This press changes nothing."


@pytest.mark.asyncio
async def test_a_press_personalclaw_cannot_take_decides_nothing(
    slack, dashboard, links, monkeypatch
):
    """The prompt offered Allow for this chat, and by the press PersonalClaw can no longer hold
    the chat's Trust: the press decides nothing, says why in the log, and the prompt keeps
    waiting for an answer."""
    import personalclaw.guardrails.policy as policy

    turn = _AsksEachTurn()
    with patch("slack_runtime.handler.sel") as said:
        first = _turn(slack, _Sessions(turn, links), dashboard, "tidy", first=True)
        prompt = await _posted(slack, 1)
        with monkeypatch.context() as ceiling:
            ceiling.setattr(policy, "ceiling_permits_approval", lambda level: False)
            await _press(prompt, "pc_answer_trust")
        await asyncio.sleep(0.05)
        assert turn.approved == [] and not first.done()
        refused = [
            c.kwargs
            for c in said.return_value.log_api_access.call_args_list
            if c.kwargs.get("outcome") == "denied"
        ]
        assert refused[-1]["error"] == "answer_not_taken"
        await _press(prompt, "reject_tool")
        await asyncio.wait_for(first, timeout=5)
    assert turn.rejected == ["req-1"]
    assert THREAD not in dashboard._sessions or dashboard._sessions[THREAD]._trust is False


@pytest.mark.asyncio
async def test_a_prompt_in_a_channel_offers_no_standing_answer(slack, dashboard):
    turn = _AsksEachTurn()
    event = LLMEvent(kind="permission_request", request_id="req-c", title="write_file", options=[])
    with patch("slack_runtime.handler.sel"):
        wait = asyncio.ensure_future(
            H._request_approval(slack, turn, "C0TEAM", THREAD, event, THREAD, is_dm=False)
        )
        for _ in range(300):
            if H._pending_approvals:
                break
            await asyncio.sleep(0.01)
        (prompt,) = _prompts(slack)
        assert _buttons(prompt) == ["approve_tool", "reject_tool"]
        assert (
            await H.handle_interaction(
                "C0TEAM", prompt["ts"], "pc_answer_trust", user_id=OWNER, thread_ts=THREAD
            )
            is None
        )
        assert not wait.done(), "an answer the prompt never offered answered the approval"
        wait.cancel()
        with pytest.raises(asyncio.CancelledError):
            await wait
    assert THREAD not in dashboard._sessions


@pytest.mark.asyncio
async def test_with_no_chat_to_hold_it_a_dm_prompt_offers_no_standing_answer(slack):
    """A gateway with no dashboard has no chat in which a Trust could be seen or switched off."""
    from personalclaw.inbox_providers import native_source

    assert native_source.get_dashboard_state() is None
    turn = _AsksEachTurn()
    event = LLMEvent(kind="permission_request", request_id="req-d", title="write_file", options=[])
    with patch("slack_runtime.handler.sel"):
        wait = asyncio.ensure_future(
            H._request_approval(slack, turn, DM, THREAD, event, THREAD, is_dm=True)
        )
        prompt = await _posted(slack, 1)
        assert _buttons(prompt) == ["approve_tool", "reject_tool"]
        await _press(prompt, "approve_tool")
        assert await asyncio.wait_for(wait, timeout=5) == "approved"
