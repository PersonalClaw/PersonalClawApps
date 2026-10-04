"""Slack names its owner only by the pairing code, as Telegram, Discord and email do.

With no owner set, the first person to message the bot directly, or to mention it in a tracked
channel, became its owner for good: from then on they answered every tool approval, and a
dashboard link that signs in as the owner could be sent to them. No message names an owner now.
Configure → Pair as owner shows a code, and whoever sends it to the bot in a direct message becomes
Slack's owner, through core, which counts wrong guesses as it does for every channel. Until then
the bot does nothing anyone asks: a direct message or a mention is told how to pair, nobody's
press answers an approval, and nobody can be sent a dashboard link. The transport says it pairs, so
the Configure page offers it.

Driven through the real inbound route, the real approval resolver and core's real credential and
trust stores, in this test's home. Each refusal sits beside its vacuity floor: the same message,
from the owner who paired, is answered.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_helpers import MockSlackClient

from personalclaw import channel_trust
from personalclaw.config.credentials import get_credential, owner_id_credential
from personalclaw.config.loader import CRED_OWNER_ID
from personalclaw.sdk.channel import CANNED_OWNER_PAIRED_REPLY, owner_id_for, owner_sign_in_token

import slack_runtime.handler as H
from slack_runtime.events import _route_message
from slack_runtime.handler import _PendingApproval, _pending_approvals, handle_interaction
from slack_runtime.settings import SlackSettings

OWNER = "U0NOORPAIRS"
STRANGER = "U0DANFIRST"
DM = "D0NOORDM"
CHANNEL = "C0TEAMROOM"
PROMPT_TS = "1712793600.000500"
_OWN = owner_id_credential("slack")

#: The note a direct message or a mention gets while Slack has no owner, by the words that make it
#: one: it points at the dashboard, where the owner pairs.
_HOW_TO_PAIR = "Pair as owner"


@pytest.fixture(autouse=True)
def _no_owner(monkeypatch):
    """No owner anywhere, an empty allowlist, and one tracked channel the bot sits in."""
    for key in (_OWN, CRED_OWNER_ID):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
    monkeypatch.setattr(H, "_allowed_users", set())
    monkeypatch.setattr(H, "_tracking_channels", {CHANNEL})
    _pending_approvals.clear()
    yield
    _pending_approvals.clear()


def _orch() -> MagicMock:
    orch = MagicMock()
    orch.settings = SlackSettings()
    orch.channel_history = MagicMock()
    orch.channel_history._user_names = {OWNER: "Noor"}
    orch.slack = MockSlackClient()
    orch.sessions = AsyncMock()
    orch.sessions.enqueue = MagicMock(return_value=False)
    orch.sessions.is_cancelled = MagicMock(return_value=False)
    orch.sessions.dequeue = MagicMock(return_value=None)
    orch.ctx_builder = orch.cron_svc = orch.conv_log = orch.consolidator = None
    orch.subagent_mgr = orch.task_runner = None
    orch._handler_tasks, orch._session_tasks, orch._pending_queue = set(), {}, {}
    return orch


async def _send(
    orch, sender: str, text: str, *, ts: str, dm: bool = True, mention_event: bool | None = None
) -> AsyncMock:
    """One message through the inbound route: a direct message, or a mention in the channel,
    delivered as Slack's ``app_mention`` unless *mention_event* says it is the plain ``message``
    Slack announces beside it. Returns the stand-in for the turn, which says whether one was
    started."""
    event = {
        "user": sender,
        "channel": DM if dm else CHANNEL,
        "text": text if dm else f"<@U0BOT> {text}",
        "ts": ts,
        "team": "TTEST",
        **({"channel_type": "im"} if dm else {"channel_type": "channel"}),
    }
    with patch("slack_runtime.events.handle_message", new_callable=AsyncMock) as turn:
        await _route_message(
            orch, event, is_mention=(not dm) if mention_event is None else mention_event
        )
        await asyncio.sleep(0)
        await asyncio.gather(*list(orch._handler_tasks), return_exceptions=True)
    return turn


def _said(orch, kind: str = "post") -> list[dict]:
    return [a[1] for a in orch.slack.actions if a[0] == kind]


class _Provider:
    def __init__(self) -> None:
        self.approved: list[str] = []

    async def approve_tool(self, request_id, option_id="allow_once"):
        self.approved.append(request_id)

    async def reject_tool(self, request_id):
        pass


def _prompt() -> tuple[_PendingApproval, _Provider]:
    """A tool approval waiting in the DM, offering Allow once and Deny."""
    provider = _Provider()
    answers = [
        {"key": "approved", "label": "Allow once", "ends": "approved", "word": "APPROVE", "promise": ""},
        {"key": "rejected", "label": "Deny", "ends": "rejected", "word": "DENY", "promise": ""},
    ]
    pending = _PendingApproval(provider, "req-1", "", answers=answers)  # type: ignore[arg-type]
    _pending_approvals[f"{DM}:{PROMPT_TS}"] = pending
    return pending, provider


# ── no owner: nobody is the owner, and the bot says how to pair ───────────────────────────


@pytest.mark.asyncio
async def test_a_first_direct_message_makes_nobody_the_owner():
    orch = _orch()

    turn = await _send(orch, STRANGER, "hi, who are you?", ts="1.0")

    assert H.get_owner_id() == "", "the first sender became the owner"
    assert owner_id_for("slack") == ""
    assert get_credential(_OWN) == ""
    assert not channel_trust.is_allowed_sender("slack", STRANGER)
    turn.assert_not_called()
    (note,) = _said(orch)
    assert note["channel"] == DM and _HOW_TO_PAIR in note["text"]


@pytest.mark.asyncio
async def test_a_first_mention_in_a_tracked_channel_makes_nobody_the_owner():
    orch = _orch()

    turn = await _send(orch, STRANGER, "summarise this thread", ts="2.0", dm=False)

    assert H.get_owner_id() == ""
    assert get_credential(_OWN) == ""
    turn.assert_not_called()
    assert _said(orch) == [], "the note to one person went to the whole channel"
    (note,) = _said(orch, "ephemeral")
    assert note["user_id"] == STRANGER and _HOW_TO_PAIR in note["text"]


@pytest.mark.asyncio
async def test_a_mention_slack_announces_twice_and_delivers_again_is_told_once():
    """Slack announces a mention as a ``message`` and an ``app_mention``, and delivers an event
    again when it is not sure its acknowledgement arrived: the note goes out once."""
    orch = _orch()

    for mention_event in (False, True, True):
        await _send(
            orch, STRANGER, "summarise this thread", ts="2.1", dm=False, mention_event=mention_event
        )

    (note,) = _said(orch, "ephemeral")
    assert _HOW_TO_PAIR in note["text"]


@pytest.mark.asyncio
async def test_an_approval_answer_from_the_first_sender_is_refused():
    orch = _orch()
    await _send(orch, STRANGER, "hello", ts="3.0")
    pending, provider = _prompt()
    slack = MockSlackClient()

    result = await handle_interaction(DM, PROMPT_TS, "approve_tool", user_id=STRANGER, slack=slack)

    assert result is None
    assert not pending.future.done(), "the first sender answered the approval"
    assert provider.approved == []
    told = [a[1]["text"] for a in slack.actions if a[0] == "ephemeral"]
    assert told == ["Only the owner can answer this."]


@pytest.mark.asyncio
async def test_a_first_sender_asking_for_the_dashboard_is_sent_no_link():
    orch = _orch()

    turn = await _send(orch, STRANGER, "!dashboard", ts="4.0")

    turn.assert_not_called()
    assert not any("token=" in a["text"] for a in _said(orch))
    with pytest.raises(ValueError):
        owner_sign_in_token("slack", STRANGER, 600)


# ── the code the Configure page shows names the owner ─────────────────────────────────────


@pytest.mark.asyncio
async def test_after_pairing_with_the_code_her_approvals_work():
    orch = _orch()
    await _send(orch, STRANGER, "hello", ts="5.0")
    code = channel_trust.create_owner_pairing_code("slack")

    turn = await _send(orch, OWNER, code, ts="5.1")

    turn.assert_not_called()  # the code is spent on pairing, never a turn
    paired = _said(orch)[-1]
    assert (paired["channel"], paired["text"]) == (DM, CANNED_OWNER_PAIRED_REPLY)
    assert H.get_owner_id() == OWNER
    assert channel_trust.paired_owner("slack") == OWNER
    assert channel_trust.owner_pairing_status("slack")["ended"] == "paired"
    assert channel_trust.owner_ref("slack")["name"] == "Noor"

    pending, provider = _prompt()
    refused = await handle_interaction(
        DM, PROMPT_TS, "approve_tool", user_id=STRANGER, slack=MockSlackClient()
    )
    assert refused is None and not pending.future.done()
    with patch("slack_runtime.handler.answer_in_chat", return_value=True):
        answered = await handle_interaction(DM, PROMPT_TS, "approve_tool", user_id=OWNER)
    assert answered == "approve_tool"
    assert provider.approved == ["req-1"]

    later = await _send(orch, OWNER, "what's on today?", ts="5.2")
    later.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_wrong_code_pairs_nobody_and_counts_against_the_code():
    orch = _orch()
    code = channel_trust.create_owner_pairing_code("slack")
    wrong = f"{(int(code) + 1) % 10**8:08d}"

    turn = await _send(orch, STRANGER, wrong, ts="6.0")

    turn.assert_not_called()
    assert H.get_owner_id() == ""
    status = channel_trust.owner_pairing_status("slack")
    assert status["active"] is True
    assert status["attempts_left"] == channel_trust.OWNER_PAIRING_MAX_ATTEMPTS - 1
    assert _HOW_TO_PAIR in _said(orch)[-1]["text"]


@pytest.mark.asyncio
async def test_a_code_slack_delivers_again_pairs_once_and_is_never_a_turn():
    """Slack delivers an event again when it is not sure its acknowledgement arrived. The DM was
    claimed before the code was looked for, so the delivery made again changes nothing: no second
    reply, no turn, and no wrong guess at a code already spent."""
    orch = _orch()
    code = channel_trust.create_owner_pairing_code("slack")

    first = await _send(orch, OWNER, code, ts="9.0")
    again = await _send(orch, OWNER, code, ts="9.0")

    assert H.get_owner_id() == OWNER
    first.assert_not_called()
    again.assert_not_called()
    assert [p["text"] for p in _said(orch)] == [CANNED_OWNER_PAIRED_REPLY]


@pytest.mark.asyncio
async def test_a_wrong_code_slack_delivers_again_counts_once():
    orch = _orch()
    code = channel_trust.create_owner_pairing_code("slack")
    wrong = f"{(int(code) + 1) % 10**8:08d}"

    await _send(orch, STRANGER, wrong, ts="9.1")
    await _send(orch, STRANGER, wrong, ts="9.1")

    status = channel_trust.owner_pairing_status("slack")
    assert status["attempts_left"] == channel_trust.OWNER_PAIRING_MAX_ATTEMPTS - 1
    assert len(_said(orch)) == 1, "the note was sent twice"
    assert _HOW_TO_PAIR in _said(orch)[0]["text"]


@pytest.mark.asyncio
async def test_the_code_in_a_channel_pairs_nobody():
    """The code is sent in a direct message: posted in a channel, everyone there has seen it."""
    orch = _orch()
    code = channel_trust.create_owner_pairing_code("slack")

    await _send(orch, OWNER, code, ts="7.0", dm=False)

    assert H.get_owner_id() == ""
    assert channel_trust.owner_pairing_status("slack")["active"] is True


@pytest.mark.asyncio
async def test_a_new_owner_pairs_in_place_of_the_one_before():
    orch = _orch()
    await _send(orch, OWNER, channel_trust.create_owner_pairing_code("slack"), ts="8.0")

    await _send(orch, "U0ROBINNEXT", channel_trust.create_owner_pairing_code("slack"), ts="8.1")

    assert H.get_owner_id() == "U0ROBINNEXT"
    assert H.is_owner(OWNER) is False


def test_the_transport_says_it_pairs_its_owner():
    from personalclaw.dashboard.handlers.channel_owner import _pairing_supported

    import slack_runtime.transport as transport_mod

    transport = transport_mod.create_provider({})

    assert transport.capabilities().owner_pairing is True
    assert _pairing_supported(transport) is True, "the Configure page would not offer pairing"


def test_with_no_owner_nobody_is_let_in_whoever_is_listed():
    """Allowed Users is the owner's list: with no owner, nobody on it is let in either."""
    H.set_allowed_users({"U0LISTED"})

    assert H.is_allowed_user("U0LISTED") is False
    assert H.is_owner("U0LISTED") is False
