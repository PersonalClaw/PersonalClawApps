"""EmailTransport against core's channel conformance kit (CE-6 / T7.1).

The kit is the ONE executable statement of the channel contract, shipped by core and
imported through ``personalclaw.sdk.channel``. Email is the channel that exercises the
kit's *negative* streaming clause: it declares ``edits=False`` (§C3 "streaming MUST-NOT"),
so instead of a throttle the kit asserts that ``start_stream`` returns ``""`` — core reads
``await start_stream(...) or ""`` and would otherwise begin an animation this channel can
never update.

The rest is the shared floor: connect/send echo shapes, capability-dict completeness,
health/test shapes, the unknown-sender flow, and non-owner content entering a session
FENCED. Email-specific behaviour (MIME threading headers, UIDVALIDITY recovery, the
reply-token approval) stays in this bundle's other test modules.
"""

from __future__ import annotations

import pytest

from personalclaw.sdk.channel import ChannelContractError, assert_channel_contract

from email_runtime.delivery import EmailDelivery
from email_runtime.transport import EmailTransport

from _fakes import FakeSmtpServer

AGENT = "agent@example.com"


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


def test_email_transport_meets_the_channel_contract(tmp_path):
    transport = _configured(tmp_path)
    assert_channel_contract(
        transport,
        delivery=EmailDelivery(FakeSmtpServer(), AGENT, owner_id=AGENT),
        # No min_edit_interval/clock: email declares edits=False, so the kit asserts the
        # MUST-NOT half (start_stream returns "") instead of a throttle.
        inbound_via="_dispatch",
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
