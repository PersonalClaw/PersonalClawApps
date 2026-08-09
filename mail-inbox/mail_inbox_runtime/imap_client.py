"""A thin IMAP client the provider polls — a seam over ``imaplib`` so the provider
logic (checkpointing, allowlist, MIME) is testable without a live server.

The provider depends on the small :class:`ImapClient` protocol, not on ``imaplib``
directly, so a test injects a fake that returns canned UIDs + RFC822 bytes. The real
:class:`Imap4Client` wraps ``imaplib.IMAP4`` / ``IMAP4_SSL``. IMAP reliability (idle
timeouts, provider throttling, folder quirks) is the app's problem, not core's — which
is exactly why mail lives in an app.

UID semantics — the resume contract (T2.1):

- ``fetch_uids_since(folder, last_uid)`` returns the message UIDs in ``folder`` with
  UID **strictly greater** than ``last_uid`` (``last_uid=0`` ⇒ every UID). IMAP UIDs
  are monotonic within a folder's UIDVALIDITY, so the highest UID seen is the cursor a
  restart resumes from — no reprocessing, no skipping.
- ``fetch_message(folder, uid)`` returns the raw RFC822 bytes for one UID.
"""

from __future__ import annotations

import imaplib
import logging
from typing import Protocol

logger = logging.getLogger(__name__)

# imaplib's default (10_000 bytes) truncates long IMAP responses (big UID sets, large
# messages) and raises "got more than N bytes". Raise it once at import.
imaplib._MAXLINE = max(getattr(imaplib, "_MAXLINE", 0), 10_000_000)  # type: ignore[attr-defined]


class ImapClient(Protocol):
    """The narrow surface the provider needs. A fake in tests implements just this."""

    def connect(self) -> None: ...

    def fetch_uids_since(self, folder: str, last_uid: int) -> list[int]: ...

    def fetch_message(self, folder: str, uid: int) -> bytes: ...

    def close(self) -> None: ...


class ImapError(Exception):
    """Any IMAP transport/auth failure. The provider degrades on it, never crashes."""


class Imap4Client:
    """The real client — wraps ``imaplib.IMAP4_SSL`` (or plain ``IMAP4``)."""

    def __init__(
        self, host: str, port: int, username: str, password: str, *, use_ssl: bool = True
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._use_ssl = use_ssl
        self._conn: imaplib.IMAP4 | None = None

    def connect(self) -> None:
        try:
            conn: imaplib.IMAP4 = (
                imaplib.IMAP4_SSL(self._host, self._port)
                if self._use_ssl
                else imaplib.IMAP4(self._host, self._port)
            )
            conn.login(self._username, self._password)
        except (imaplib.IMAP4.error, OSError) as exc:
            raise ImapError(f"IMAP connect/login failed: {exc}") from exc
        self._conn = conn

    def _select(self, folder: str) -> None:
        if self._conn is None:
            raise ImapError("not connected")
        # readonly: never set \Seen or otherwise mutate the mailbox while polling.
        typ, _ = self._conn.select(folder, readonly=True)
        if typ != "OK":
            raise ImapError(f"IMAP select {folder!r} failed: {typ}")

    def fetch_uids_since(self, folder: str, last_uid: int) -> list[int]:
        if self._conn is None:
            raise ImapError("not connected")
        self._select(folder)
        # UID SEARCH for the half-open range (last_uid, ∞). UID 0 is never assigned, so
        # (last_uid+1):* with last_uid=0 becomes 1:* — every message.
        start = last_uid + 1
        try:
            typ, data = self._conn.uid("SEARCH", None, f"UID {start}:*")
        except (imaplib.IMAP4.error, OSError) as exc:
            raise ImapError(f"IMAP UID SEARCH failed: {exc}") from exc
        if typ != "OK" or not data:
            return []
        raw = data[0]
        if not raw:
            return []
        text = raw.decode("ascii", errors="replace") if isinstance(raw, bytes) else str(raw)
        uids: list[int] = []
        for tok in text.split():
            try:
                uid = int(tok)
            except ValueError:
                continue
            # "start:*" always returns at least the highest UID even when none are newer,
            # so filter to strictly-greater to honor the resume contract exactly.
            if uid > last_uid:
                uids.append(uid)
        return sorted(uids)

    def fetch_message(self, folder: str, uid: int) -> bytes:
        if self._conn is None:
            raise ImapError("not connected")
        self._select(folder)
        try:
            typ, data = self._conn.uid("FETCH", str(uid), "(RFC822)")
        except (imaplib.IMAP4.error, OSError) as exc:
            raise ImapError(f"IMAP UID FETCH {uid} failed: {exc}") from exc
        if typ != "OK" or not data:
            return b""
        for part in data:
            if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], (bytes, bytearray)):
                return bytes(part[1])
        return b""

    def close(self) -> None:
        conn = self._conn
        self._conn = None
        if conn is None:
            return
        try:
            conn.logout()
        except (imaplib.IMAP4.error, OSError):
            logger.debug("mail-inbox: IMAP logout error", exc_info=True)
