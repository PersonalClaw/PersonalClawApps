"""Imap4Client — UID SEARCH parsing and the strictly-greater resume contract.

Drives the real client against a fake ``imaplib`` connection so the UID range math
(``(last+1):*``, filtering to strictly-greater) and FETCH payload extraction are tested
without a live server.
"""

from __future__ import annotations

import pytest

from mail_inbox_runtime.imap_client import Imap4Client, ImapError


class _FakeConn:
    def __init__(self, search_result, fetch_result, *, login_ok=True):
        self._search_result = search_result
        self._fetch_result = fetch_result
        self._login_ok = login_ok
        self.selected = None
        self.readonly = None
        self.logged_out = False

    def login(self, user, pw):
        if not self._login_ok:
            import imaplib

            raise imaplib.IMAP4.error("bad auth")
        return ("OK", [b"logged in"])

    def select(self, folder, readonly=False):
        self.selected = folder
        self.readonly = readonly
        return ("OK", [b"1"])

    def uid(self, command, *args):
        if command == "SEARCH":
            return self._search_result
        if command == "FETCH":
            return self._fetch_result
        return ("NO", [])

    def logout(self):
        self.logged_out = True
        return ("BYE", [b""])


def _client(conn):
    c = Imap4Client("h", 993, "u", "p", use_ssl=True)
    c._conn = conn  # inject the fake connection (skip the socket connect)
    return c


def test_fetch_uids_since_filters_strictly_greater():
    # "start:*" always includes the highest UID even when none are newer — the client
    # must filter to > last_uid so a restart doesn't reprocess the cursor message.
    conn = _FakeConn(search_result=("OK", [b"3 5 8"]), fetch_result=("OK", []))
    c = _client(conn)
    assert c.fetch_uids_since("INBOX", 5) == [8]
    assert conn.readonly is True  # never mutates the mailbox while polling


def test_fetch_uids_since_from_zero_returns_all():
    conn = _FakeConn(search_result=("OK", [b"1 2 3"]), fetch_result=("OK", []))
    assert _client(conn).fetch_uids_since("INBOX", 0) == [1, 2, 3]


def test_fetch_uids_since_empty():
    conn = _FakeConn(search_result=("OK", [b""]), fetch_result=("OK", []))
    assert _client(conn).fetch_uids_since("INBOX", 0) == []
    conn2 = _FakeConn(search_result=("OK", [None]), fetch_result=("OK", []))
    assert _client(conn2).fetch_uids_since("INBOX", 0) == []


def test_fetch_message_extracts_rfc822_payload():
    fetch = ("OK", [(b"1 (RFC822 {5}", b"hello"), b")"])
    assert _client(_FakeConn(("OK", []), fetch)).fetch_message("INBOX", 1) == b"hello"


def test_fetch_message_missing_returns_empty():
    assert _client(_FakeConn(("OK", []), ("OK", []))).fetch_message("INBOX", 1) == b""


def test_search_failure_raises_imap_error():
    class _Boom(_FakeConn):
        def uid(self, command, *args):
            import imaplib

            raise imaplib.IMAP4.error("search boom")

    with pytest.raises(ImapError):
        _client(_Boom(("OK", []), ("OK", []))).fetch_uids_since("INBOX", 0)


class _Folder(_FakeConn):
    """A folder holding ``exists`` messages that records every UID command."""

    def __init__(self, exists, search_result=("OK", [b""])):
        super().__init__(search_result, ("OK", []))
        self._exists = exists
        self.uid_calls: list[tuple] = []

    def select(self, folder, readonly=False):
        super().select(folder, readonly)
        return ("OK", [str(self._exists).encode()])

    def uid(self, command, *args):
        self.uid_calls.append((command, args))
        return super().uid(command, *args)


def test_newest_uid_asks_for_the_largest_uid_in_use():
    conn = _Folder(3, search_result=("OK", [b"42"]))
    assert _client(conn).newest_uid("INBOX") == 42
    assert conn.uid_calls == [("SEARCH", (None, "UID *"))]
    assert conn.readonly is True


def test_newest_uid_of_an_empty_folder_is_zero_without_a_search():
    """Servers answer ``UID *`` in an empty folder differently — some with BAD, which
    ``imaplib`` raises — so a new, empty mailbox must not depend on it to start."""
    conn = _Folder(0)
    assert _client(conn).newest_uid("INBOX") == 0
    assert conn.uid_calls == []


def test_a_select_that_loses_the_connection_is_an_imap_error():
    """The provider degrades on an ``ImapError``; anything else escaped the poll."""

    class _Dropped(_FakeConn):
        def select(self, folder, readonly=False):
            raise OSError(54, "Connection reset by peer")

    with pytest.raises(ImapError):
        _client(_Dropped(("OK", []), ("OK", []))).newest_uid("INBOX")


def test_not_connected_raises():
    c = Imap4Client("h", 993, "u", "p")
    with pytest.raises(ImapError):
        c.fetch_uids_since("INBOX", 0)
