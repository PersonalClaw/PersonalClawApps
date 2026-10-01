"""EmailTransport against core's channel conformance kit.

The kit is the ONE executable statement of the channel contract, shipped by core and
imported through ``personalclaw.sdk.channel``. Email is the channel that exercises the
kit's *negative* streaming clause: it declares ``edits=False`` (§C3 "streaming MUST-NOT"),
so instead of a throttle the kit asserts that ``start_stream`` returns ``""`` — core reads
``await start_stream(...) or ""`` and would otherwise begin an animation this channel can
never update.

The rest is the shared floor: connect/send echo shapes, capability-dict completeness,
health/test shapes, the unknown-sender flow (no reply at all: this channel speaks as its
owner), and non-owner content entering a session FENCED. With the owner's reply wired, how
an approval ends however it ends, and what a reply after that is told, driven at
``resolve_reply_token``, where a reply arrives. Email-specific behaviour (MIME threading
headers, UIDVALIDITY recovery) stays in this bundle's other test modules.
"""

from __future__ import annotations

import pytest

from personalclaw.sdk.channel import (
    ChannelContractError,
    assert_channel_contract,
    owner_id_credential,
)

from email_runtime.delivery import EmailDelivery
from email_runtime.transport import EmailTransport

from _fakes import FakeSmtpServer

AGENT = "agent@example.com"
OWNER = "owner@example.org"


def _reply(delivery: EmailDelivery, smtp: FakeSmtpServer):
    """The owner's reply with one of the prompt's answers, by its key: the word the mail lists
    for it and the token, through the handler an inbound reply reaches; returns what the owner
    was mailed back ("" when nothing was)."""

    async def press(pending, answer: str) -> str:
        mailed = len(smtp.sent)
        (word,) = [a["word"] for a in pending.answers if a["key"] == answer]
        offered = (f"{word} {pending.token}" in smtp.body_text(i) for i in range(len(smtp.sent)))
        assert any(offered), "the mail did not offer it"
        assert delivery.resolve_reply_token(f"{word} {pending.token}", OWNER)
        for task in list(delivery._answering):  # the answer to a late reply goes on its own
            await task
        return smtp.body_text().strip() if len(smtp.sent) > mailed else ""

    return press


def _configured(tmp_path) -> EmailTransport:
    """A transport whose settings make it outbound-capable without a socket.

    Built through the same per-instance config overlay the registry uses, so the
    conformance run sees the object shape production builds rather than a hand-poked one.
    """
    transport = EmailTransport(
        {
            "imap_host": "imap.example.com",
            "imap_user": AGENT,
            "smtp_host": "smtp.example.com",
            "smtp_user": AGENT,
            "mailbox_address": AGENT,
        }
    )
    transport._sender_factory = lambda *a, **kw: FakeSmtpServer()
    return transport


def test_email_transport_meets_the_channel_contract(tmp_path, monkeypatch):
    monkeypatch.setenv(owner_id_credential("email"), OWNER)
    smtp = FakeSmtpServer()
    delivery = EmailDelivery(smtp, AGENT, owner_id=AGENT)
    assert_channel_contract(
        _configured(tmp_path),
        delivery=delivery,
        # No min_edit_interval/clock: email declares edits=False, so the kit asserts the
        # MUST-NOT half (start_stream returns "") instead of a throttle.
        inbound_via="_dispatch",
        press=_reply(delivery, smtp),
    )


def test_unconfigured_transport_also_conforms():
    """No IMAP/SMTP config at all ⇒ offline, send() returns False rather than raising."""
    assert_channel_contract(EmailTransport({}), inbound_via="_dispatch")


def test_the_kit_catches_a_channel_that_pretends_to_stream():
    """Guard against a vacuous green.

    A delivery that hands back a stream ts while the transport declares ``edits=False``
    leaves core animating into a message email can never edit — the exact violation the
    negative streaming clause exists to catch.
    """

    class PretendsToStream(EmailDelivery):
        async def start_stream(self, channel, thread_ts="", initial_text=""):
            return "1"

    with pytest.raises(ChannelContractError, match=r"\[streaming\].*MUST return"):
        assert_channel_contract(
            EmailTransport({}),
            delivery=PretendsToStream(FakeSmtpServer(), AGENT, owner_id=AGENT),
            inbound_via="_dispatch",
        )


def test_the_kit_catches_a_late_reply_told_nothing_of_how_it_ended(tmp_path, monkeypatch):
    """The approvals clause reaches this app: a delivery that answers every late reply alike
    is named by the kit."""
    monkeypatch.setenv(owner_id_credential("email"), OWNER)

    class OneAnswer(EmailDelivery):
        def _answer_late(self, ended):  # type: ignore[no-untyped-def]
            super()._answer_late(ended._replace(outcome=""))

    smtp = FakeSmtpServer()
    delivery = OneAnswer(smtp, AGENT, owner_id=AGENT)
    with pytest.raises(ChannelContractError, match=r"\[approvals\].*answered alike"):
        assert_channel_contract(
            _configured(tmp_path),
            delivery=delivery,
            inbound_via="_dispatch",
            press=_reply(delivery, smtp),
        )
