"""A thread's call the deny-list refuses is refused, never asked about.

A thread this app runs itself read PersonalClaw's deny-list on the call's title alone. An agent
CLI's title need not carry the command it asks to run (it can be ``unknown``, a description, a
bare tool name), so a call the deny-list refuses for the command in its input reached the next
step, which approves nothing it refuses either, and was put on the thread's prompt as a question.

Now each call is first put to the screen PersonalClaw's own chat asks
(``personalclaw.sdk.channel.screen_tool_call``), read on the command the call would run as well as
on its title, and a call it refuses is refused: the thread says it was blocked, and the security
log records the refusal with the rule that made it. An ordinary call is asked about, or runs on
PersonalClaw's answer, as before.

Driven where a thread's turn asks (``handle_message``), with this app's Slack double and a model
runtime that asks about one call, against PersonalClaw's hook chain and a Settings → Security →
Shell denylist pattern added in the test's scratch home. The pattern names a program that cannot
resolve (``pcfixture-cloudctl``).
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

ADDED = "pcfixture-cloudctl"
DENIED = f"{ADDED} status"
#: How the shell denylist names an added pattern's rule.
RULE = "a pattern added to the shell denylist under Settings → Security"


def _add_pattern(**sections) -> None:
    """Add the pattern under Settings → Security → Shell denylist, in this test's home."""
    doc = {"security": {"denied_commands": [ADDED]}, **sections}
    (config_dir() / "config.json").write_text(json.dumps(doc), encoding="utf-8")


def _turn(runtime: _AsksOnce, slack: MockSlackClient) -> "asyncio.Future[None]":
    return asyncio.ensure_future(
        H.handle_message(
            slack,
            FakeSessionManager(runtime),
            DM,
            "check the deployment",
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
            raise AssertionError("a call the deny-list refuses was asked about")
        if runtime.approved:
            turn.cancel()
            raise AssertionError("a call the deny-list refuses was approved")
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
@pytest.mark.parametrize(
    "title, command",
    [
        ("unknown", DENIED),
        ("Check the service status", DENIED),
        ("Run command", [ADDED, "status"]),
    ],
)
async def test_a_command_the_deny_list_refuses_is_refused_and_never_asked(
    dashboard, title, command
):
    """Whatever the title says, and whether the command is text or a list of words."""
    _add_pattern()
    slack = MockSlackClient()
    runtime = _AsksOnce(
        title=title, tool_kind="execute", tool_input=json.dumps({"command": command})
    )
    with patch("slack_runtime.handler.sel") as said:
        await _refused(slack, runtime, _turn(runtime, slack))
    assert f"🚫 _Tool `{title}` blocked by hooks._" in _said(slack)
    [row] = _rows(said)
    assert row["outcome"] == "refused" and row["error"] == "hook_deny"
    assert row["metadata"]["decided_by"] == "shell_denylist"
    assert row["metadata"]["rule"] == ADDED


@pytest.mark.asyncio
async def test_a_refused_call_is_refused_whatever_would_have_approved_it(
    dashboard, monkeypatch
):
    """YOLO on and a hook pattern naming the call: neither reaches a call the deny-list refuses."""
    from personalclaw.sdk.channel import trust_mode

    _add_pattern(hooks={"auto_approve_tools": ["unknown"]})
    monkeypatch.setattr(trust_mode, "is_yolo_active", lambda: True)
    slack = MockSlackClient()
    runtime = _AsksOnce(
        title="unknown", tool_kind="execute", tool_input=json.dumps({"command": DENIED})
    )
    await _refused(slack, runtime, _turn(runtime, slack))


@pytest.mark.asyncio
async def test_an_ordinary_command_is_still_asked_about(dashboard):
    """The control: the same call with an ordinary command is put on the thread's prompt, and
    your Allow runs it."""
    _add_pattern()
    slack = MockSlackClient()
    runtime = _AsksOnce(
        title="unknown", tool_kind="execute", tool_input=json.dumps({"command": "echo hello"})
    )
    turn = _turn(runtime, slack)
    prompt = await _asked(slack, runtime)
    await H.handle_interaction(DM, prompt["ts"], "approve_tool", user_id=OWNER)
    await asyncio.wait_for(turn, timeout=5)
    assert runtime.approved == ["req-1"] and runtime.rejected == []


@pytest.mark.asyncio
async def test_an_ordinary_command_personalclaw_approves_still_runs_unasked(dashboard):
    """The control: a hook pattern naming an ordinary call still runs it without asking."""
    _add_pattern(hooks={"auto_approve_tools": ["write_file"]})
    slack = MockSlackClient()
    runtime = _AsksOnce()
    with patch("slack_runtime.handler.sel") as said:
        await asyncio.wait_for(_turn(runtime, slack), timeout=5)
    assert runtime.approved == ["req-1"] and _prompts(slack) == []
    assert [r["outcome"] for r in _rows(said)] == ["auto_approved"]
