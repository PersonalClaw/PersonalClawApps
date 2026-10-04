"""A turn this app runs for a thread names whose message it answers, while it runs.

A thread in a shared channel has the owner and the colleagues she allowed in it. Memory already
takes no colleague's words as hers, but the agent's memory tools wrote whatever a colleague's turn
asked for: PersonalClaw could not tell, while the tools ran, that the turn was someone else's,
because this app runs the thread's turns itself. Each turn now says whose message it answers
(``turn_asked_by``), so a change to the owner's memory that turn's tools ask for waits for her own
word, and her own turn changes it as before. So does a change the agent's own tools would make to
one of her memory documents: PersonalClaw's gate, which this app asks before any call runs, refuses
it in a colleague's turn whatever would approve it.

The turn is driven through the app's handler; PersonalClaw's reading of who asked is real.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from slack_helpers import MockSlackClient
from test_a_threads_call_runs_unasked_only_when_personalclaw_says_so import dashboard  # noqa: F401
from test_slack_handler import FakeProvider, FakeSessionManager

import slack_runtime.handler as H
from personalclaw.config.credentials import owner_id_credential
from personalclaw.config.loader import CRED_OWNER_ID
from personalclaw.llm.base import LLMEvent

OWNER = "U0MIRAOWNR"
COLLEAGUE = "U0JONASCOL"
CHANNEL = "C0EXAMPLE02"
THREAD = "1712793600.000500"


@pytest.fixture(autouse=True)
def _a_shared_channel(monkeypatch):
    """Slack knows its owner, and the owner allowed a colleague to talk to the agent."""
    monkeypatch.delenv(CRED_OWNER_ID, raising=False)
    monkeypatch.setenv(owner_id_credential("slack"), OWNER)
    H.set_owner_id(OWNER)
    H.set_allowed_users({OWNER, COLLEAGUE})
    yield
    H.set_owner_id("")
    H.set_allowed_users(set())


class _Asking(FakeProvider):
    """An agent that reads, as its tool calls for the thread would, who asked for its turn."""

    def __init__(self) -> None:
        super().__init__()
        self.asked: list[dict] = []

    async def stream(self, message, timeout=120.0):
        from personalclaw import mcp_core, memory_writes

        token = mcp_core.set_current_session_key(THREAD)
        try:
            self.asked.append(memory_writes.asker())
        finally:
            mcp_core.reset_current_session_key(token)
        async for event in super().stream(message, timeout):
            yield event


@pytest.mark.asyncio
async def test_a_colleagues_turn_is_named_as_theirs_and_the_owners_as_hers():
    """🔴 Red before: the colleague's turn read as nobody's in particular, so the owner's memory
    took what that turn's tools asked for as though she had."""
    from personalclaw import session_restrictions
    from personalclaw.sdk.channel import arrived_on

    agent = _Asking()
    slack = MockSlackClient()
    sessions = FakeSessionManager(agent)
    for n, sender in enumerate((COLLEAGUE, OWNER)):
        await H.handle_message(
            slack, sessions, CHANNEL, "remember this, please", THREAD, f"m{n}", sender
        )

    assert agent.asked == [arrived_on(THREAD, COLLEAGUE, "slack"), {}]
    assert session_restrictions.asked_by(THREAD) is None, "the mark ends with the turn"


class _AsksToWrite(FakeProvider):
    """An agent whose own tool asks to write the owner's preferences.md, and waits for the answer."""

    def __init__(self, path: str) -> None:
        super().__init__()
        self._path = path
        self._answered = asyncio.Event()

    async def stream(self, message, timeout=120.0):
        yield LLMEvent(
            kind="permission_request",
            request_id="req-1",
            options=[],
            title=f"Write {self._path}",
            tool_kind="edit",
            tool_input=json.dumps({"file_path": self._path, "content": "- Mira is vegetarian.\n"}),
        )
        await asyncio.wait_for(self._answered.wait(), timeout=5)
        yield LLMEvent(kind="text_chunk", text="Done." if self.approved else "Not written.")
        yield LLMEvent(kind="complete")

    async def approve_tool(self, request_id, option_id="allow_once"):
        await super().approve_tool(request_id, option_id)
        self._answered.set()

    async def reject_tool(self, request_id):
        await super().reject_tool(request_id)
        self._answered.set()


@pytest.mark.asyncio
async def test_a_colleagues_turn_changes_no_memory_document_through_the_agents_own_tools(dashboard):
    """🔴 Red before: an operator's pattern ran the agent's own write to her preferences.md in her
    colleague's turn without asking anyone. PersonalClaw's gate, which this app asks first, now
    refuses it, nobody asked; her own turn runs it as before."""
    from personalclaw import memory
    from personalclaw.context import ContextBuilder
    from personalclaw.hooks import live_hook_manager
    from personalclaw.sdk.channel import config_dir

    (config_dir() / "config.json").write_text(
        json.dumps({"hooks": {"auto_approve_tools": ["Write*"]}})
    )
    document = str(memory.memory_folders()[0] / "preferences.md")
    answered = {}
    for n, sender in enumerate((COLLEAGUE, OWNER)):
        agent, slack = _AsksToWrite(document), MockSlackClient()
        await asyncio.wait_for(
            H.handle_message(
                slack,
                FakeSessionManager(agent),
                CHANNEL,
                "add that to her preferences",
                THREAD,
                f"w{n}",
                sender,
                context_builder=ContextBuilder(hooks=live_hook_manager()),
            ),
            timeout=10,
        )
        asked = [a for a in slack.actions if "Tool approval requested" in str(a)]
        answered[sender] = (agent.approved, agent.rejected, asked)

    assert answered[COLLEAGUE] == ([], ["req-1"], []), answered[COLLEAGUE]
    assert answered[OWNER] == (["req-1"], [], []), answered[OWNER]
