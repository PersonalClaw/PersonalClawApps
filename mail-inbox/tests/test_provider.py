"""MailInboxProvider — checkpointing, dedup, fail-closed allowlist, SEL, credentials.

Covers the EIAT-2 done-when: a restart neither reprocesses nor skips (UID cursor via
poll's returned dict); a duplicate Message-ID is dropped; an unlisted sender AND an
empty allowlist both surface ZERO messages and zero events; SEL mail_sender_rejected
fires per rejection; credentials come only from the SDK credential store.
"""

from __future__ import annotations

import asyncio

from mail_inbox_runtime.provider import MailInboxProvider, create_provider
from mail_inbox_runtime.settings import CRED_MAIL_PASSWORD, _APP

from _fakes import FakeImapClient, build_message

FOLDER = "INBOX"


def _configure(allow_senders=("*@example.com",), *, password="secret"):
    """Write app settings (ProviderSettings) + the IMAP password (credential store)."""
    from personalclaw.sdk.settings import ProviderSettings

    ProviderSettings.update(
        _APP,
        {
            "host": "imap.example.com",
            "port": 993,
            "username": "me@example.com",
            "address": "me@example.com",
            "folder": FOLDER,
            "allow_senders": list(allow_senders),
        },
    )
    if password is not None:
        from personalclaw.sdk.channel import save_credential

        save_credential(CRED_MAIL_PASSWORD, password)


def _provider_with(messages):
    p = MailInboxProvider()
    client = FakeImapClient(messages)
    p._client_factory = lambda settings, password: client
    return p, client


def _poll(provider, checkpoints=None):
    return asyncio.run(provider.poll([], checkpoints or {}, "me@example.com"))


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
    _configure(allow_senders=())
    provider, client = _provider_with({FOLDER: {5: build_message()}})

    messages, checkpoints = _poll(provider)
    assert messages == []
    assert client.connected is False  # fail-closed: never even connects
    assert checkpoints == {}  # cursor untouched


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
    messages, checkpoints = _poll(provider)
    assert messages == [] and client.connected is False and checkpoints == {}


def test_unconfigured_returns_empty():
    # No settings at all.
    provider, client = _provider_with({FOLDER: {5: build_message()}})
    messages, checkpoints = _poll(provider)
    assert messages == [] and client.connected is False


def test_password_only_from_credential_store_never_settings():
    """The password must come from the credential store, never ProviderSettings."""
    from personalclaw.sdk.settings import ProviderSettings

    _configure(password=None)
    # Even if a password were (wrongly) placed in the app settings, it must be ignored.
    ProviderSettings.update(_APP, {"password": "leaked-in-settings"})
    assert MailInboxProvider._resolve_password() == ""


def test_create_provider_returns_provider():
    assert type(create_provider({})).__name__ == "MailInboxProvider"
    assert create_provider().source_name == "mail"


def test_send_reply_refuses_when_there_is_nothing_to_reply_to():
    """The outbound path exists now (EIAT-3) but stays fail-closed: with no polled message
    there is no recipient, and one is never invented from a channel id. Threading, the
    draft-by-default posture and the dry-run/live-writes rails live in
    ``test_outbound.py``."""
    p = create_provider()
    assert asyncio.run(p.send_reply("c", "hi")) is False
    assert asyncio.run(p.add_reaction("c", "1", "x")) is False


def _load_settings():
    from mail_inbox_runtime.settings import MailInboxSettings

    return MailInboxSettings.load()
