"""Settings › Sender trust words Email's section by what Email declares, and a code works by its rule.

The page gave every channel a chat bot's words: a group rule, "Add the bot to a group…", and a code
to send "to your bot in a direct message". A mail to the mailbox is one person writing to the
owner, so Email declares no groups, and it says how someone it pairs sends their code: a mail to
the mailbox, from the address being let in, anywhere in the message.

A sender's code lets someone in only while Email's rule for strangers asks for one. This channel
finds the code inside the mail and redeems it itself, before the door, and core's rule now lives
in the redemption, so "only you let them in" is what it says here too.

Trust runs against the real core seam in the isolated home; IMAP and SMTP are fakes.
"""

from __future__ import annotations

import asyncio

import pytest

from personalclaw import channel_trust as ct
from personalclaw.sdk.channel import create_pairing_code, is_allowed_sender

from email_runtime.transport import EmailTransport
from test_transport import AGENT, BOB, _mail, wired  # noqa: F401 - the shared fixture


def test_email_carries_no_groups():
    assert EmailTransport().capabilities().groups is False


def test_email_says_where_someone_it_pairs_sends_their_code(wired):  # noqa: F811
    hint = wired[0].sender_pairing_hint()
    assert hint.startswith(f"Have them mail this code from the address you want to let in, to {AGENT}.")
    assert "anywhere in the message" in hint
    assert "bot" not in hint


@pytest.mark.asyncio
async def test_a_mailed_code_lets_nobody_in_while_only_you_let_them_in(wired):  # noqa: F811
    transport, imap, smtp, _, captured = wired
    ct.set_trust_policies("email", dm="owner_only")
    code = create_pairing_code("email")
    _mail(1, imap, plain=f"Hi,\n\nthe code you gave me: {code}\n\n-- \nBob\n")
    await transport._poll_once(transport._settings())
    await asyncio.sleep(0)
    assert is_allowed_sender("email", BOB) is False
    to_bob = [m for m in smtp.sent if BOB in str(m.get("To", ""))]
    assert to_bob == [], "nothing goes to a stranger from the owner's mailbox"
    assert "text" not in captured, "a stranger's mail is no turn"


@pytest.mark.asyncio
async def test_a_mailed_code_lets_them_in_while_the_rule_asks_for_one(wired):  # noqa: F811
    """The floor for the refusal above: the same mail, under the rule that takes codes."""
    transport, imap, smtp, _, _ = wired
    code = create_pairing_code("email")
    _mail(1, imap, plain=f"Hi,\n\nthe code you gave me: {code}\n\n-- \nBob\n")
    await transport._poll_once(transport._settings())
    await asyncio.sleep(0)
    assert is_allowed_sender("email", BOB) is True
    assert "Paired" in smtp.body_text()
