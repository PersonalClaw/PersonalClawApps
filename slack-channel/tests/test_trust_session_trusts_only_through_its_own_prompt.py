"""Trust session trusts a thread only through the prompt this app posted it on.

A prompt for a DM thread this app runs itself offers Trust session, which lets the rest of that
thread's tool calls run without asking. Pressed after the approval it came with had ended, it
trusted the thread for any press of a button with Trust session's action id, on any message of a
thread the owner had started: the press carried nothing this app had issued, so a button it never
posted, or one on a prompt from long ago, trusted the thread just the same.

Now the prompt's button carries a nonce this app mints as it posts the prompt, and the app keeps
it, with the thread the prompt is in and the session that thread's turns run in, for as long as
the approval waits. A press trusts that session only on that prompt, with that nonce, in that
thread, in that time, and pressed after the approval ended it still does. Any other press trusts
nothing and changes nothing; once nothing waits on its prompt it is told the prompt is no longer
valid, and the prompt loses its buttons.

Driven where Slack hands this app a press (``interactions.dispatch``), with this app's Slack
double, on prompts the app posts itself (``_request_approval``).
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from slack_helpers import MockSlackClient

import slack_runtime.handler as H
import slack_runtime.interactions as interactions
from personalclaw.llm.base import LLMEvent

OWNER = "U0OWNER"
DM = "D0OWNER"
THREAD = "1700000100.000100"
OTHER_THREAD = "1700000200.000200"
NO_LONGER_VALID = "This prompt is no longer valid. This press changes nothing."
TRUSTED_LINE = "🤝 Trusted: the rest of this thread's tool calls run without asking"


@pytest.fixture(autouse=True)
def _the_owners_dm(monkeypatch):
    H.set_owner_id(OWNER)
    H.set_allowed_users({OWNER})
    for state in (H._pending_approvals, H._ended_prompts, H._trusted_sessions):
        state.clear()
    # The offers this process keeps start empty in each test. ``raising=False`` so that, on an app
    # that kept none, these tests fail on what a press does rather than on a missing name.
    monkeypatch.setattr(H, "_trust_offers", OrderedDict(), raising=False)
    monkeypatch.setattr(H, "approval_window_secs", lambda: 60.0)
    yield
    for state in (H._pending_approvals, H._ended_prompts, H._trusted_sessions):
        state.clear()
    H.set_owner_id("")
    H.set_allowed_users(set())


class _OwnersDm(MockSlackClient):
    """Slack as it is in the owner's DM with the app: each thread opens with the owner's message."""

    async def fetch_thread_replies(self, channel, thread_ts, limit=200):
        return [{"user": OWNER, "ts": thread_ts, "text": "please tidy the build folder"}]


class _Turn:
    """The tool calls of the turn that asked, as the provider hears how each was answered."""

    def __init__(self) -> None:
        self.approved: list[str] = []
        self.rejected: list[str] = []

    async def approve_tool(self, request_id, option_id="allow_once"):
        self.approved.append(request_id)

    async def reject_tool(self, request_id):
        self.rejected.append(request_id)


class _Asked:
    """One approval this app asked in a DM thread it runs itself, and the prompt it posted."""

    def __init__(self, slack: _OwnersDm, turn: _Turn, thread: str, request_id: str) -> None:
        self.slack = slack
        self.thread = thread
        self.request_id = request_id
        self._before = len(slack.actions)
        event = LLMEvent(
            kind="permission_request",
            request_id=request_id,
            title="bash",
            options=[],
            tool_input='{"command": "rm -rf build"}',
        )
        self.wait = asyncio.ensure_future(
            H._request_approval(slack, turn, DM, thread, event, thread, is_dm=True)
        )

    async def posted(self) -> "_Asked":
        for _ in range(300):
            prompts = [
                a[1] for a in self.slack.actions[self._before:]
                if a[0] == "blocks" and any(b["type"] == "actions" for b in a[1]["blocks"])
            ]
            if prompts and f"{DM}:{prompts[0]['ts']}" in H._pending_approvals:
                self._prompt = prompts[0]
                return self
            await asyncio.sleep(0.01)
        raise AssertionError("the prompt was never posted")

    @property
    def message(self) -> dict:
        """The prompt's message, as Slack hands it back with a press on one of its buttons."""
        posted = self._prompt
        return {"ts": posted["ts"], "thread_ts": posted["thread_ts"], "blocks": posted["blocks"]}

    def button(self, action_id: str) -> dict:
        actions = next(b for b in self.message["blocks"] if b["type"] == "actions")
        return next(e for e in actions["elements"] if e["action_id"] == action_id)

    async def ended(self) -> str:
        return await asyncio.wait_for(self.wait, timeout=5)


async def _press(
    slack: _OwnersDm, message: dict, action_id: str, value: str, *, thread: str | None = None
) -> None:
    """The owner's press, as Slack sends it: on *message*, its button's *value* with it."""
    payload = {
        "type": "block_actions",
        "user": {"id": OWNER},
        "channel": {"id": DM},
        "message": {**message, "thread_ts": thread or message["thread_ts"]},
        "actions": [{"action_id": action_id, "value": value}],
        "response_url": "",
    }
    await interactions.dispatch(payload)


def _told(slack: _OwnersDm) -> list[str]:
    return [a[1]["text"] for a in slack.actions if a[0] == "ephemeral"]


def _denials(sel) -> list[dict]:
    return [
        c.kwargs for c in sel.return_value.log_api_access.call_args_list
        if c.kwargs.get("outcome") == "denied"
    ]


@pytest.fixture
def wired(monkeypatch):
    slack = _OwnersDm()
    monkeypatch.setattr(interactions, "_orch", SimpleNamespace(slack=slack))
    return slack, _Turn()


@pytest.mark.asyncio
async def test_trust_pressed_on_its_prompt_after_the_approval_ended_trusts_that_thread(wired):
    """The floor: Allow once pressed, then Trust session on the same prompt (a client that still
    shows it), and the rest of the thread runs without asking."""
    slack, turn = wired
    with patch("slack_runtime.handler.sel") as sel:
        asked = await _Asked(slack, turn, THREAD, "req-1").posted()
        prompt = asked.message
        await _press(slack, prompt, "approve_tool", asked.button("approve_tool")["value"])
        assert await asked.ended() == "approved"

        await _press(slack, prompt, "trust_tool", asked.button("trust_tool")["value"])

    assert H._trusted_sessions == {THREAD}
    assert NO_LONGER_VALID not in _told(slack)
    (closed,) = [a[1] for a in slack.actions if a[0] == "update" and a[1]["ts"] == prompt["ts"]]
    assert closed["text"] == TRUSTED_LINE
    assert not [b for b in closed["blocks"] if b["type"] == "actions"]
    (allowed,) = [
        c.kwargs for c in sel.return_value.log_api_access.call_args_list
        if c.kwargs.get("operation") == "slack.interactive.trust_late"
    ]
    assert (allowed["outcome"], allowed["resources"]) == ("allowed", THREAD)


@pytest.mark.asyncio
async def test_trust_pressed_while_the_approval_waits_approves_and_trusts_that_thread(wired):
    slack, turn = wired
    with patch("slack_runtime.handler.sel"):
        asked = await _Asked(slack, turn, THREAD, "req-1").posted()
        await _press(slack, asked.message, "trust_tool", asked.button("trust_tool")["value"])
        assert await asked.ended() == "approved"

    assert turn.approved == ["req-1"]
    assert H._trusted_sessions == {THREAD}


@pytest.mark.asyncio
async def test_each_prompt_carries_a_trust_of_its_own(wired):
    """What a Trust session button carries is minted for its prompt: not anything the prompt
    shows, and never the same on two prompts."""
    slack, turn = wired
    with patch("slack_runtime.handler.sel"):
        first = await _Asked(slack, turn, THREAD, "req-1").posted()
        second = await _Asked(slack, turn, OTHER_THREAD, "req-2").posted()
        values = [a.button("trust_tool")["value"] for a in (first, second)]
        for asked in (first, second):
            await _press(slack, asked.message, "reject_tool", asked.button("reject_tool")["value"])
            assert await asked.ended() == "rejected"

    assert values[0] != values[1]
    assert not {"req-1", "req-2"} & set(values)
    assert all(len(v) >= 16 for v in values)


async def _ended_prompt(slack: _OwnersDm, turn: _Turn, thread: str, request_id: str) -> _Asked:
    """A prompt whose approval the owner answered (Allow once), as a client still shows it."""
    asked = await _Asked(slack, turn, thread, request_id).posted()
    await _press(slack, asked.message, "approve_tool", asked.button("approve_tool")["value"])
    assert await asked.ended() == "approved"
    return asked


@pytest.mark.asyncio
async def test_trust_on_a_message_this_app_posted_no_prompt_on_trusts_nothing(wired):
    """A Trust session button on any other message of the thread: one this process never posted
    a prompt on, such as a prompt left from before a restart."""
    slack, turn = wired
    with patch("slack_runtime.handler.sel") as sel:
        asked = await _ended_prompt(slack, turn, THREAD, "req-1")
        elsewhere = {**asked.message, "ts": "1700000150.000150"}
        await _press(slack, elsewhere, "trust_tool", asked.button("trust_tool")["value"])

    assert H._trusted_sessions == set()
    assert _told(slack)[-1] == NO_LONGER_VALID
    assert _denials(sel)[-1]["error"] == "no_trust_offer"
    (closed,) = [a[1] for a in slack.actions if a[0] == "update" and a[1]["ts"] == elsewhere["ts"]]
    assert not [b for b in closed["blocks"] if b["type"] == "actions"], "it kept its buttons"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["a nonce this app did not mint", "another thread's prompt", "a press from another thread"],
)
async def test_trust_naming_anything_but_its_own_prompt_trusts_nothing(wired, case):
    slack, turn = wired
    with patch("slack_runtime.handler.sel") as sel:
        mine = await _ended_prompt(slack, turn, THREAD, "req-1")
        theirs = await _ended_prompt(slack, turn, OTHER_THREAD, "req-2")
        nonce = mine.button("trust_tool")["value"]
        if case == "a nonce this app did not mint":
            await _press(slack, mine.message, "trust_tool", "x" * len(nonce))
        elif case == "another thread's prompt":
            await _press(slack, theirs.message, "trust_tool", nonce)
        else:
            await _press(slack, mine.message, "trust_tool", nonce, thread=OTHER_THREAD)

    assert H._trusted_sessions == set()
    assert _told(slack)[-1] == NO_LONGER_VALID
    denied = _denials(sel)[-1]
    assert (denied["operation"], denied["error"]) == (
        "slack.interactive.trust_late",
        "not_this_trust_offer",
    )


@pytest.mark.asyncio
async def test_trust_past_its_approvals_wait_trusts_nothing(wired, monkeypatch):
    """The offer lasts as long as the approval it came with could wait: nobody answered in that
    time, so the call did not run, and the thread is not trusted by a press after it."""
    slack, turn = wired
    monkeypatch.setattr(H, "approval_window_secs", lambda: 0.05)
    with patch("slack_runtime.handler.sel") as sel:
        asked = await _Asked(slack, turn, THREAD, "req-1").posted()
        prompt, nonce = asked.message, asked.button("trust_tool")["value"]
        assert await asked.ended() == "expired"
        await _press(slack, prompt, "trust_tool", nonce)

    assert H._trusted_sessions == set()
    assert turn.approved == []
    assert _told(slack)[-1] == NO_LONGER_VALID
    assert _denials(sel)[-1]["error"] == "trust_offer_expired"


@pytest.mark.asyncio
async def test_trust_with_another_nonce_on_a_waiting_prompt_decides_nothing(wired):
    """The approval still waits for the owner's answer, and its own buttons still give it."""
    slack, turn = wired
    with patch("slack_runtime.handler.sel"):
        asked = await _Asked(slack, turn, THREAD, "req-1").posted()
        nonce = asked.button("trust_tool")["value"]
        await _press(slack, asked.message, "trust_tool", nonce[::-1])
        await asyncio.sleep(0)
        assert not asked.wait.done(), "a press naming no offer answered the approval"
        assert turn.approved == [] and H._trusted_sessions == set()

        await _press(slack, asked.message, "trust_tool", nonce)
        assert await asked.ended() == "approved"

    assert turn.approved == ["req-1"]
    assert H._trusted_sessions == {THREAD}


@pytest.mark.asyncio
async def test_a_prompt_in_a_channel_offers_no_trust_session(wired):
    slack, turn = wired
    event = LLMEvent(kind="permission_request", request_id="req-c", title="bash", options=[])
    with patch("slack_runtime.handler.sel"):
        wait = asyncio.ensure_future(
            H._request_approval(slack, turn, "C0TEAM", THREAD, event, THREAD, is_dm=False)
        )
        for _ in range(300):
            if H._pending_approvals:
                break
            await asyncio.sleep(0.01)
        prompt = [a[1] for a in slack.actions if a[0] == "blocks"][-1]
        actions = next(b for b in prompt["blocks"] if b["type"] == "actions")
        assert [e["action_id"] for e in actions["elements"]] == ["approve_tool", "reject_tool"]
        assert await H.handle_interaction(
            "C0TEAM", prompt["ts"], "trust_tool", user_id=OWNER, thread_ts=THREAD
        ) is None
        assert not wait.done(), "a Trust the prompt never offered answered the approval"
        wait.cancel()
        with pytest.raises(asyncio.CancelledError):
            await wait
        for _ in range(100):  # the cancelled prompt is closed on its own, off the wait
            if any(a[0] == "update" for a in slack.actions):
                break
            await asyncio.sleep(0.01)
    assert turn.approved == [] and H._trusted_sessions == set()
