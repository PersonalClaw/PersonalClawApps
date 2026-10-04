"""A stranger who writes to the mailbox is sent nothing: their mail waits for the owner.

This mailbox is the owner's own. The channel answered any stranger with the pairing note ("I
don't recognize you yet…"), from the owner's address, before the owner knew they had written: it
told whoever wrote that the address is read, and it was mail the owner never agreed to send.

The channel now declares that it speaks as its owner. Nothing goes to a stranger. Their mail is
held in PersonalClaw's Inbox as someone new, and the owner is told, at most once a day per
address. From the Inbox the owner replies (it goes in the stranger's thread, and only when the
owner presses Send), pairs them (their next mail is a conversation), or ignores it.

Driven through the real core door, the real Inbox handlers and this app's own delivery; IMAP
and SMTP are the injected fakes.
"""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from personalclaw.channel_trust import CANNED_PAIRED_REPLY
from personalclaw.sdk.channel import (
    CANNED_PAIRING_REPLY,
    ProviderSettings,
    TrustVerdict,
    is_allowed_sender,
    save_credential,
)

from email_runtime.delivery import EmailDelivery, ThreadStore
from email_runtime.settings import CRED_IMAP_PASS, reload_settings
from email_runtime.transport import EmailTransport
from _fakes import FakeImapServer, FakeSmtpServer, FakeState, build_message

_APP = "email-channel"
MAILBOX = "me@example.test"
PAT = "pat@example.org"


class _State(FakeState):
    def broadcast_ws(self, kind, payload) -> None:
        return None


class _Services:
    """The gateway's handle, at the seam the transport calls: the real core door."""

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
def mailbox(tmp_path):
    """A configured channel on the owner's mailbox, registered and connected as the gateway
    runs it."""
    from personalclaw import channel_delivery, channel_transports
    ProviderSettings.update(
        _APP,
        {
            "imap_host": "imap.test", "imap_user": MAILBOX, "smtp_host": "smtp.test",
            "smtp_user": MAILBOX, "address": MAILBOX, "folder": "INBOX",
        },
    )
    save_credential(CRED_IMAP_PASS, "app-password")
    reload_settings()
    imap, smtp, state, turns = FakeImapServer(), FakeSmtpServer(), _State(), []
    transport = EmailTransport()
    transport._client_factory = lambda settings, password: imap
    transport._sender_factory = lambda settings, password: smtp
    transport._cursor = 0  # connected once, to an empty folder
    transport._services = _Services(state, turns)
    transport._delivery = EmailDelivery(
        smtp, MAILBOX, owner_id=MAILBOX,
        threads=ThreadStore(path_provider=lambda: tmp_path / "threads.json"),
    )
    channel_transports.register_transport(transport, app=_APP)
    channel_delivery.register(transport._delivery, provider="email")
    yield transport, imap, smtp, state, turns
    channel_delivery.register(None, provider="email")
    channel_transports.unregister_transport(transport.name)


ASKED = "Could I borrow your ladder on Saturday?"
ANSWER = "Yes, any time after ten."


async def _arrives(transport, imap, *, uid=1, sender=PAT, plain=ASKED):
    imap.add(
        uid,
        build_message(
            from_addr=f"Pat Example <{sender}>", to_addr=MAILBOX, subject="Ladder",
            message_id=f"<p{uid}@example.org>", plain=plain,
        ),
    )
    await transport._poll_once(transport._settings())
    await asyncio.sleep(0)


def _inbox_app(state) -> web.Application:
    from personalclaw.dashboard import handlers_inbox

    app = web.Application()
    app.router.add_post("/api/inbox/send", handlers_inbox.api_inbox_send)
    app.router.add_post("/api/inbox/{id}/pair", handlers_inbox.api_inbox_pair)
    app["state"] = state
    return app


async def _post(state, path, body=None):
    async with TestClient(TestServer(_inbox_app(state))) as client:
        resp = await client.post(path, json=body or {})
        return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_a_strangers_mail_is_sent_nothing_and_waits_in_the_inbox(mailbox):
    """🔴 Before: "I don't recognize you yet…" went to them, from the owner's own address."""
    transport, imap, smtp, state, turns = mailbox

    await _arrives(transport, imap)

    assert smtp.sent == [], f"mailed {[str(m['To']) for m in smtp.sent]} from {MAILBOX}"
    assert turns == []
    [(args, _meta)] = state.notified
    assert args[1] == "Someone new wrote to you on Email"
    [row] = state._inbox_store.items.values()
    assert (row.sender_id, row.sender_name) == (PAT, "Pat Example")
    assert row.message == f"Ladder\n\n{ASKED}"
    assert row.refs == {"someone_new": "email", "channel_name": "Email"}
    assert row.thread_ts == "<p1@example.org>"


@pytest.mark.asyncio
async def test_the_owners_reply_from_the_inbox_goes_to_them_in_their_thread(mailbox):
    transport, imap, smtp, state, _ = mailbox
    await _arrives(transport, imap)
    [row] = state._inbox_store.items.values()

    status, body = await _post(state, "/api/inbox/send", {"id": row.id, "text": ANSWER})

    assert status == 200 and body == {"ok": True, "sent": True}, body
    [sent] = smtp.sent
    assert str(sent["To"]) == PAT
    assert smtp.header("In-Reply-To") == "<p1@example.org>"
    assert smtp.header("Subject") == "Re: Ladder"
    assert smtp.body_text().strip() == ANSWER


@pytest.mark.asyncio
async def test_pair_from_the_inbox_makes_their_next_mail_a_conversation(mailbox):
    transport, imap, smtp, state, turns = mailbox
    await _arrives(transport, imap)
    [row] = state._inbox_store.items.values()

    # Letting someone talk to your agent asks your consent first, as the dashboard's Pair does.
    status, body = await _post(state, f"/api/inbox/{row.id}/pair", {"confirm": True})

    assert status == 200 and body == {"ok": True, "paired": True}, body
    assert is_allowed_sender("email", PAT) is True
    await _arrives(transport, imap, uid=2, plain="Thanks!")
    assert len(turns) == 1 and "Thanks!" in turns[0]
    assert smtp.sent == [], "pairing them mailed them something by itself"


class _Door:
    """A door that hands back the verdict it is given: what this app does with each reply."""

    def __init__(self, verdict) -> None:
        self.dashboard_state = FakeState()
        self._verdict = verdict

    async def deliver_channel_inbound(self, provider, msg, *, is_dm=True):
        return self._verdict


@pytest.mark.asyncio
async def test_a_reply_the_door_hands_back_for_a_stranger_is_not_sent(mailbox):
    """Even from a door that still hands back the pairing note: a mail from here goes out as
    the owner, so this app sends a stranger nothing."""
    transport, imap, smtp, _, _ = mailbox
    transport._services = _Door(
        TrustVerdict(allowed=False, reason="unknown_sender", canned_reply=CANNED_PAIRING_REPLY)
    )

    await _arrives(transport, imap)

    assert smtp.sent == []


@pytest.mark.asyncio
async def test_the_one_reply_it_sends_is_to_someone_who_just_paired(mailbox):
    """The one mail this app sends on its own to someone it did not know: that they paired."""
    transport, imap, smtp, _, _ = mailbox
    transport._services = _Door(
        TrustVerdict(
            allowed=False, reason="paired", canned_reply=CANNED_PAIRED_REPLY, meta={"paired": True}
        )
    )

    await _arrives(transport, imap, plain="hello")

    [sent] = smtp.sent
    assert str(sent["To"]) == PAT
    assert smtp.body_text().strip() == CANNED_PAIRED_REPLY
    assert smtp.header("In-Reply-To") == "<p1@example.org>"
