"""A mail the channel reads again runs once.

The channel saves its place in the folder once a batch is done, so a gateway stopped in the middle
of one reads that batch again when it starts, and every branch a mail takes acted on it again: a
second turn, a second event for this bundle's automations, a pairing code tried a second time
(the code was spent the first time, so the mail itself became a question for the agent). Each
mail is now claimed with PersonalClaw before anything acts on it (``claim_message``), by its own
``Message-ID``, or by this mailbox's own name for it when it came without one, and a mail read
again changes nothing. The door behind it is core's own.
"""

from __future__ import annotations

import asyncio

import pytest

from personalclaw.sdk.channel import (
    ProviderSettings,
    allow_sender,
    create_pairing_code,
    is_allowed_sender,
    save_credential,
)

from email_runtime import inbound_tap
from email_runtime.delivery import EmailDelivery, ThreadStore
from email_runtime.mime import parse_inbound
from email_runtime.settings import CRED_IMAP_PASS, reload_settings
from email_runtime.transport import EmailTransport
from _fakes import FakeImapServer, FakeSmtpServer, FakeState, build_message

_APP = "email-channel"
AGENT = "agent@example.test"
SAM = "sam@example.test"


class _Services:
    def __init__(self, state, turns) -> None:
        self.dashboard_state = state
        self._turns = turns

    def register_channel_delivery(self, delivery) -> None:
        return None

    async def deliver_channel_inbound(self, provider, msg, *, is_dm=True):
        from personalclaw.channel_inbound import deliver_inbound

        async def turn_runner(state, session, text):
            self._turns.append(text)

        return await deliver_inbound(self, provider, msg, is_dm=is_dm, turn_runner=turn_runner)


@pytest.fixture
def wired(tmp_path):
    ProviderSettings.update(
        _APP,
        {
            "imap_host": "imap.test", "imap_user": AGENT, "smtp_host": "smtp.test",
            "smtp_user": AGENT, "address": AGENT, "folder": "INBOX",
        },
    )
    save_credential(CRED_IMAP_PASS, "app-password")
    reload_settings()
    imap, smtp, state, turns = FakeImapServer(), FakeSmtpServer(), FakeState(), []
    transport = EmailTransport()
    transport._client_factory = lambda settings, password: imap
    transport._sender_factory = lambda settings, password: smtp
    transport._cursor = 0  # connected once, to an empty folder
    transport._services = _Services(state, turns)
    transport._delivery = EmailDelivery(
        smtp, AGENT, owner_id=AGENT,
        threads=ThreadStore(path_provider=lambda: tmp_path / "threads.json"),
    )
    from personalclaw import channel_transports

    channel_transports.register_transport(transport, app=_APP)
    yield transport, imap, smtp, turns
    channel_transports.unregister_transport(transport.name)


async def _read(transport) -> None:
    await transport._poll_once(transport._settings())
    await asyncio.sleep(0)


async def _read_again(transport) -> None:
    """The folder read again from the place saved before the mail: what a gateway stopped
    before it saved its cursor reads when it starts."""
    transport._cursor = 0
    await _read(transport)


def _mail(imap, *, uid=1, sender=SAM, body="Can you move my dentist to Friday?", **kw) -> None:
    kw.setdefault("message_id", f"<m{uid}@example.test>")
    imap.add(uid, build_message(from_addr=sender, to_addr=AGENT, plain=body, **kw))


@pytest.mark.asyncio
async def test_a_mail_read_again_runs_one_turn(wired):
    transport, imap, _, turns = wired
    allow_sender("email", SAM, "Sam")
    _mail(imap)

    await _read(transport)
    assert turns == ["Can you move my dentist to Friday?"], "vacuity floor: the mail ran"
    await _read_again(transport)

    assert turns == ["Can you move my dentist to Friday?"], "the mail read again ran again"
    assert transport._cursor == 1


@pytest.mark.asyncio
async def test_two_mails_with_the_same_words_are_two_turns(wired):
    """The partner of the test above: mails are told apart by their ids, not their words."""
    transport, imap, _, turns = wired
    allow_sender("email", SAM, "Sam")
    _mail(imap, uid=1, body="yes")
    _mail(imap, uid=2, body="yes")

    await _read(transport)

    assert turns == ["yes", "yes"]


@pytest.mark.asyncio
async def test_an_automated_mail_read_again_reaches_the_automations_once(wired):
    transport, imap, _, turns = wired
    allow_sender("email", SAM, "Sam")
    seen: list[str] = []

    def observer(cm, *, text):
        seen.append(text)

    inbound_tap.subscribe(observer)
    try:
        _mail(imap, body="Build 214 passed.", extra_headers={"Auto-Submitted": "auto-generated"})
        await _read(transport)
        await _read_again(transport)
    finally:
        inbound_tap.unsubscribe(observer)

    assert seen == ["Build 214 passed."], "the automations heard the mail read again too"
    assert turns == []


@pytest.mark.asyncio
async def test_a_pairing_code_read_again_pairs_once_and_is_never_a_turn(wired):
    """The code pairs its sender and is confirmed once. Read again, the mail is no question for
    the agent: its sender is paired now, so it would otherwise have crossed the door as one."""
    transport, imap, smtp, turns = wired
    code = create_pairing_code("email")
    _mail(imap, sender="newcomer@example.test", body=f"Here is the code: {code}\n\nThanks!")

    await _read(transport)
    assert is_allowed_sender("email", "newcomer@example.test"), "vacuity floor: the code paired"
    confirmed = len(smtp.sent)
    assert confirmed == 1
    await _read_again(transport)

    assert len(smtp.sent) == confirmed, "the pairing was confirmed a second time"
    assert turns == [], "the pairing mail read again became a question for the agent"


@pytest.mark.asyncio
async def test_a_mail_with_no_message_id_is_named_by_the_mailbox_and_runs_once(wired):
    transport, imap, _, turns = wired
    allow_sender("email", SAM, "Sam")
    _mail(imap, uid=7, message_id="", body="No id on this one.")
    mail = parse_inbound(imap.fetch_message("INBOX", 7), 7)
    assert mail is not None and mail.message_id == "", "vacuity floor: the mail has no id"

    assert transport._to_channel_message(mail).message_id.startswith("imap:INBOX:")
    await _read(transport)
    await _read_again(transport)

    assert turns == ["No id on this one."]
