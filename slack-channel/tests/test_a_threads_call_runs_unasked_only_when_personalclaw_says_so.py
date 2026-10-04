"""A thread's call runs without asking only when PersonalClaw says so.

A thread this app runs itself answered three kinds of call before asking PersonalClaw: one an
operator's pattern in the hook settings named, one called ``subagent_run`` while the spawn setting
was on, and every call of a turn run with the ``auto`` approval mode, which was what a turn got
unless its caller said otherwise. None of the three was held to the operator ceiling or to the
allowed hosts, so under a ceiling saying every call asks a person, or for a command reaching a host
off the allowed hosts, the call ran without anyone being asked.

Now each call asks PersonalClaw who approves it without asking (``chat_grant``), the decision
PersonalClaw's own chat makes, with both rules in it, and a call nobody approves is asked here, on
the thread's prompt.

Driven where a thread's turn asks (``handle_message``), with this app's Slack double and a model
runtime that asks about one call, against PersonalClaw's dashboard state, its hook settings, its
allowed hosts and an operator ceiling, in the test's scratch home.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from slack_helpers import MockSlackClient, set_owner
from test_slack_handler import FakeSessionManager

import slack_runtime.handler as H
from personalclaw.context import ContextBuilder
from personalclaw.guardrails import ceiling as C
from personalclaw.hooks import live_hook_manager
from personalclaw.llm.base import LLMEvent
from personalclaw.sdk.channel import config_dir

OWNER = "U0OWNER"
DM = "D0OWNER"
THREAD = "1700000300.000100"


@pytest.fixture(autouse=True)
def _the_owners_dm(monkeypatch):
    set_owner(OWNER)
    H.set_allowed_users({OWNER})
    for kept in (H._pending_approvals, H._ended_prompts, H._thread_agents):
        kept.clear()
    monkeypatch.setattr(H, "approval_window_secs", lambda: 60.0)
    C.reset_ceiling()
    yield
    C.reset_ceiling()
    for kept in (H._pending_approvals, H._ended_prompts, H._thread_agents):
        kept.clear()
    set_owner("")
    H.set_allowed_users(set())


@pytest.fixture
def dashboard(tmp_path):
    """PersonalClaw's dashboard state, as the gateway runs it."""
    from personalclaw.dashboard.state import DashboardState
    from personalclaw.history import ConversationLog
    from personalclaw.inbox_providers import native_source

    sessions = MagicMock()
    sessions.get_channel_link.return_value = (None, None)
    state = DashboardState(
        sessions=sessions, start_time=0.0, conversation_log=ConversationLog(base_dir=tmp_path)
    )
    state.push_sessions_update = MagicMock()
    before = native_source.get_dashboard_state()
    native_source.set_dashboard_state(state)
    yield state
    native_source.set_dashboard_state(before)


def _settings(*, patterns: tuple[str, ...] = (), spawns: bool = False) -> None:
    """The owner's hook settings, as Settings saves them, in this test's home."""
    hooks: dict[str, Any] = {"auto_approve_tools": list(patterns)}
    if spawns:
        hooks["auto_approve_subagent_spawn"] = True
    (config_dir() / "config.json").write_text(json.dumps({"hooks": hooks}))


def _ceiling(monkeypatch, tmp_path, value: str) -> None:
    """An operator ceiling, as the operator writes it."""
    path = tmp_path / "operator" / "ceiling.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "scopes": {"approval": {"value": value}}}))
    monkeypatch.setenv(C.CEILING_PATH_ENV, str(path))
    C.reset_ceiling()


class _AsksOnce:
    """A model runtime whose turn asks about one call, runs it once approved, and says so."""

    def __init__(self, **call: Any) -> None:
        self._call = {"title": "write_file", "tool_input": '{"path": "notes/index.md"}', **call}
        self.approved: list[str] = []
        self.rejected: list[str] = []
        self._answered = asyncio.Event()

    async def stream(self, message, timeout=120.0):
        yield LLMEvent(kind="permission_request", request_id="req-1", options=[], **self._call)
        await asyncio.wait_for(self._answered.wait(), timeout=5)
        yield LLMEvent(kind="text_chunk", text="Done." if self.approved else "Not run.")
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


def _turn(runtime: _AsksOnce, slack: MockSlackClient) -> "asyncio.Future[None]":
    """The owner's message opening a DM thread, run by this app, as its events hand it over."""
    return asyncio.ensure_future(
        H.handle_message(
            slack,
            FakeSessionManager(runtime),
            DM,
            "tidy the notes folder",
            None,
            THREAD,
            OWNER,
            context_builder=ContextBuilder(hooks=live_hook_manager()),
        )
    )


def _prompts(slack: MockSlackClient) -> list[dict]:
    return [
        a[1]
        for a in slack.actions
        if a[0] == "blocks" and "Tool approval requested" in str(a[1]["blocks"])
    ]


async def _asked(slack: MockSlackClient, runtime: _AsksOnce) -> dict:
    """The thread's prompt for the call, once posted, with the call still waiting on it."""
    for _ in range(500):
        prompts = _prompts(slack)
        if prompts and f"{DM}:{prompts[0]['ts']}" in H._pending_approvals:
            assert runtime.approved == [], "the call ran before anyone answered"
            return prompts[0]
        if runtime.approved:
            raise AssertionError("the call ran without asking anyone")
        await asyncio.sleep(0.01)
    raise AssertionError("the thread never asked about the call")


async def _ran_unasked(slack: MockSlackClient, runtime: _AsksOnce, turn) -> None:
    await asyncio.wait_for(turn, timeout=5)
    assert runtime.approved == ["req-1"]
    assert _prompts(slack) == [], "a call PersonalClaw approves was asked about"


async def _answer(prompt: dict, turn) -> None:
    await H.handle_interaction(DM, prompt["ts"], "approve_tool", user_id=OWNER)
    await asyncio.wait_for(turn, timeout=5)


def _decided(said: MagicMock) -> list[tuple[str, Any]]:
    """How the thread's audit rows say the call was decided: (outcome, who decided)."""
    return [
        (c.kwargs.get("outcome"), (c.kwargs.get("metadata") or {}).get("decided_by"))
        for c in said.return_value.log_tool_invocation.call_args_list
        if c.kwargs.get("request_id") == "req-1"
    ]


def _refused(audit: MagicMock) -> list[str]:
    """The grants PersonalClaw's ``approval.grant_refused`` rows name."""
    return [
        str(c.kwargs.get("resources", "")).split(",")[0].removeprefix("grant=")
        for c in audit.return_value.log_api_access.call_args_list
        if c.kwargs.get("operation") == "approval.grant_refused"
    ]


# ── An operator's pattern ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_under_an_ask_ceiling_a_pattern_naming_the_call_asks_you_instead(
    dashboard, monkeypatch, tmp_path
):
    _settings(patterns=("write_file",))
    _ceiling(monkeypatch, tmp_path, "ask")
    slack, runtime = MockSlackClient(), _AsksOnce()
    with patch("personalclaw.sel.sel") as audit:
        turn = _turn(runtime, slack)
        prompt = await _asked(slack, runtime)
        await _answer(prompt, turn)
    assert runtime.approved == ["req-1"], "your Allow runs it"
    assert "hook_pattern" in _refused(audit), "the refused pattern is audited, naming it"


@pytest.mark.asyncio
async def test_a_pattern_naming_a_command_to_a_host_off_the_allowed_hosts_asks_you(dashboard):
    _settings(patterns=("bash",))
    slack = MockSlackClient()
    runtime = _AsksOnce(
        title="bash", tool_input=json.dumps({"command": "curl https://example.com"})
    )
    turn = _turn(runtime, slack)
    prompt = await _asked(slack, runtime)
    assert "example.com" in str(prompt["blocks"]), "the prompt names the host"
    await _answer(prompt, turn)


@pytest.mark.asyncio
async def test_with_no_ceiling_a_pattern_still_runs_the_call_it_names_unasked(dashboard):
    """The control: the pattern still answers what it answered, recorded as approved unasked."""
    _settings(patterns=("write_file",))
    slack, runtime = MockSlackClient(), _AsksOnce()
    with patch("slack_runtime.handler.sel") as said:
        await _ran_unasked(slack, runtime, _turn(runtime, slack))
    assert [outcome for outcome, _ in _decided(said)] == ["auto_approved"]


# ── The spawn setting ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_under_an_ask_ceiling_the_spawn_setting_runs_no_call_on_its_own(
    dashboard, monkeypatch, tmp_path
):
    _settings(spawns=True)
    _ceiling(monkeypatch, tmp_path, "ask")
    slack, runtime = MockSlackClient(), _AsksOnce(title="subagent_run", tool_input="{}")
    turn = _turn(runtime, slack)
    await _answer(await _asked(slack, runtime), turn)


@pytest.mark.asyncio
async def test_the_call_that_starts_a_subagent_is_answered_as_personalclaws_chat_answers_it(
    dashboard,
):
    """What it starts asks you itself, or starts on the spawn setting where the start is decided,
    under the ceiling: so the call asks nobody, here as in PersonalClaw's chat."""
    _settings(spawns=True)
    slack = MockSlackClient()
    runtime = _AsksOnce(
        title="mcp__personalclaw-core__subagent_run",
        tool_kind="other",
        tool_input='{"task": "summarise the notes"}',
        work_asks=True,
    )
    with patch("slack_runtime.handler.sel") as said:
        await _ran_unasked(slack, runtime, _turn(runtime, slack))
    assert ("auto_approved", "work_asks") in _decided(said)


# ── A turn's own approval ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_turn_approves_no_call_on_its_own(dashboard, monkeypatch, tmp_path):
    """No caller passes anything that approves its turn's calls: with nothing standing, the call
    is asked, and so it is under an ask ceiling."""
    slack, runtime = MockSlackClient(), _AsksOnce()
    turn = _turn(runtime, slack)
    await _answer(await _asked(slack, runtime), turn)

    H._pending_approvals.clear()
    _ceiling(monkeypatch, tmp_path, "ask")
    slack, runtime = MockSlackClient(), _AsksOnce()
    turn = _turn(runtime, slack)
    await _answer(await _asked(slack, runtime), turn)
