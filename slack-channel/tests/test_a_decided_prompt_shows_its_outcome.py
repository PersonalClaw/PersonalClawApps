"""A Slack approval prompt says how its approval ended, and takes its buttons off.

When core asked the owner an approval on Slack and it ended — Approve or Reject pressed here, an
answer in PersonalClaw, nobody answering in time, the work that asked stopping first — the prompt
was edited with `chat.update` and a `text` alone. An update that sets only the text keeps the
blocks, so the message kept its buttons and never showed what happened; only the notification
fallback changed. And an approval that expired, or was cancelled, was told apart from a Deny by
nothing: both read "Rejected". Each prompt is now closed with its blocks replaced: what it asked,
then how it ended, and no buttons. A press that arrives after that is answered with how it ended.

Also here, because they are the same prompt: core's prompt offers no Trust button (the trust it
grants is this app's own, for threads core's approvals never belong to), its wait keeps no timer
of its own (the window is core's), and the owner a fresh install claims on first contact is the
one it prompts, before any restart.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from slack_helpers import MockSlackClient

import slack_runtime.handler as H
from personalclaw.llm.base import LLMEvent
from slack_runtime.delivery import SlackDelivery

OWNER = "U_OWNER"


@pytest.fixture(autouse=True)
def _an_owner():
    H.set_owner_id(OWNER)
    H.set_allowed_users({OWNER})
    H._pending_approvals.clear()
    H._ended_prompts.clear()
    yield
    H._pending_approvals.clear()
    H._ended_prompts.clear()
    H.set_owner_id("")
    H.set_allowed_users(set())


def _event() -> LLMEvent:
    return LLMEvent(
        kind="permission_request",
        request_id="req-1",
        title="bash",
        options=[],
        tool_input='{"command": "make test"}',
    )


class _Asked:
    """Core asking the owner on Slack: the wait, its prompt, and the record core resolves."""

    def __init__(self, slack: MockSlackClient, owner=lambda: OWNER) -> None:
        self.slack = slack
        self.pending: dict = {}
        self.wait = asyncio.ensure_future(
            SlackDelivery(slack, owner).request_approval(
                _event(), source="chat", on_prompted=lambda p: self.pending.setdefault("it", p)
            )
        )

    async def prompted(self) -> "_Asked":
        for _ in range(200):
            if self.pending:
                return self
            await asyncio.sleep(0.01)
        raise AssertionError("the prompt was never posted")

    @property
    def ts(self) -> str:
        return [a[1]["ts"] for a in self.slack.actions if a[0] == "blocks"][-1]

    @property
    def channel(self) -> str:
        return [a[1]["channel"] for a in self.slack.actions if a[0] == "blocks"][-1]

    def posted_buttons(self) -> list[str]:
        blocks = [a[1]["blocks"] for a in self.slack.actions if a[0] == "blocks"][-1]
        return [e["action_id"] for b in blocks if b["type"] == "actions" for e in b["elements"]]

    def closed_with(self) -> list[dict]:
        """The blocks the prompt's last message was replaced with."""
        updates = [a[1] for a in self.slack.actions if a[0] == "update" and a[1]["ts"] == self.ts]
        assert updates, "the prompt was never closed"
        assert updates[-1]["blocks"], "only the text was updated: the buttons stay"
        return updates[-1]["blocks"]


def _buttons(blocks: list[dict]) -> list[dict]:
    return [b for b in blocks if b["type"] == "actions"]


def _says(blocks: list[dict]) -> str:
    return " ".join(
        e.get("text", "") for b in blocks if b["type"] == "context" for e in b["elements"]
    )


@pytest.mark.asyncio
async def test_approve_pressed_here_closes_the_prompt_saying_approved():
    asked = await _Asked(MockSlackClient()).prompted()
    with patch("slack_runtime.handler.sel"):
        assert await H.handle_interaction(asked.channel, asked.ts, "approve_tool", user_id=OWNER)
    assert await asyncio.wait_for(asked.wait, timeout=2) is True

    blocks = asked.closed_with()
    assert _buttons(blocks) == [], "the decided prompt kept its buttons"
    assert "✅ Approved" in _says(blocks)
    assert any("make test" in str(b) for b in blocks), "the prompt no longer shows what it asked"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ended", "line"),
    [
        ("rejected", "🚫 Rejected"),
        ("expired", "⌛ Nobody answered in time, so it did not run"),
        ("cancelled", "⏹️ Cancelled: the work that asked for it stopped first, so it did not run"),
    ],
)
async def test_an_approval_that_ended_elsewhere_says_how(ended, line):
    """Core resolves the prompt's record with how the approval ended wherever it ended."""
    asked = await _Asked(MockSlackClient()).prompted()
    asked.pending["it"].future.set_result(ended)
    assert await asyncio.wait_for(asked.wait, timeout=2) is False

    blocks = asked.closed_with()
    assert _buttons(blocks) == []
    assert line in _says(blocks)


@pytest.mark.asyncio
async def test_a_wait_that_is_cancelled_closes_its_prompt():
    asked = await _Asked(MockSlackClient()).prompted()
    asked.wait.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asked.wait
    for _ in range(100):
        if any(a[0] == "update" for a in asked.slack.actions):
            break
        await asyncio.sleep(0.01)
    blocks = asked.closed_with()
    assert _buttons(blocks) == []
    assert "Cancelled" in _says(blocks)


@pytest.mark.asyncio
async def test_a_press_after_the_approval_ended_is_told_how_it_ended():
    slack = MockSlackClient()
    asked = await _Asked(slack).prompted()
    asked.pending["it"].future.set_result("expired")
    await asyncio.wait_for(asked.wait, timeout=2)

    with patch("slack_runtime.handler.sel"):
        got = await H.handle_interaction(
            asked.channel, asked.ts, "approve_tool", user_id=OWNER, slack=slack
        )
    assert got == H.LATE_PRESS
    told = [a[1]["text"] for a in slack.actions if a[0] == "ephemeral"]
    assert told == ["Nobody answered in time, so it did not run. This press changes nothing."]


@pytest.mark.asyncio
async def test_a_press_on_a_prompt_from_before_a_restart_closes_it(monkeypatch):
    """A prompt still showing buttons that this process never saw end: the owner is told it is
    no longer waiting, and the message loses its buttons."""
    import slack_runtime.interactions as interactions

    slack = MockSlackClient()
    monkeypatch.setattr(interactions, "_orch", SimpleNamespace(slack=slack))
    stale = H._approval_messages(_event())[-1]
    payload = {"message": {"ts": "9.000", "blocks": stale}, "response_url": ""}

    with patch("slack_runtime.handler.sel"):
        await interactions._handle_tool_approval(payload, "approve_tool", "D1", "9.000", OWNER)

    told = [a[1]["text"] for a in slack.actions if a[0] == "ephemeral"]
    assert told == ["This approval is no longer waiting. This press changes nothing."]
    (update,) = [a[1] for a in slack.actions if a[0] == "update"]
    assert _buttons(update["blocks"]) == []
    assert "no longer waiting" in _says(update["blocks"])


@pytest.mark.asyncio
async def test_a_colleague_s_late_press_changes_nothing(monkeypatch):
    """The floor of the case above: only the owner's press closes a prompt."""
    import slack_runtime.interactions as interactions

    slack = MockSlackClient()
    monkeypatch.setattr(interactions, "_orch", SimpleNamespace(slack=slack))
    H.set_allowed_users({OWNER, "U_COLLEAGUE"})
    stale = H._approval_messages(_event())[-1]
    payload = {"message": {"ts": "9.000", "blocks": stale}, "response_url": ""}

    with patch("slack_runtime.handler.sel"):
        await interactions._handle_tool_approval(payload, "approve_tool", "D1", "9.000", "U_COLLEAGUE")

    assert [a for a in slack.actions if a[0] == "update"] == []


@pytest.mark.asyncio
async def test_core_s_prompt_offers_no_trust_it_cannot_give():
    asked = await _Asked(MockSlackClient()).prompted()
    assert asked.posted_buttons() == ["approve_tool", "reject_tool"]
    asked.wait.cancel()


@pytest.mark.asyncio
async def test_the_wait_keeps_no_timer_of_its_own():
    """Core's window can be a week; a prompt that gave up on a timer of its own said the approval
    was rejected while PersonalClaw still waited on it. Any timer here gives up at once."""
    import slack_runtime.delivery as delivery

    async def gives_up_at_once(awaitable, timeout=None):
        raise asyncio.TimeoutError

    # Scoped: the patch reaches every module's asyncio, this test's own included.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(delivery.asyncio, "wait_for", gives_up_at_once)
        asked = await _Asked(MockSlackClient()).prompted()
        await asyncio.sleep(0.05)
        assert not asked.wait.done(), "the prompt stopped waiting on its own clock"
    asked.pending["it"].future.set_result("approved")
    assert await asyncio.wait_for(asked.wait, timeout=2) is True


class _Socket:
    """``SocketModeClient`` stand-in: holds the listener the real wiring attaches, no network."""

    def __init__(self, **_kw) -> None:
        self.socket_mode_request_listeners: list = []

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_an_owner_claimed_after_the_start_is_prompted(monkeypatch):
    """A fresh install has no owner until the first person messages the bot (``claim_owner``).
    The delivery the transport registers at the start, when nobody could be prompted, prompts
    that person once they are the owner, with no restart. Driven through the real transport."""
    import slack_runtime.events as events_mod
    import slack_runtime.interactions as interactions_mod
    import slack_runtime.transport as transport_mod
    from slack_runtime.enterprise import VALIDATED, WorkspaceCheck
    from personalclaw.sdk.channel import (
        CRED_SLACK_APP_TOKEN,
        CRED_SLACK_BOT_TOKEN,
        AppConfig,
        owner_id_credential,
    )

    for key in (owner_id_credential("slack"), "PERSONALCLAW_OWNER_ID"):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
    monkeypatch.setenv(CRED_SLACK_BOT_TOKEN, "fake-bot-token-claim")
    monkeypatch.setenv(CRED_SLACK_APP_TOKEN, "fake-app-token-claim")
    slack = MockSlackClient()
    monkeypatch.setattr(transport_mod, "RealSlackClient", lambda token: slack)
    monkeypatch.setattr(
        events_mod, "validate_enterprise", lambda *a, **k: WorkspaceCheck(VALIDATED)
    )
    monkeypatch.setattr(events_mod, "AsyncWebClient", lambda **kw: SimpleNamespace())
    monkeypatch.setattr(events_mod, "SocketModeReceiver", _Socket)
    for name in ("_gateway_services", "_orch_cfg"):
        monkeypatch.setattr(H, name, getattr(H, name))
    for name in ("global_enabled", "auto_speak", "auto_reply_to_voice"):
        monkeypatch.setattr(H._vc, name, getattr(H._vc, name))
    monkeypatch.setattr(interactions_mod, "_orch", interactions_mod._orch)
    registered: list = []
    services = SimpleNamespace(
        config=AppConfig.load(),
        owner_id="",
        register_channel_delivery=lambda d, provider="": registered.append(d),
        dashboard_state=None,
        channel_history=None,
    )
    transport = transport_mod.create_provider({})
    await transport.start_inbound(services)  # no owner yet: first-contact claim mode
    await transport.stop_inbound()
    (delivery,) = registered
    assert await delivery.request_approval(_event(), source="chat") is None, "nobody to prompt"

    assert H.claim_owner("U0CLAIMED")  # the first DM, while the gateway runs

    told: dict = {}
    wait = asyncio.ensure_future(
        delivery.request_approval(
            _event(), source="chat", on_prompted=lambda p: told.setdefault("it", p)
        )
    )
    for _ in range(200):
        if told:
            break
        await asyncio.sleep(0.01)
    assert told, "the approval skipped Slack: the claimed owner was unknown until a restart"
    assert ("open_dm", {"user_id": "U0CLAIMED"}) in slack.actions
    told["it"].future.set_result("approved")
    assert await asyncio.wait_for(wait, timeout=2) is True


# ── what core's prompt offers: the chat's approval card's answers ───────────────────────────

_THIS_CHAT_PROMISE = "Every tool in this chat runs without asking, until you change it back."

#: What core hands a prompt asked in its own chat: Allow once, Allow for this chat and Deny.
_IN_ITS_CHAT = [
    {"key": "approved", "label": "Allow once", "ends": "approved", "word": "APPROVE", "promise": ""},
    {"key": "trust", "label": "Allow for this chat", "ends": "approved", "word": "TRUST",
     "promise": _THIS_CHAT_PROMISE},
    {"key": "rejected", "label": "Deny", "ends": "rejected", "word": "DENY", "promise": ""},
]


def _asked_in_its_chat(slack: MockSlackClient) -> "_Asked":
    asked = _Asked.__new__(_Asked)
    asked.slack = slack
    asked.pending = {}
    event = _event()
    event.tool_meta["approval_brief"] = {
        "tool": "bash",
        "input": '{"command": "make test"}',
        "purpose": "run the tests",
        "risk": "caution",
        "summary": "Can: runs a command · Risk: Caution",
        "answers": _IN_ITS_CHAT,
    }
    asked.wait = asyncio.ensure_future(
        SlackDelivery(slack, lambda: OWNER).request_approval(
            event, source="chat", on_prompted=lambda p: asked.pending.setdefault("it", p)
        )
    )
    return asked


@pytest.mark.asyncio
async def test_a_prompt_in_its_chat_offers_allow_for_this_chat():
    """Core's answers, in the card's words, and what the standing one does, before the buttons."""
    asked = await _asked_in_its_chat(MockSlackClient()).prompted()
    blocks = [a[1]["blocks"] for a in asked.slack.actions if a[0] == "blocks"][-1]
    (actions,) = _buttons(blocks)
    assert [e["text"]["text"] for e in actions["elements"]] == [
        "Allow once", "Allow for this chat", "Deny",
    ]
    assert asked.posted_buttons() == ["approve_tool", "pc_answer_trust", "reject_tool"]
    assert [e.get("style") for e in actions["elements"]] == ["primary", None, "danger"]
    assert f"Allow for this chat: {_THIS_CHAT_PROMISE}" in _says(blocks)
    asked.wait.cancel()


@pytest.mark.asyncio
async def test_allow_for_this_chat_pressed_here_answers_with_it():
    """The chat's Trust is core's to set: the press tells core which answer it was, approves this
    call, and leaves this app's own thread trust alone."""
    asked = await _asked_in_its_chat(MockSlackClient()).prompted()
    H._trusted_sessions.clear()
    with patch("slack_runtime.handler.sel"):
        assert await H.handle_interaction(asked.channel, asked.ts, "pc_answer_trust", user_id=OWNER)
    assert await asyncio.wait_for(asked.wait, timeout=2) is True
    assert asked.pending["it"].future.result() == "trust"
    assert not H._trusted_sessions, "the app trusted a thread of its own for core's chat"
    assert f"✅ Approved. {_THIS_CHAT_PROMISE}" in _says(asked.closed_with())


@pytest.mark.asyncio
async def test_a_press_naming_no_offered_answer_answers_nothing():
    """Core's prompt offers no Trust session; a press on one decides nothing."""
    asked = await _asked_in_its_chat(MockSlackClient()).prompted()
    with patch("slack_runtime.handler.sel"):
        assert await H.handle_interaction(asked.channel, asked.ts, "trust_tool", user_id=OWNER) is None
        assert await H.handle_interaction(asked.channel, asked.ts, "pc_answer_yolo", user_id=OWNER) is None
    await asyncio.sleep(0)
    assert not asked.pending["it"].future.done()
    asked.wait.cancel()
