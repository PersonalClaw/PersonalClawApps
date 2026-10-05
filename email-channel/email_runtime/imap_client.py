"""The blocking IMAP mechanics, behind a narrow protocol the transport can fake.

``imaplib`` is a **synchronous** API: every call blocks the calling thread on socket
IO. The transport therefore never calls this module from the event loop — it hands each
operation to a thread executor (``asyncio.to_thread``). A single blocking ``select()``
on the loop would stall the whole gateway (every session, every WebSocket) for as long
as the mail server takes to answer.

Three IMAP facts shape this file:

* **UID, never sequence numbers.** Sequence numbers renumber on every expunge, so a
  cursor kept in them silently skips or reprocesses mail the moment the user deletes a
  message from another client. Every command here is a ``UID`` command, and the cursor
  is a UID.
* **``UID SEARCH n:*`` always returns at least one message.** The range is inclusive
  and the server clamps ``*`` to the highest existing UID, so searching ``(last+1):*``
  when nothing is new still returns the last UID. The result is filtered to strictly
  greater than the cursor, which is what makes "no new mail" mean an empty list.
* **``imaplib`` has a line-length ceiling.** ``imaplib._MAXLINE`` bounds one response
  line; a large UID set or a big literal past it raises ``imaplib.IMAP4.error("got more
  than N bytes")``. It is raised once at import here, and every call is wrapped so the
  poll loop degrades instead of dying.

``UIDVALIDITY`` is checked on select: when a server renumbers a mailbox (a restore, a
migration) every UID becomes meaningless, and a cursor kept across that boundary would
skip the whole mailbox. The client reports the value so the transport can reset.

**The server is verified before it is trusted with the password.** ``IMAP4_SSL`` is given
:func:`~email_runtime.tls.client_context` (system CAs, host name checked, plus the
``tls_ca_file`` setting's authority), where it used to build a context that checks
nothing. With ``imap_use_ssl`` off, the plain connection is upgraded with STARTTLS through
the same context before the login, and a server that does not offer STARTTLS is refused
with the password unsent: it used to be sent in the clear, and no setting sends it that way
now. A connection failure is raised as an :class:`ImapError` whose ``kind`` says which
stage failed — ``tls``, ``unreachable`` or ``auth`` — and whose text is the sentence the
channel's health shows.
"""

from __future__ import annotations

import imaplib
import logging
import re
import ssl
from typing import Protocol

from email_runtime.tls import CaFileError, client_context, describe_failure

logger = logging.getLogger(__name__)

# imaplib's default line cap truncates long IMAP responses (big UID sets, large
# messages) and raises "got more than N bytes". Raise it once at import; the calls
# below still handle the error, because a hostile/broken server can exceed any bound.
imaplib._MAXLINE = max(getattr(imaplib, "_MAXLINE", 0), 10_000_000)  # type: ignore[attr-defined]

#: Socket timeout for every IMAP operation. Without it a half-open connection parks a
#: worker thread forever and the poll loop never fires again.
IMAP_TIMEOUT_SECS = 60

_UIDVALIDITY_RE = re.compile(rb"UIDVALIDITY\s+(\d+)", re.IGNORECASE)


class ImapError(Exception):
    """Any IMAP transport/auth/protocol failure. The caller degrades, never crashes.

    ``kind`` names the stage: ``tls`` (the certificate or the handshake), ``unreachable``
    (the host did not answer), ``auth`` (the login was refused) or ``protocol`` (a command
    after login failed)."""

    def __init__(self, message: str, *, kind: str = "protocol") -> None:
        super().__init__(message)
        self.kind = kind


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


class ImapClient(Protocol):
    """The narrow surface the transport needs. A fake in tests implements just this."""

    def connect(self) -> None: ...

    def select_folder(self, folder: str) -> int: ...

    def newest_uid(self, folder: str) -> int: ...

    def fetch_uids_since(self, folder: str, last_uid: int) -> list[int]: ...

    def fetch_message(self, folder: str, uid: int) -> bytes: ...

    def close(self) -> None: ...


class Imap4Client:
    """The real client — wraps ``imaplib.IMAP4_SSL`` (or plain ``IMAP4``).

    Every public method is BLOCKING and must be called from a thread executor."""

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
        """Open the connection, verify the server, and log in.

        Raises :class:`ImapError` on any failure, its ``kind`` naming the stage. The
        certificate is checked in the handshake (implicit TLS, or STARTTLS with
        ``imap_use_ssl`` off), so a server that fails it, or cannot upgrade, never sees the
        login."""
        conn = self._open()
        try:
            conn.login(self._username, self._password)
        except imaplib.IMAP4.error as exc:
            _logout_quietly(conn)
            raise ImapError(
                f"the IMAP server {self._host}:{self._port} refused the login for "
                f"{self._username} ({_server_text(exc)})",
                kind="auth",
            ) from exc
        except OSError as exc:
            _logout_quietly(conn)
            raise ImapError(self._describe(exc), kind="unreachable") from exc
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
            raise ImapError(self._describe(exc), kind=_stage(exc)) from exc
        where = f"the IMAP server {self._host}:{self._port}"
        if "STARTTLS" not in conn.capabilities:
            _logout_quietly(conn)
            raise ImapError(
                f"{where} doesn't offer STARTTLS, so the password was not sent: with IMAP SSL "
                "off, the connection must be upgraded to TLS before the login. Turn IMAP SSL "
                "on (usually port 993), or use a server that offers STARTTLS",
                kind="tls",
            )
        try:
            conn.starttls(ssl_context=client_context(self._ca_file))
        except OSError as exc:  # the certificate refused, the handshake failed, a CA file bad
            _logout_quietly(conn)
            raise ImapError(self._describe(exc), kind=_stage(exc)) from exc
        except imaplib.IMAP4.error as exc:
            _logout_quietly(conn)
            raise ImapError(
                f"{where} could not upgrade the connection with STARTTLS "
                f"({_server_text(exc)}), so the password was not sent",
                kind="tls",
            ) from exc
        return conn

    def select_folder(self, folder: str) -> int:
        """Select *folder* read-only and return its ``UIDVALIDITY`` (0 if unreported).

        ``readonly=True``: polling must never set ``\\Seen`` or otherwise mutate the
        user's mailbox — the mail is still unread in their client after we answer it."""
        self._select_readonly(folder)
        return self._read_uidvalidity(folder)

    def _select_readonly(self, folder: str) -> int | None:
        """SELECT *folder* read-only; return how many messages it holds (its ``EXISTS``),
        or None when the answer does not say."""
        if self._conn is None:
            raise ImapError("not connected")
        try:
            typ, data = self._conn.select(folder, readonly=True)
        except (imaplib.IMAP4.error, OSError) as exc:
            raise ImapError(f"IMAP select {folder!r} failed: {exc}") from exc
        if typ != "OK":
            raise ImapError(f"IMAP select {folder!r} failed: {typ}")
        return _exists(data)

    def _read_uidvalidity(self, folder: str) -> int:
        """``UIDVALIDITY`` for the selected folder via ``STATUS``, or 0 if unavailable.

        A server that refuses STATUS (or answers oddly) yields 0, which the transport
        reads as "unknown" and treats as unchanged — a missing value must not look like
        a renumbering and wipe a good cursor."""
        if self._conn is None:
            return 0
        try:
            typ, data = self._conn.status(f'"{folder}"', "(UIDVALIDITY)")
        except (imaplib.IMAP4.error, OSError):
            logger.debug("email: IMAP STATUS UIDVALIDITY failed", exc_info=True)
            return 0
        if typ != "OK" or not data:
            return 0
        for part in data:
            raw = part if isinstance(part, (bytes, bytearray)) else str(part).encode()
            match = _UIDVALIDITY_RE.search(bytes(raw))
            if match:
                try:
                    return int(match.group(1))
                except ValueError:
                    return 0
        return 0

    def _describe(self, exc: BaseException) -> str:
        return describe_failure(exc, protocol="IMAP", host=self._host, port=self._port)

    def newest_uid(self, folder: str) -> int:
        """The highest UID in *folder*, or 0 when it is empty.

        ``UID SEARCH UID *``: RFC 3501's ``*`` is the largest UID in use, so the answer is
        one number however large the mailbox is. The transport starts a first connection
        after it, so mail that was already there is never answered. An empty folder has no
        UID in use, and servers answer ``*`` there differently (some with BAD, which
        ``imaplib`` raises), so a folder whose SELECT reports no message is 0 without a
        search — a new, empty mailbox must be able to start."""
        if self._select_readonly(folder) == 0:
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
        uids = [int(tok) for tok in text.split() if tok.isdigit()]
        return max(uids, default=0)

    def fetch_uids_since(self, folder: str, last_uid: int) -> list[int]:
        """UIDs in *folder* strictly greater than *last_uid*, ascending.

        ``last_uid=0`` means every message (UID 0 is never assigned, so ``1:*``)."""
        if self._conn is None:
            raise ImapError("not connected")
        self.select_folder(folder)
        start = max(0, last_uid) + 1
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
            # "start:*" always returns at least the highest UID even when none are
            # newer, so filter to strictly-greater to honor the resume contract.
            if uid > last_uid:
                uids.append(uid)
        return sorted(set(uids))

    def fetch_message(self, folder: str, uid: int) -> bytes:
        """Raw RFC822 bytes for one UID (``b""`` when the server returns nothing).

        ``BODY.PEEK[]`` rather than ``RFC822``: a bare ``RFC822`` fetch sets ``\\Seen``
        on most servers even inside a read-only select on some implementations, and a
        channel must not mark the user's mail read behind their back."""
        if self._conn is None:
            raise ImapError("not connected")
        self.select_folder(folder)
        try:
            typ, data = self._conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
        except (imaplib.IMAP4.error, OSError) as exc:
            raise ImapError(f"IMAP UID FETCH {uid} failed: {exc}") from exc
        if typ != "OK" or not data:
            return b""
        for part in data:
            if not (isinstance(part, tuple) and len(part) >= 2):
                continue
            if isinstance(part[1], (bytes, bytearray)):
                return bytes(part[1])
        return b""

    def close(self) -> None:
        """Log out, swallowing the usual teardown noise. Idempotent."""
        conn = self._conn
        self._conn = None
        if conn is None:
            return
        try:
            conn.logout()
        except (imaplib.IMAP4.error, OSError):
            logger.debug("email: IMAP logout error", exc_info=True)


def _stage(exc: BaseException) -> str:
    """Which stage a connection attempt that raised *exc* failed at (:class:`ImapError`)."""
    if isinstance(exc, (ssl.SSLError, CaFileError)):
        return "tls"
    if isinstance(exc, OSError):
        return "unreachable"
    return "protocol"  # it answered, and not as an IMAP server greets


def _logout_quietly(conn: imaplib.IMAP4) -> None:
    try:
        conn.logout()
    except (imaplib.IMAP4.error, OSError):
        logger.debug("email: IMAP logout after a failed login", exc_info=True)


def probe_login(
    host: str, port: int, username: str, password: str, folder: str, *, use_ssl: bool = True,
    ca_file: str = "",
) -> tuple[bool, str]:
    """The doctor/Test probe: connect + login + SELECT the folder. BLOCKING.

    It selects as well as logging in: a login alone proves the credential but
    not that the folder we poll exists, and a wrong folder name is the second most
    common misconfiguration after a wrong password."""
    client = Imap4Client(host, port, username, password, use_ssl=use_ssl, ca_file=ca_file)
    try:
        client.connect()
        client.select_folder(folder)
        return True, f"IMAP login OK; folder {folder!r} selectable"
    except ImapError as exc:
        return False, str(exc)
    finally:
        client.close()
