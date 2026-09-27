"""Each mail reaches the Inbox under an id of its own, so no two mails share one.

Core keys an Inbox row on the id a source gives the message (``IncomingMessage.id``). Mail
Inbox gives the mail's ``Message-ID``, and a mail with none was named by its folder and UID:
after the server renumbers the folder (its ``UIDVALIDITY`` changes, after a restore or a
migration) an old UID names another mail, which the Inbox then took for the one it already had.
A mail with no Message-ID is named by its content now. Two mails sent in the same second are
two rows either way: core used to key the row on the mail's Date, to the second, as well.
"""

from __future__ import annotations

import asyncio

from mail_inbox_runtime.provider import MailInboxProvider
from mail_inbox_runtime.settings import MailInboxSettings, _APP

from _fakes import FakeImapClient, build_message

SECOND = "Mon, 09 Aug 2026 10:00:00 +0000"


def _configure() -> None:
    from personalclaw.sdk.settings import ProviderSettings

    ProviderSettings.update(
        _APP,
        {
            "host": "imap.example.com", "port": 993, "username": "me@example.com",
            "address": "me@example.com", "folder": "INBOX",
            "allow_senders": ["*@example.com"], "password": "secret",
        },
    )


def _poll(folder: dict[int, bytes], after: int, validity: int = 7) -> list:
    """What a poll after UID ``after`` surfaces from ``folder``."""
    client = FakeImapClient({"INBOX": folder}, uidvalidity=validity)
    provider = MailInboxProvider()
    provider._client_factory = lambda settings, password: client
    settings = MailInboxSettings.load()
    checkpoints = {
        MailInboxProvider._checkpoint_key(settings): str(after),
        MailInboxProvider._validity_key(settings): str(validity),
    }
    messages, _ = asyncio.run(provider.poll([], checkpoints, "me@example.com"))
    return messages


def test_two_mails_with_no_message_id_in_the_same_second_have_two_ids():
    _configure()
    first = build_message(message_id="", subject="Invoice", plain="attached", date=SECOND)
    second = build_message(message_id="", subject="Invoice", plain="wrong one, sorry", date=SECOND)
    got = _poll({1: first, 2: second}, after=0)
    assert len(got) == 2 and got[0].id != got[1].id
    assert all(m.id.startswith("sha256:") for m in got)


def test_a_reused_uid_names_another_mail_not_the_first_one():
    """The folder renumbered: UID 1 is a different mail now, and it must not be taken for the
    one UID 1 named before."""
    _configure()
    before = _poll({1: build_message(message_id="", plain="the old one", date=SECOND)}, after=0)
    after = _poll({1: build_message(message_id="", plain="a new one", date=SECOND)}, after=0)
    assert before[0].id != after[0].id


def test_the_same_mail_read_again_keeps_its_id():
    _configure()
    raw = build_message(message_id="", plain="the same", date=SECOND)
    assert _poll({1: raw}, after=0)[0].id == _poll({1: raw}, after=0)[0].id


def test_a_mail_with_a_message_id_is_keyed_on_it():
    _configure()
    got = _poll({1: build_message(message_id="<a1@example.com>", date=SECOND)}, after=0)
    assert got[0].id == "<a1@example.com>"
