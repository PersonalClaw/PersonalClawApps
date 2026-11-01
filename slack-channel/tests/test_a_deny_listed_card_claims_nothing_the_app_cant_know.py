"""A thread's card for a call the deny-list names claims nothing the app can't know.

Some agent CLIs run certain calls under their own policy without asking PersonalClaw. The app read
each tool-call card's title against the deny-list, and on a match it wrote "blocked" in the thread
and a ``denied`` row in the security log, and hid the card. Hiding a card can't stop its call, so
the call still ran while the thread and the log said it was stopped. From a card alone the app
can't tell whether the call will ask or already ran, so the thread now says just that, and its row
says the call was seen on the deny-list, with whether it ran unknown. A call's permission request
is still where a call is refused (the deny-list test beside this one).

Driven where a thread's turn reads its runtime's events (``handle_message``), against
PersonalClaw's hook chain and a shell denylist pattern added in the test's scratch home.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from slack_helpers import MockSlackClient
from test_a_threads_call_runs_unasked_only_when_personalclaw_says_so import DM, OWNER, THREAD
from test_a_threads_call_runs_unasked_only_when_personalclaw_says_so import (  # noqa: F401
    _the_owners_dm,
    dashboard,
)
from test_a_threads_call_the_deny_list_refuses_is_refused_not_asked import (
    DENIED,
    _add_pattern,
    _said,
)
from test_slack_handler import FakeProvider, FakeSessionManager

import slack_runtime.handler as H
from personalclaw.context import ContextBuilder
from personalclaw.hooks import live_hook_manager
from personalclaw.llm.base import LLMEvent

CARD = f"Running: {DENIED}"


async def _a_turn_with_a_card(title: str) -> tuple[MockSlackClient, FakeSessionManager, list]:
    runtime = FakeProvider([LLMEvent(kind="tool_call", title=title, tool_kind="execute")])
    sessions, slack = FakeSessionManager(runtime), MockSlackClient()
    with patch("slack_runtime.handler.sel") as said:
        await H.handle_message(
            slack,
            sessions,
            DM,
            "check the deployment",
            None,
            THREAD,
            OWNER,
            context_builder=ContextBuilder(hooks=live_hook_manager()),
        )
    rows = [c.kwargs for c in said.return_value.log_tool_invocation.call_args_list]
    return slack, sessions, [r for r in rows if r.get("tool_name") == title]


@pytest.mark.asyncio
async def test_a_deny_listed_card_is_not_called_blocked(dashboard):
    """🔴 Red before: "blocked by hooks" and a ``denied`` row, for a call that may have run."""
    _add_pattern()
    slack, _, rows = await _a_turn_with_a_card(CARD)

    said = _said(slack)
    assert "blocked" not in said
    assert (
        f"`{CARD}` is on the deny list. This agent runs some calls without asking first, so "
        "whether it ran isn't known here."
    ) in said
    [row] = rows
    assert row["outcome"] == "deny_listed" and row["error"] == "hook_deny"
    assert row["metadata"] == {"seen_on": "card", "ran": "unknown"}


@pytest.mark.asyncio
async def test_a_card_the_deny_list_doesnt_name_goes_on_as_before(dashboard):
    _add_pattern()
    _, _, rows = await _a_turn_with_a_card("Running: ls")

    assert [r["outcome"] for r in rows] == ["invoked"]
