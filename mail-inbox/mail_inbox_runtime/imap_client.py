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
- ``newest_uid(folder)`` is the highest UID now in ``folder``: a source's first poll starts
  after it, so the mail already there never surfaces (nor fires an inbox automation).
- ``select_folder(folder)`` selects it and returns its ``UIDVALIDITY`` (0 when the server
  does not say). When a server renumbers a mailbox (a restore, a migration) every UID
  changes meaning, and a cursor kept across that would skip the whole mailbox, so the
  provider starts after the newest message again. Email Channel does the same.

**The server is verified before it is trusted with the password.** ``IMAP4_SSL`` is given
:func:`~mail_inbox_runtime.tls.client_context` (system CAs, host name checked, plus the
``tls_ca_file`` setting's authority), where it used to build a context that checks nothing.
With Use SSL off, the connection is upgraded with STARTTLS through the same context before
the login, and a server that does not offer STARTTLS is refused with the password unsent: it
used to be sent in the clear. There is no setting that sends it in the clear.

Every operation has a socket timeout (:data:`IMAP_TIMEOUT_SECS`), so a server that stops
answering fails the poll with a sentence instead of parking it for good.
"""

from __future__ import annotations

import imaplib
import logging
import re
from typing import Protocol

from mail_inbox_runtime.tls import client_context, describe_failure

logger = logging.getLogger(__name__)

# imaplib's default (10_000 bytes) truncates long IMAP responses (big UID sets, large
# messages) and raises "got more than N bytes". Raise it once at import.
imaplib._MAXLINE = max(getattr(imaplib, "_MAXLINE", 0), 10_000_000)  # type: ignore[attr-defined]

#: Socket timeout for every IMAP operation. Without it a half-open connection parks the
#: poll's worker thread forever, and the inbox never hears from this source again.
IMAP_TIMEOUT_SECS = 60

_UIDVALIDITY_RE = re.compile(rb"UIDVALIDITY\s+(\d+)", re.IGNORECASE)


class ImapClient(Protocol):
    """The narrow surface the provider needs. A fake in tests implements just this."""

    def connect(self) -> None: ...

    def select_folder(self, folder: str) -> int: ...

    def newest_uid(self, folder: str) -> int: ...

    def fetch_uids_since(self, folder: str, last_uid: int) -> list[int]: ...

    def fetch_message(self, folder: str, uid: int) -> bytes: ...

    def close(self) -> None: ...


class ImapError(Exception):
    """Any IMAP transport/auth failure, as the sentence the inbox shows for this source."""


def _exists(data: object) -> int | None:
    """The message count a SELECT answered (``imaplib`` returns ``[b"<EXISTS>"]``)."""
    raw = data[0] if isinstance(data, (list, tuple)) and data else None
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _server_text(exc: BaseException) -> str:
    """What the server said, as text: ``imaplib`` raises its reply as bytes."""
    raw = exc.args[0] if exc.args else exc
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw).decode("utf-8", errors="replace")
    return str(raw)


class Imap4Client:
    """The real client — wraps ``imaplib.IMAP4_SSL`` (or plain ``IMAP4``)."""

    def __init__(
        self, host: str, port: int, username: str, password: str, *, use_ssl: bool = True,
        ca_file: str = "",
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._use_ssl = use_ssl
        self._ca_file = ca_file
        self._conn: imaplib.IMAP4 | None = None

    def connect(self) -> None:
        """Open the connection, verify the server, and log in. The certificate is checked
        in the handshake (implicit TLS, or STARTTLS with Use SSL off), so a server that fails
        it, or cannot upgrade, never sees the login."""
        conn = self._open()
        try:
            conn.login(self._username, self._password)
        except (imaplib.IMAP4.error, OSError) as exc:
            _logout_quietly(conn)
            raise ImapError(
                f"the IMAP server {self._host}:{self._port} refused the login for "
                f"{self._username} ({_server_text(exc)})"
            ) from exc
        self._conn = conn

    def _open(self) -> imaplib.IMAP4:
        """A connection the password may be sent on: implicit TLS, or plain IMAP upgraded
        with STARTTLS. Nothing else is returned, so nothing else is logged in on."""
        try:
            if self._use_ssl:
                return imaplib.IMAP4_SSL(
                    self._host, self._port, ssl_context=client_context(self._ca_file),
                    timeout=IMAP_TIMEOUT_SECS,
                )
            conn = imaplib.IMAP4(self._host, self._port, timeout=IMAP_TIMEOUT_SECS)
        except (imaplib.IMAP4.error, OSError) as exc:
            raise ImapError(self._describe(exc)) from exc
        where = f"the IMAP server {self._host}:{self._port}"
        if "STARTTLS" not in conn.capabilities:
            _logout_quietly(conn)
            raise ImapError(
                f"{where} doesn't offer STARTTLS, so the password was not sent: with Use SSL "
                "off, the connection must be upgraded to TLS before the login. Turn Use SSL "
                "on (usually port 993), or use a server that offers STARTTLS"
            )
        try:
            conn.starttls(ssl_context=client_context(self._ca_file))
        except OSError as exc:  # the certificate refused, the handshake failed, a CA file bad
            _logout_quietly(conn)
            raise ImapError(self._describe(exc)) from exc
        except imaplib.IMAP4.error as exc:
            _logout_quietly(conn)
            raise ImapError(
                f"{where} could not upgrade the connection with STARTTLS "
                f"({_server_text(exc)}), so the password was not sent"
            ) from exc
        return conn

    def _describe(self, exc: BaseException) -> str:
        return describe_failure(exc, protocol="IMAP", host=self._host, port=self._port)

    def select_folder(self, folder: str) -> int:
        """Select *folder* read-only and return its ``UIDVALIDITY`` (0 if unreported).

        A server that refuses STATUS (or answers oddly) yields 0, which the provider reads as
        "unknown" and treats as unchanged: a missing value must not look like a renumbering
        and throw away a good cursor. (Email Channel's ``select_folder``, the same rule.)"""
        self._select(folder)
        assert self._conn is not None
        try:
            typ, data = self._conn.status(f'"{folder}"', "(UIDVALIDITY)")
        except (imaplib.IMAP4.error, OSError):
            logger.debug("mail-inbox: IMAP STATUS UIDVALIDITY failed", exc_info=True)
            return 0
        if typ != "OK" or not data:
            return 0
        for part in data:
            raw = part if isinstance(part, (bytes, bytearray)) else str(part).encode()
            match = _UIDVALIDITY_RE.search(bytes(raw))
            if match:
                return int(match.group(1))
        return 0

    def _select(self, folder: str) -> int | None:
        """SELECT *folder*; return how many messages it holds (``EXISTS``), or None when
        the answer does not say."""
        if self._conn is None:
            raise ImapError("not connected")
        # readonly: never set \Seen or otherwise mutate the mailbox while polling.
        try:
            typ, data = self._conn.select(folder, readonly=True)
        except (imaplib.IMAP4.error, OSError) as exc:
            raise ImapError(f"IMAP select {folder!r} failed: {exc}") from exc
        if typ != "OK":
            raise ImapError(f"IMAP select {folder!r} failed: {typ}")
        return _exists(data)

    def newest_uid(self, folder: str) -> int:
        """The highest UID in *folder*, or 0 when it is empty (``UID SEARCH UID *``).

        Servers answer ``*`` in an empty folder differently (some with BAD, which
        ``imaplib`` raises), so a folder whose SELECT reports no message is 0 without a
        search — a new, empty mailbox must be able to start."""
        if self._select(folder) == 0:
            return 0
        assert self._conn is not None
        try:
            typ, data = self._conn.uid("SEARCH", None, "UID *")
        except (imaplib.IMAP4.error, OSError) as exc:
            raise ImapError(f"IMAP UID SEARCH failed: {exc}") from exc
        if typ != "OK" or not data or not data[0]:
            return 0
        raw = data[0]
        text = raw.decode("ascii", errors="replace") if isinstance(raw, bytes) else str(raw)
        return max((int(tok) for tok in text.split() if tok.isdigit()), default=0)

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
        if conn is not None:
            _logout_quietly(conn)


def _logout_quietly(conn: imaplib.IMAP4) -> None:
    """Say goodbye to a connection that is being given up on; its errors are not news."""
    try:
        conn.logout()
    except (imaplib.IMAP4.error, OSError):
        logger.debug("mail-inbox: IMAP logout error", exc_info=True)
