"""A first poll surfaces none of the mail that was already there.

The UID cursor started at 0, so the first poll after setup surfaced the whole folder: every
allowed sender's message became an inbox item, and each item raises an inbox event, so the
owner's inbox automations (and a prompt-bound address's stored prompt) fired for mail that
arrived before PersonalClaw was connected. A first poll now records the folder's newest UID as
the cursor and surfaces nothing; mail that arrives after it is the source's. Email Channel's
first connection does the same.

The last test runs the provider under core's REAL inbox service, the caller that persists the
cursor, to prove the start survives it: a start only the provider remembered would re-baseline
every cycle and surface nothing, ever.
"""

from __future__ import annotations

import asyncio

import pytest

from mail_inbox_runtime.imap_client import ImapError
from mail_inbox_runtime.provider import MailInboxProvider
from mail_inbox_runtime.settings import MailInboxSettings, _APP

from _fakes import FakeImapClient, build_message

FOLDER = "INBOX"


def _configure(**over) -> None:
    from personalclaw.sdk.settings import ProviderSettings

    cfg = {
        "host": "imap.example.com",
        "port": 993,
        "username": "me@example.com",
        "address": "me@example.com",
        "folder": FOLDER,
        "allow_senders": ["*@example.com"],
        "password": "secret",
    }
    cfg.update(over)
    ProviderSettings.update(_APP, cfg)


def _seeded() -> dict[int, bytes]:
    """Eleven messages from allowed senders: without the fix, every one of them surfaces."""
    return {
        uid: build_message(
            from_addr=f"person{uid}@example.com",
            message_id=f"<seed{uid}@example.com>",
            plain=f"message {uid}",
            date=f"Mon, 09 Aug 2026 10:{uid:02d}:00 +0000",
        )
        for uid in range(1, 12)
    }


def _new_mail() -> bytes:
    return build_message(
        from_addr="new@example.com", message_id="<new@example.com>", plain="after setup",
        date="Tue, 10 Aug 2026 09:00:00 +0000",
    )


def _key() -> str:
    return MailInboxProvider._checkpoint_key(MailInboxSettings.load())


def _vkey() -> str:
    return MailInboxProvider._validity_key(MailInboxSettings.load())


def _poll(client: FakeImapClient, checkpoints: dict[str, str]):
    provider = MailInboxProvider()
    provider._client_factory = lambda settings, password: client
    return asyncio.run(provider.poll([], checkpoints, "me@example.com"))


def test_the_first_poll_surfaces_none_of_the_mail_already_there():
    _configure()
    client = FakeImapClient({FOLDER: _seeded()})
    messages, checkpoints = _poll(client, {})
    assert messages == []
    assert client.fetch_calls == [], "a first poll reads no message"
    assert client.newest_calls == [FOLDER]
    assert checkpoints == {_key(): "11"}


def test_mail_after_the_first_poll_is_surfaced():
    """The floor: the source still surfaces what arrives once it is connected."""
    _configure()
    folder = _seeded()
    client = FakeImapClient({FOLDER: folder})
    _, checkpoints = _poll(client, {})

    folder[12] = _new_mail()
    messages, checkpoints = _poll(client, checkpoints)
    assert [m.id for m in messages] == ["<new@example.com>"]
    assert client.fetch_calls == [12]
    assert checkpoints == {_key(): "12"}


def test_a_first_poll_that_cannot_connect_records_no_start():
    """Nothing was read, so nothing is recorded: the poll raises (its sentence is the inbox's
    health for this source) and core keeps the checkpoints it had. A missing cursor must never
    become 0 ("surface everything"), so the next poll that connects starts after the newest."""
    _configure()
    client = FakeImapClient({FOLDER: _seeded()})
    client.fail_connect = True
    with pytest.raises(ImapError, match="is unreachable"):
        _poll(client, {})

    client.fail_connect = False
    messages, checkpoints = _poll(client, {})
    assert messages == [] and checkpoints == {_key(): "11"}


def test_an_empty_folder_starts_at_zero_and_surfaces_its_first_mail():
    _configure()
    folder: dict[int, bytes] = {}
    client = FakeImapClient({FOLDER: folder})
    _, checkpoints = _poll(client, {})
    assert checkpoints == {_key(): "0"}

    folder[1] = _new_mail()
    messages, _ = _poll(client, checkpoints)
    assert [m.id for m in messages] == ["<new@example.com>"]


def test_another_folder_is_a_first_poll_of_its_own():
    """The cursor is per (account, folder): pointing the source at another folder must not
    surface that folder's backlog either."""
    _configure()
    client = FakeImapClient({FOLDER: _seeded(), "Receipts": _seeded()})
    _, checkpoints = _poll(client, {})

    _configure(folder="Receipts")
    messages, checkpoints = _poll(client, checkpoints)
    assert messages == []
    assert checkpoints == {
        "mailuid:me@example.com:INBOX": "11",
        "mailuid:me@example.com:Receipts": "11",
    }


def test_core_keeps_the_start_so_the_next_poll_surfaces_new_mail():
    from personalclaw.inbox import InboxState
    from personalclaw.inbox_service import InboxService

    _configure()
    folder = _seeded()
    client = FakeImapClient({FOLDER: folder})
    provider = MailInboxProvider()
    provider._client_factory = lambda settings, password: client
    service = InboxService(sources=lambda: [provider])

    asyncio.run(service._poll_once())
    assert service.inbox.items == {}, "mail that was already there became inbox items"
    persisted = InboxState()
    persisted.load()
    assert persisted.last_read_ts.get(_key()) == "11"

    folder[12] = _new_mail()
    asyncio.run(service._poll_once())
    assert [item.sender_id for item in service.inbox.items.values()] == ["new@example.com"]


# ── UIDVALIDITY: a renumbered folder starts again after its newest message ──


def test_the_folders_numbering_is_recorded_with_its_start():
    _configure()
    client = FakeImapClient({FOLDER: _seeded()}, uidvalidity=7)
    _, checkpoints = _poll(client, {})
    assert checkpoints == {_key(): "11", _vkey(): "7"}


def test_a_renumbered_folder_starts_after_its_newest_message_again():
    """The server renumbered the folder (a restore, a migration), so the cursor is in a
    numbering that no longer exists. Read from it, UIDs 1..12 are all below 30: nothing would
    ever surface again. A cursor below the new UIDs would replay them instead. Email Channel
    starts after the newest message on a UIDVALIDITY change, and so does this source now."""
    _configure()
    folder = _seeded()
    client = FakeImapClient({FOLDER: folder}, uidvalidity=8)
    messages, checkpoints = _poll(client, {_key(): "30", _vkey(): "7"})
    assert messages == [] and client.fetch_calls == [], "a renumbered folder was read"
    assert checkpoints == {_key(): "11", _vkey(): "8"}

    folder[12] = _new_mail()
    messages, _ = _poll(client, checkpoints)
    assert [m.id for m in messages] == ["<new@example.com>"]


def test_an_unreported_numbering_is_not_a_renumbering():
    """0 is "the server did not say", which must not throw away a good cursor."""
    _configure()
    folder = _seeded()
    folder[12] = _new_mail()
    client = FakeImapClient({FOLDER: folder}, uidvalidity=0)
    messages, checkpoints = _poll(client, {_key(): "11", _vkey(): "7"})
    assert [m.id for m in messages] == ["<new@example.com>"]
    assert checkpoints == {_key(): "12", _vkey(): "7"}


def test_a_cursor_from_before_the_numbering_was_known_adopts_it():
    """A cursor recorded by an earlier version, or while the server did not report it, reads on
    and records the numbering it is in from then on."""
    _configure()
    folder = _seeded()
    folder[12] = _new_mail()
    client = FakeImapClient({FOLDER: folder}, uidvalidity=7)
    messages, checkpoints = _poll(client, {_key(): "11"})
    assert [m.id for m in messages] == ["<new@example.com>"]
    assert checkpoints == {_key(): "12", _vkey(): "7"}
