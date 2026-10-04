"""A reply to an approval that has ended is told how it ended, and the mail says how long it waits.

A sent mail cannot be edited, so the approval mail cannot show how its approval ended the way a
chat prompt now does. What the owner does see is the answer to their reply, and that was wrong in
two ways. A reply after the approval ended matched nothing, so ``APPROVE 1A2B3C4D`` went to the
agent as a new message. And the mail said "No reply within 2h counts as a denial": this channel
stopped waiting after two hours of its own and reported a Deny nobody gave, whatever the owner's
Approval wait said (it can be a week), and an approval nobody answered is not a Deny.

Core now ends every approval it asks here (Approve or Deny in a reply, an answer in PersonalClaw,
nobody answering in time, the work that asked stopping first) and resolves the prompt's record with
how it ended. The wait keeps no clock of its own, the mail says how long PersonalClaw waits, and a
reply after the end is consumed and answered in the prompt's thread with how it ended.

The endings were kept in memory, so a restart forgot them all and a reply after it reached the
agent again. Each approval is now kept in the app's own data from the moment its mail goes out.
"""

from __future__ import annotations

import asyncio

import pytest
from test_transport import FakeServices, _configure, _mail

from personalclaw.sdk.channel import AppConfig, allow_sender, owner_id_credential, sel

from email_runtime.delivery import EmailDelivery, ThreadStore
from email_runtime.transport import EmailTransport
from _fakes import FakeImapServer, FakeSmtpServer, FakeState

#: The reply words of the answers an approval with no chat of its own offers (core's brief).
APPROVE_WORD, DENY_WORD = "APPROVE", "DENY"
AGENT = "agent@example.com"
OWNER = "me@example.com"
BOB = "bob@example.com"

ENDED_ANSWERS = {
    "approved": "This approval was already approved. Your reply changes nothing.",
    "rejected": "This approval was already rejected. Your reply changes nothing.",
    "expired": "Nobody answered this approval in time, so it did not run. Your reply changes "
    "nothing.",
    "cancelled": "This approval was cancelled: the work that asked for it stopped first, so it "
    "did not run. Your reply changes nothing.",
}


class _Asks:
    request_id = "req-7"
    title = "run the thing"
    tool_input = ""
    tool_purpose = ""
    tool_meta: dict = {}


def _started(tmp_path):
    """A transport over fake IMAP and SMTP, as the gateway starts one, with the turn captured. Its
    delivery keeps its approvals where the app keeps them, so a second one started over the same
    home is this channel after a restart."""
    captured: dict = {}
    imap, smtp = FakeImapServer(), FakeSmtpServer()
    transport = EmailTransport()
    transport._client_factory = lambda settings, password: imap
    transport._sender_factory = lambda settings, password: smtp
    transport._cursor = 0
    transport._services = FakeServices(FakeState(), captured)
    transport._delivery = EmailDelivery(
        smtp, AGENT, owner_id=AGENT,
        threads=ThreadStore(path_provider=lambda: tmp_path / "threads.json"),
    )
    return transport, imap, smtp, captured


@pytest.fixture
def mailbox(monkeypatch, tmp_path):
    """A configured transport, the real core door with the turn captured, and the owner allowed
    and known by their address."""
    _configure()
    monkeypatch.setenv(owner_id_credential("email"), OWNER)
    allow_sender("email", OWNER)
    return _started(tmp_path)


class _Asked:
    """Core asking the owner by mail: the wait, the record core resolves, and the prompt's token."""

    def __init__(self, delivery: EmailDelivery) -> None:
        self.delivery = delivery
        self.record: dict = {}
        self.wait = asyncio.ensure_future(
            delivery.request_approval(
                _Asks(), source="tool", on_prompted=lambda p: self.record.setdefault("it", p)
            )
        )

    async def prompted(self) -> "_Asked":
        for _ in range(400):  # the send hops to a worker thread
            if self.record:
                return self
            await asyncio.sleep(0.005)
        raise AssertionError("the prompt was never mailed")

    @property
    def token(self) -> str:
        return self.record["it"].token

    async def ends(self, how: str) -> None:
        self.record["it"].future.set_result(how)
        await asyncio.wait_for(self.wait, timeout=2)


async def _mailed(smtp: FakeSmtpServer, count: int) -> None:
    for _ in range(400):
        if len(smtp.sent) >= count:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"only {len(smtp.sent)} of {count} mails went out")


# ── a reply after the end ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_late_reply_is_answered_in_its_thread_and_is_not_a_message_to_the_agent(mailbox):
    transport, imap, smtp, captured = mailbox
    asked = await _Asked(transport._delivery).prompted()
    prompt_id = smtp.header("Message-ID")
    await asked.ends("expired")

    _mail(
        1, imap, from_addr=OWNER, subject="Re: [PersonalClaw] Approval needed: a tool",
        in_reply_to=prompt_id, references=prompt_id, plain=f"{APPROVE_WORD} {asked.token}",
    )
    await transport._poll_once(transport._settings())

    assert "text" not in captured, "the late reply reached the agent as a new message"
    await _mailed(smtp, 2)
    assert smtp.header("To") == OWNER
    assert smtp.body_text().strip() == ENDED_ANSWERS["expired"]
    assert smtp.header("In-Reply-To") == "<m1@example.com>", "not answered in the reply's thread"
    assert prompt_id in smtp.header("References")


@pytest.mark.asyncio
@pytest.mark.parametrize("ended", sorted(ENDED_ANSWERS))
async def test_a_late_reply_is_told_how_the_approval_ended(ended, mailbox):
    transport, _, smtp, _ = mailbox
    delivery = transport._delivery
    asked = await _Asked(delivery).prompted()
    await asked.ends(ended)

    assert delivery.resolve_reply_token(f"{DENY_WORD} {asked.token}", OWNER) is True
    await _mailed(smtp, 2)
    assert smtp.body_text().strip() == ENDED_ANSWERS[ended]


@pytest.mark.asyncio
async def test_a_reply_after_the_wait_was_cancelled_is_told_it_was_cancelled(mailbox):
    transport, _, smtp, _ = mailbox
    delivery = transport._delivery
    asked = await _Asked(delivery).prompted()
    asked.wait.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asked.wait

    assert delivery.resolve_reply_token(f"{APPROVE_WORD} {asked.token}", OWNER) is True
    await _mailed(smtp, 2)
    assert smtp.body_text().strip() == ENDED_ANSWERS["cancelled"]


@pytest.mark.asyncio
async def test_someone_else_s_late_reply_is_refused_as_a_live_one_is(mailbox, monkeypatch):
    """Only the owner is answered: anyone else's reply to the owner's approval is logged to the
    SEL and consumed, as it is while the approval waits, and mails nobody."""
    rows: list[dict] = []
    monkeypatch.setattr(type(sel()), "log_api_access", lambda _log, **row: rows.append(row))
    transport, imap, smtp, captured = mailbox
    allow_sender("email", BOB)
    asked = await _Asked(transport._delivery).prompted()
    await asked.ends("approved")

    _mail(1, imap, from_addr=BOB, plain=f"{APPROVE_WORD} {asked.token}")
    await transport._poll_once(transport._settings())
    await asyncio.sleep(0.05)

    assert "text" not in captured
    assert len(smtp.sent) == 1, "someone who is not the owner was mailed about the approval"
    refused = [r for r in rows if r.get("operation") == "email.approval_reply"]
    assert [(r["caller"], r["resources"], r["error"]) for r in refused] == [
        (f"email:{BOB}", "req-7", "not the owner")
    ]


@pytest.mark.asyncio
async def test_the_owner_s_reply_while_it_waits_is_still_the_answer(mailbox):
    """The floor: a reply to a waiting approval decides it, and nothing is mailed back."""
    transport, imap, smtp, captured = mailbox
    asked = await _Asked(transport._delivery).prompted()

    _mail(1, imap, from_addr=OWNER, plain=f"{DENY_WORD} {asked.token}")
    await transport._poll_once(transport._settings())

    assert await asyncio.wait_for(asked.wait, timeout=2) is False
    assert "text" not in captured
    await asyncio.sleep(0.05)
    assert len(smtp.sent) == 1


@pytest.mark.asyncio
async def test_a_message_that_answers_no_approval_still_reaches_the_agent(mailbox):
    """The other floor: an approval that ended does not swallow the owner's next ordinary mail."""
    transport, imap, _, captured = mailbox
    asked = await _Asked(transport._delivery).prompted()
    await asked.ends("approved")

    _mail(1, imap, from_addr=OWNER, plain=f"thanks, and about {asked.token}: what did it change?")
    await transport._poll_once(transport._settings())

    for _ in range(200):  # the turn is started as a task of its own
        if "text" in captured:
            break
        await asyncio.sleep(0.005)
    assert "what did it change" in captured.get("text", ""), "an ordinary mail was swallowed"


# ── a restart forgets no approval ───────────────────────────────────────────────────────────


async def _reply_after_a_restart(tmp_path, token: str):
    """The owner's reply to *token*, read by this channel started again over the same home."""
    transport, imap, smtp, captured = _started(tmp_path)
    _mail(1, imap, from_addr=OWNER, plain=f"{APPROVE_WORD} {token}")
    await transport._poll_once(transport._settings())
    return smtp, captured


@pytest.mark.asyncio
@pytest.mark.parametrize("ended", sorted(ENDED_ANSWERS))
async def test_a_late_reply_after_a_restart_is_still_told_how_the_approval_ended(
    ended, mailbox, tmp_path
):
    """🔴 Before: the endings were kept in memory only, so after a restart the reply matched
    nothing and reached the agent as a new message."""
    transport, _, _, _ = mailbox
    asked = await _Asked(transport._delivery).prompted()
    await asked.ends(ended)

    smtp, captured = await _reply_after_a_restart(tmp_path, asked.token)

    assert "text" not in captured, "after a restart the late reply reached the agent"
    await _mailed(smtp, 1)
    assert smtp.header("To") == OWNER
    assert smtp.body_text().strip() == ENDED_ANSWERS[ended]


@pytest.mark.asyncio
async def test_a_reply_to_an_approval_a_crash_cut_short_is_told_it_is_no_longer_waiting(
    mailbox, tmp_path
):
    """A process that stops without ending its wait (a crash) records no ending, yet the owner
    may still reply to its mail. That approval is written down when its mail goes out, so the
    reply after the restart is answered, and approves nothing."""
    transport, _, _, _ = mailbox
    asked = await _Asked(transport._delivery).prompted()  # still waiting when it "crashes"

    smtp, captured = await _reply_after_a_restart(tmp_path, asked.token)

    assert "text" not in captured, "after a crash the reply reached the agent"
    await _mailed(smtp, 1)
    assert smtp.body_text().strip() == (
        "This approval is no longer waiting. Your reply changes nothing."
    )
    asked.wait.cancel()


@pytest.mark.asyncio
async def test_an_ordinary_mail_after_a_restart_still_reaches_the_agent(mailbox, tmp_path):
    """The floor of the two above: what is kept is approvals, and nothing else is swallowed."""
    transport, _, _, _ = mailbox
    asked = await _Asked(transport._delivery).prompted()
    await asked.ends("approved")

    restarted, imap, _, captured = _started(tmp_path)
    _mail(1, imap, from_addr=OWNER, plain=f"about {asked.token}: what did it change?")
    await restarted._poll_once(restarted._settings())

    for _ in range(200):  # the turn is started as a task of its own
        if "text" in captured:
            break
        await asyncio.sleep(0.005)
    assert "what did it change" in captured.get("text", ""), "an ordinary mail was swallowed"


def test_the_kept_approvals_are_bounded(tmp_path):
    """The newest ones are kept, so a long-lived mailbox cannot grow the record without limit."""
    from email_runtime.delivery import _ENDED_KEPT, EndedApprovalStore, _EndedApproval

    store = EndedApprovalStore(path_provider=lambda: tmp_path / "approvals.json")
    for n in range(_ENDED_KEPT + 3):
        store.record(f"T{n:07d}", _EndedApproval("approved", f"req-{n}", OWNER, "<t@example.com>"))

    again = EndedApprovalStore(path_provider=lambda: tmp_path / "approvals.json").items()
    assert len(again) == _ENDED_KEPT
    assert again[0][0] == "T0000003" and again[-1][0] == f"T{_ENDED_KEPT + 2:07d}"


def test_an_unreadable_record_reads_as_empty(tmp_path):
    from email_runtime.delivery import EndedApprovalStore

    (tmp_path / "approvals.json").write_text("{not json", encoding="utf-8")
    assert EndedApprovalStore(path_provider=lambda: tmp_path / "approvals.json").items() == []


# ── the wait is PersonalClaw's ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("minutes", "said"),
    [(120, "2 hours"), (60, "1 hour"), (90, "90 minutes"), (10080, "7 days"), (1440, "1 day")],
)
async def test_the_mail_says_how_long_personalclaw_waits(minutes, said, mailbox):
    cfg = AppConfig.load()
    cfg.agent.approval_timeout_minutes = minutes
    cfg.save()
    transport, _, smtp, _ = mailbox
    asked = await _Asked(transport._delivery).prompted()

    body = smtp.body_text()
    assert (
        f"PersonalClaw waits up to {said} for your answer. If nobody answers by then, it does not"
        " run." in " ".join(body.split())
    )
    assert "denial" not in body, "an approval nobody answered is not a Deny"
    await asked.ends("rejected")


@pytest.mark.asyncio
async def test_the_wait_keeps_no_timer_of_its_own(mailbox):
    """The owner's window can be a week; a wait that gave up on a clock of its own reported a Deny
    while PersonalClaw still waited. Any timer here gives up at once."""
    import email_runtime.delivery as delivery_mod

    async def gives_up_at_once(awaitable, timeout=None):
        raise asyncio.TimeoutError

    transport, _, _, _ = mailbox
    # Scoped: the patch reaches every module's asyncio, this test's own included.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(delivery_mod.asyncio, "wait_for", gives_up_at_once)
        asked = await _Asked(transport._delivery).prompted()
        await asyncio.sleep(0.05)
        assert not asked.wait.done(), "the mail stopped waiting on its own clock"
    asked.record["it"].future.set_result("approved")
    assert await asyncio.wait_for(asked.wait, timeout=2) is True
