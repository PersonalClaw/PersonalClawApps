"""MailInboxProvider — checkpointing, dedup, fail-closed allowlist, SEL, credentials.

Covers the acceptance criteria: a restart neither reprocesses nor skips (UID cursor via
poll's returned dict); a duplicate Message-ID is dropped; an unlisted sender AND an
empty allowlist both surface ZERO messages and zero events; SEL mail_sender_rejected
fires per rejection; the password is kept in the credential store, never in the settings file.
"""

from __future__ import annotations

import asyncio

import pytest

from mail_inbox_runtime.imap_client import ImapError
from mail_inbox_runtime.provider import MailInboxProvider, NotSetUp, create_provider
from mail_inbox_runtime.settings import _APP

from _fakes import FakeImapClient, build_message

FOLDER = "INBOX"


def _configure(allow_senders=("*@example.com",), *, password="secret"):
    """Write app settings, the IMAP password included, the way the Configure form does."""
    from personalclaw.sdk.settings import ProviderSettings

    cfg = {
        "host": "imap.example.com",
        "port": 993,
        "username": "me@example.com",
        "address": "me@example.com",
        "folder": FOLDER,
        "allow_senders": list(allow_senders),
    }
    if password is not None:
        cfg["password"] = password
    ProviderSettings.update(_APP, cfg)


def _provider_with(messages):
    p = MailInboxProvider()
    client = FakeImapClient(messages)
    p._client_factory = lambda settings, password: client
    return p, client


def _polled_before() -> dict[str, str]:
    """A mailbox this source has polled before, whose folder was empty then — where every
    steady-state test starts. A first poll (no cursor at all) is ``test_first_poll.py``'s."""
    return {MailInboxProvider._checkpoint_key(_load_settings()): "0"}


def _poll(provider, checkpoints=None):
    cursor = _polled_before() if checkpoints is None else checkpoints
    return asyncio.run(provider.poll([], cursor, "me@example.com"))


def test_poll_surfaces_allowlisted_message():
    _configure()
    raw = build_message(from_addr="sender@example.com", subject="Hi", plain="the body")
    provider, _ = _provider_with({FOLDER: {5: raw}})

    messages, checkpoints = _poll(provider)

    assert len(messages) == 1
    m = messages[0]
    assert m.sender_id == "sender@example.com"
    assert m.channel_id == "me@example.com"
    assert "the body" in m.text and "Subject: Hi" in m.text
    # A mail is an `email` row: the Inbox's Email filter, its icon and its label.
    assert m.kind == "email"
    # The UID cursor is carried in the returned checkpoint dict.
    assert checkpoints[MailInboxProvider._checkpoint_key(_load_settings())] == "5"


def test_restart_neither_reprocesses_nor_skips():
    _configure()
    msgs = {FOLDER: {5: build_message(message_id="<a@x>"), 6: build_message(message_id="<b@x>")}}
    provider, client = _provider_with(msgs)

    first, checkpoints = _poll(provider)
    assert len(first) == 2
    assert client.fetch_calls == [5, 6]

    # Simulate a restart: fresh provider, SAME checkpoint dict handed back in.
    provider2, client2 = _provider_with(msgs)
    second, checkpoints2 = _poll(provider2, checkpoints)
    assert second == []  # nothing reprocessed
    assert client2.fetch_calls == []  # UID SEARCH returned nothing past the cursor

    # A newer message arrives — it is surfaced, older ones are NOT.
    msgs[FOLDER][7] = build_message(message_id="<c@x>")
    provider3, _ = _provider_with(msgs)
    third, _ = _poll(provider3, checkpoints2)
    assert len(third) == 1 and third[0].id == "<c@x>"


def test_duplicate_message_id_is_dropped():
    _configure()
    # Same Message-ID under two different UIDs (e.g. Gmail label copies).
    dup = "<dup@example.com>"
    msgs = {FOLDER: {5: build_message(message_id=dup), 6: build_message(message_id=dup)}}
    provider, _ = _provider_with(msgs)

    messages, _ = _poll(provider)
    assert len(messages) == 1  # the second copy is deduped on Message-ID


def test_empty_allowlist_surfaces_nothing_and_never_connects():
    """Fail closed, and say so: the poll raises the sentence the inbox shows for this source,
    so an empty Allowed Senders no longer reads as a quiet mailbox."""
    _configure(allow_senders=())
    provider, client = _provider_with({FOLDER: {5: build_message()}})

    with pytest.raises(NotSetUp) as refused:
        _poll(provider, {})
    assert str(refused.value) == (
        "Allowed Senders is empty, so it reads no mail: it surfaces only mail from the senders "
        "listed there. Add them in Mail Inbox's Configure form"
    )
    assert client.connected is False  # fail-closed: never even connects


def test_unlisted_sender_is_rejected_with_sel_event():
    _configure(allow_senders=("allowed@example.com",))
    raw = build_message(from_addr="stranger@evil.test")
    provider, _ = _provider_with({FOLDER: {5: raw}})

    from personalclaw.sel import sel

    before = len(sel().recent(limit=100))
    messages, _ = _poll(provider)
    assert messages == []  # not surfaced

    events = sel().recent(limit=100)
    rejections = [e for e in events if e.get("operation") == "mail_sender_rejected"]
    assert len(rejections) == 1
    assert "stranger@evil.test" in rejections[0].get("resources", "")
    assert len(events) > before


def test_rejected_sender_still_advances_cursor():
    """A rejected message is PROCESSED — the cursor advances so a restart doesn't refetch
    it forever (and re-log the rejection each cycle)."""
    _configure(allow_senders=("allowed@example.com",))
    provider, _ = _provider_with({FOLDER: {9: build_message(from_addr="no@evil.test")}})
    _, checkpoints = _poll(provider)
    assert checkpoints[MailInboxProvider._checkpoint_key(_load_settings())] == "9"


def test_no_password_does_not_poll():
    _configure(password=None)  # settings written, but no credential
    provider, client = _provider_with({FOLDER: {5: build_message()}})
    with pytest.raises(NotSetUp) as refused:
        _poll(provider, {})
    assert str(refused.value) == "no IMAP password is saved. Enter it in Mail Inbox's Configure form"
    assert client.connected is False


def test_an_unconfigured_source_says_what_is_missing():
    # No settings at all.
    provider, client = _provider_with({FOLDER: {5: build_message()}})
    with pytest.raises(NotSetUp) as refused:
        _poll(provider)
    assert str(refused.value) == (
        "its IMAP Host and Username aren't set yet. Set them in Mail Inbox's Configure form"
    )
    assert client.connected is False


# ── a poll that cannot read the mailbox fails with its sentence ──


def test_a_poll_that_cannot_reach_the_server_raises_its_sentence():
    """It logged a warning and returned nothing, so a wrong password or a server that is down
    read exactly like a quiet mailbox. Core shows the sentence as this source's health."""
    _configure()
    provider, client = _provider_with({FOLDER: {5: build_message()}})
    client.fail_connect = True
    with pytest.raises(ImapError) as failed:
        _poll(provider)
    assert str(failed.value) == client.UNREACHABLE


def test_a_message_the_server_returns_empty_says_the_mail_after_it_waits():
    _configure()
    provider, _ = _provider_with({FOLDER: {5: b"", 6: build_message()}})
    with pytest.raises(ImapError) as failed:
        _poll(provider)
    assert str(failed.value) == (
        "the IMAP server imap.example.com:993 returned nothing for message 5 in INBOX, so the "
        "mail after it waits"
    )


def test_an_empty_message_after_new_mail_keeps_what_was_read():
    """What was read before it is surfaced and the cursor stops at it: the next poll starts
    there, and says so if the server still returns nothing for it."""
    _configure()
    provider, _ = _provider_with({FOLDER: {5: build_message(), 6: b""}})
    messages, checkpoints = _poll(provider)
    assert len(messages) == 1
    assert checkpoints[MailInboxProvider._checkpoint_key(_load_settings())] == "5"


def test_the_password_setting_is_kept_in_the_credential_store_not_the_settings_file():
    """The source reads the password it was configured with, and the settings file holds only
    a reference to it."""
    from personalclaw.sdk.settings import ProviderSettings

    _configure(password="kept-in-the-credential-store")

    assert MailInboxProvider._resolve_password() == "kept-in-the-credential-store"
    stored = ProviderSettings.config_path(_APP).read_text()
    assert "kept-in-the-credential-store" not in stored
    assert "{{secret:PCSECRET_APP_" in stored


def test_create_provider_returns_provider():
    assert type(create_provider({})).__name__ == "MailInboxProvider"
    assert create_provider().source_name == "mail"


def test_send_reply_refuses_when_there_is_nothing_to_reply_to():
    """The outbound path exists now but stays fail-closed: with no polled message
    there is no recipient, and one is never invented from a channel id. The result is falsy,
    and says why to the owner who pressed Send. Threading, the draft-by-default posture and
    the dry-run/live-writes rails live in ``test_outbound.py``."""
    p = create_provider()
    result = asyncio.run(p.send_reply("c", "hi"))
    assert not result
    assert str(result) == "it has no record of that mail, so there is no one to reply to."
    assert asyncio.run(p.add_reaction("c", "1", "x")) is False


def test_the_source_is_called_mail_inbox_in_the_inbox():
    assert create_provider().display_name == "Mail Inbox"


def _load_settings():
    from mail_inbox_runtime.settings import MailInboxSettings

    return MailInboxSettings.load()
