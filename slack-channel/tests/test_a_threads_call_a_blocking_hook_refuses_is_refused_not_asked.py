"""A thread's call the operator's blocking hook refuses is refused, never approved or asked about.

A thread this app runs itself asked PersonalClaw's deny-list about each call, then who approves it
without asking, and put the rest on its prompt. The operator's blocking hooks (a ``PreToolUse``
hook bound to the agent the thread runs as) were asked nowhere on that path: an operator's pattern
approved the call, or the owner's Allow ran it, and the hook that would have refused it never ran.

Now each call meets the hooks right after the deny-list, at the step PersonalClaw asks them on every
path (``personalclaw.sdk.channel.ask_pre_tool_hooks``): a hook that blocks the call refuses it, the
thread says why, and the security log records the refusal as the hook's. An ordinary call runs, or
is asked about, as before.

Driven where a thread's turn asks (``handle_message``), with this app's Slack double and a model
runtime that asks about one call, against PersonalClaw's hook store and a hook bound to the default
agent in the test's scratch home. The hook's action is a stand-in answering as its command would.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest
from slack_helpers import MockSlackClient
from test_a_threads_call_runs_unasked_only_when_personalclaw_says_so import (
    DM,
    OWNER,
    THREAD,
    _AsksOnce,
    _asked,
    _prompts,
)
from test_a_threads_call_runs_unasked_only_when_personalclaw_says_so import (  # noqa: F401
    _the_owners_dm,
    dashboard,
)
from test_slack_handler import FakeSessionManager

import slack_runtime.handler as H
from personalclaw.context import ContextBuilder
from personalclaw.hooks import live_hook_manager
from personalclaw.sdk.channel import config_dir

#: The default agent the thread runs as, which the hook is bound to.
AGENT = "keeper"


class _Command:
    """The hook's action, answering as its command would: ``exit 2`` refuses the call."""

    def __init__(self) -> None:
        self.answer = "exit 2"
        self.runs = 0

    async def execute(self, config, ctx, timeout=30):
        from personalclaw.action_providers.base import ActionResult

        self.runs += 1
        if self.answer == "exit 2":
            return ActionResult(success=False, exit_code=2, blocked=True, stderr="no writes today")
        return ActionResult(success=True, exit_code=0)


@pytest.fixture
def hook(monkeypatch) -> _Command:
    """A blocking hook the operator bound to the default agent, in this test's home."""
    import personalclaw.action_providers as action_providers
    from personalclaw.hooks import HOOK_EVENT_PRE_TOOL_USE, ScriptHook, ScriptHookStore

    command = _Command()
    monkeypatch.setattr(action_providers, "get_action_provider", lambda name: command)
    monkeypatch.setattr(H, "_cached_default_agent", None)
    store = ScriptHookStore()
    store.create(
        ScriptHook(
            id="no-writes",
            name="no-writes",
            event=HOOK_EVENT_PRE_TOOL_USE,
            provider="bash",
            provider_config={"command": "/nonexistent/pc-fixture-hook"},
            enabled=True,
            capabilities={"providers": ["bash"]},
        ).to_dict()
    )
    monkeypatch.setattr("personalclaw.hooks._global_script_hook_store", store)
    return command


def _settings(*patterns: str) -> None:
    """The default agent with the hook bound to it, and the owner's hook settings."""
    (config_dir() / "config.json").write_text(
        json.dumps(
            {
                "default_agent": AGENT,
                "agents": {AGENT: {"triggers": ["no-writes"]}},
                "hooks": {"auto_approve_tools": list(patterns)},
            }
        ),
        encoding="utf-8",
    )


def _turn(runtime: _AsksOnce, slack: MockSlackClient) -> "asyncio.Future[None]":
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


async def _refused(slack: MockSlackClient, runtime: _AsksOnce, turn) -> None:
    """The call was refused before anyone was asked, and the turn went on to its end."""
    for _ in range(500):
        if _prompts(slack):
            turn.cancel()
            raise AssertionError("a call the hook refuses was asked about")
        if runtime.approved:
            turn.cancel()
            raise AssertionError("a call the hook refuses was approved")
        if runtime.rejected:
            break
        await asyncio.sleep(0.01)
    else:
        turn.cancel()
        raise AssertionError("the call was neither refused nor asked about")
    await asyncio.wait_for(turn, timeout=5)
    assert runtime.rejected == ["req-1"] and runtime.approved == []


def _said(slack: MockSlackClient) -> str:
    """Everything the thread was sent or shown, in order."""
    return "\n".join(
        str(a[1].get("text") or "") + str(a[1].get("blocks") or "")
        for a in slack.actions
        if a[0] in ("post", "update", "blocks", "append_stream", "stop_stream")
    )


def _rows(said: MagicMock) -> list[dict]:
    return [
        c.kwargs
        for c in said.return_value.log_tool_invocation.call_args_list
        if c.kwargs.get("request_id") == "req-1"
    ]


@pytest.mark.asyncio
async def test_a_call_a_blocking_hook_refuses_is_refused_though_a_pattern_names_it(dashboard, hook):
    """🔴 Before: the operator's pattern approved the write, and the hook never ran."""
    _settings("write_file")
    slack = MockSlackClient()
    runtime = _AsksOnce()
    with patch("slack_runtime.handler.sel") as said:
        await _refused(slack, runtime, _turn(runtime, slack))
    assert hook.runs == 1
    assert (
        "🚫 _Tool `write_file` not run: a pre-tool hook blocked it (no-writes:no writes today)._"
        in _said(slack)
    )
    [row] = _rows(said)
    assert row["outcome"] == "hook_blocked" and row["metadata"]["decided_by"] == "hook"


@pytest.mark.asyncio
async def test_a_call_a_blocking_hook_refuses_is_never_put_to_the_owner(dashboard, hook):
    """🔴 Before: nothing approved the call, so it was put on the thread's prompt."""
    _settings()
    slack = MockSlackClient()
    runtime = _AsksOnce()
    await _refused(slack, runtime, _turn(runtime, slack))
    assert hook.runs == 1


@pytest.mark.asyncio
async def test_an_ordinary_call_runs_or_is_asked_about_as_before(dashboard, hook):
    """The control: with the hook letting the call go on, the pattern still runs it unasked, and
    with no pattern the owner is asked and her Allow runs it. The hook is asked once a call."""
    hook.answer = "exit 0"
    _settings("write_file")
    slack = MockSlackClient()
    runtime = _AsksOnce()
    with patch("slack_runtime.handler.sel") as said:
        await asyncio.wait_for(_turn(runtime, slack), timeout=5)
    assert runtime.approved == ["req-1"] and _prompts(slack) == []
    assert [r["outcome"] for r in _rows(said)] == ["auto_approved"]
    assert hook.runs == 1

    _settings()
    slack = MockSlackClient()
    runtime = _AsksOnce()
    turn = _turn(runtime, slack)
    prompt = await _asked(slack, runtime)
    await H.handle_interaction(DM, prompt["ts"], "approve_tool", user_id=OWNER)
    await asyncio.wait_for(turn, timeout=5)
    assert runtime.approved == ["req-1"] and runtime.rejected == []
    assert hook.runs == 2
