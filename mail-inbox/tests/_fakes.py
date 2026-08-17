"""Test doubles: a fake ImapClient, a fake SmtpSender, and an RFC822 message builder.

Named ``_fakes`` (leading underscore, not ``test_*``) so pytest doesn't collect it and
the boundary lint still skips it — it imports no core, only stdlib.
"""

from __future__ import annotations

from email.message import EmailMessage


class FakeImapClient:
    """An in-memory IMAP client: maps folder → {uid: raw_bytes}. Implements the narrow
    ImapClient protocol the provider depends on (connect/fetch_uids_since/fetch_message/
    close), so a test injects it via ``provider._client_factory``."""

    def __init__(self, messages: dict[str, dict[int, bytes]]) -> None:
        self._messages = messages
        self.connected = False
        self.closed = False
        self.fetch_calls: list[int] = []

    def connect(self) -> None:
        self.connected = True

    def fetch_uids_since(self, folder: str, last_uid: int) -> list[int]:
        return sorted(u for u in self._messages.get(folder, {}) if u > last_uid)

    def fetch_message(self, folder: str, uid: int) -> bytes:
        self.fetch_calls.append(uid)
        return self._messages.get(folder, {}).get(uid, b"")

    def close(self) -> None:
        self.closed = True


class FakeSmtpSender:
    """An in-memory SMTP sender: captures the composed ``EmailMessage`` instead of putting
    it on a socket. Implements the narrow ``SmtpSender`` protocol (``send``), so a test
    injects it via ``provider._sender_factory``.

    ``sent`` staying EMPTY is the assertion that matters for draft-by-default: no message
    left the machine. ``error`` makes the real transport-failure path testable."""

    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[EmailMessage] = []
        self.error = error

    def send(self, msg: EmailMessage) -> None:
        if self.error is not None:
            raise self.error
        self.sent.append(msg)


def build_message(
    *,
    from_addr: str = "sender@example.com",
    to_addr: str = "me@example.com",
    subject: str = "Hello",
    message_id: str = "<msg-1@example.com>",
    plain: str | None = "plain body",
    html: str | None = None,
    in_reply_to: str = "",
    references: str = "",
    date: str = "Mon, 09 Aug 2026 10:00:00 +0000",
    attachments: list[tuple[str, str, bytes]] | None = None,
) -> bytes:
    """Build a raw RFC822 message. ``attachments`` are (filename, mimetype, payload)."""
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    if message_id:
        msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    if date:
        msg["Date"] = date

    if plain is not None and html is not None:
        msg.set_content(plain)
        msg.add_alternative(html, subtype="html")
    elif html is not None:
        msg.set_content(html, subtype="html")
    else:
        msg.set_content(plain if plain is not None else "")

    for filename, mimetype, payload in attachments or []:
        maintype, _, subtype = mimetype.partition("/")
        msg.add_attachment(
            payload, maintype=maintype, subtype=subtype or "octet-stream", filename=filename
        )

    return msg.as_bytes()
