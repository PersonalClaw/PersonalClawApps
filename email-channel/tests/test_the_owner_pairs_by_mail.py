"""The owner pairs Email from its Configure page: the code, mailed from the owner's address.

An approval asked by mail goes to the channel's owner, and Email had no way to have one: its
Configure page said it could not pair its owner, so the owner id was a variable set by hand.
Email offers owner pairing now. The code the page shows is mailed to the mailbox from the owner's
own address, anywhere in the message (under a greeting, above a signature), and the page says so.
The sender becomes the channel's owner, the address core asks approvals at, by core's rules
(``redeem_owner_pairing_code``): trusted, the code spent, and a wrong code-shaped word counted
against the five wrong guesses.

Trust runs against the real core seam in the isolated home; IMAP and SMTP are fakes.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from personalclaw import channel_trust as ct
from personalclaw.sdk.channel import (
    allow_sender,
    create_pairing_code,
    is_allowed_sender,
    owner_id_credential,
    owner_id_for,
)

from test_transport import AGENT, BOB, _mail, wired  # noqa: F401 - the shared fixture

_KEY = owner_id_credential("email")


@pytest.fixture(autouse=True)
def no_owner_yet():
    """No owner before a test, and none left after it: a saved credential is mirrored into
    the environment, which outlives the test's home."""
    os.environ.pop(_KEY, None)
    yield
    os.environ.pop(_KEY, None)


def _attempts_left() -> int:
    return int(ct.owner_pairing_status("email")["attempts_left"])


def test_email_offers_owner_pairing_and_says_how_the_code_is_sent(wired):  # noqa: F811
    transport = wired[0]
    assert transport.capabilities().owner_pairing is True
    hint = transport.owner_pairing_hint()
    assert hint.startswith(f"Mail this code to {AGENT} from the address that should get")
    assert "anywhere in the message" in hint


@pytest.mark.asyncio
async def test_the_owners_code_in_a_mail_makes_its_sender_the_owner(wired):  # noqa: F811
    transport, imap, smtp, _, captured = wired
    code = ct.create_owner_pairing_code("email")
    _mail(1, imap, plain=f"Hi,\n\nhere is the code: {code}\n\n-- \nBob\n")
    await transport._poll_once(transport._settings())
    await asyncio.sleep(0)
    assert owner_id_for("email") == BOB
    assert is_allowed_sender("email", BOB) is True
    assert ct.owner_pairing_status("email")["ended"] == "paired"
    assert "you're my owner here now" in smtp.body_text()
    assert "text" not in captured  # the pairing mail is not a turn


@pytest.mark.asyncio
async def test_a_correspondent_already_allowed_can_become_the_owner(wired):  # noqa: F811
    transport, imap, _, _, captured = wired
    allow_sender("email", BOB)
    code = ct.create_owner_pairing_code("email")
    _mail(1, imap, plain=f"{code}")
    await transport._poll_once(transport._settings())
    await asyncio.sleep(0)
    assert owner_id_for("email") == BOB
    assert "text" not in captured


@pytest.mark.asyncio
async def test_a_wrong_code_pairs_nobody_and_counts_as_a_guess(wired):  # noqa: F811
    transport, imap, _, _, _ = wired
    code = ct.create_owner_pairing_code("email")
    wrong = "12345678" if code != "12345678" else "87654321"
    before = _attempts_left()
    _mail(1, imap, plain=f"is it {wrong}?")
    await transport._poll_once(transport._settings())
    assert owner_id_for("email") == ""
    assert _attempts_left() == before - 1
    # The floor: the right code, the same way, pairs.
    _mail(2, imap, plain=f"then it is {code}")
    await transport._poll_once(transport._settings())
    assert owner_id_for("email") == BOB


@pytest.mark.asyncio
async def test_a_senders_code_pairs_a_sender_and_costs_the_owners_code_nothing(wired):  # noqa: F811
    transport, imap, _, _, _ = wired
    ct.create_owner_pairing_code("email")
    sender_code = create_pairing_code("email")
    before = _attempts_left()
    _mail(1, imap, plain=f"code {sender_code}")
    await transport._poll_once(transport._settings())
    assert is_allowed_sender("email", BOB) is True
    assert owner_id_for("email") == ""
    assert _attempts_left() == before


@pytest.mark.asyncio
async def test_mail_from_the_mailbox_itself_pairs_no_owner(wired):  # noqa: F811
    """The mailbox's own copies are dropped unread, so it can never become its own owner."""
    transport, imap, _, _, _ = wired
    code = ct.create_owner_pairing_code("email")
    _mail(1, imap, from_addr=AGENT, plain=f"{code}")
    await transport._poll_once(transport._settings())
    assert owner_id_for("email") == ""
    assert ct.owner_pairing_status("email")["active"] is True
