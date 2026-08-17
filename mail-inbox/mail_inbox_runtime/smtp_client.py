"""A thin SMTP client the outbound path sends through — a seam over ``smtplib`` so the
reply logic (draft-by-default posture, threading headers, composition) is testable
without a live server and, critically, without ever putting a message on a socket.

Mirrors ``imap_client.py``: :mod:`mail_inbox_runtime.outbound` depends on the narrow
:class:`SmtpSender` protocol, not on ``smtplib``, so a test injects a fake that captures
the composed ``EmailMessage``. The real :class:`SmtplibSender` wraps
``smtplib.SMTP_SSL`` / ``smtplib.SMTP`` + STARTTLS.

**TLS is verified, never merely attempted.** ``starttls`` ABORTS the send when the
upgrade fails rather than continuing in the clear — a silent downgrade would put the app
password and the reply body on the wire in plaintext, which is the exact failure this
check exists to prevent. ``plain`` exists only for a relay on loopback and is never a
default.

A connection is opened per send rather than held open: providers drop idle SMTP sessions
aggressively (Gmail at ~a minute), so a cached session is usually dead by the time the
next reply needs sending and the failure surfaces as a lost message instead of a retry.
Replies are occasional, not a stream.

Error text is SCRUBBED before it leaves this module (:func:`scrub_secret`): an SMTP
failure echoes the server dialogue, and a mis-set login can land the app password inside
it. The provider logs these strings, so redaction happens at the source, once.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Protocol

logger = logging.getLogger(__name__)

#: Socket timeout for every SMTP operation — a half-open connection must not park a
#: worker thread forever.
SMTP_TIMEOUT_SECS = 60

#: Transport modes. ``starttls`` (587) upgrades an established plaintext session;
#: ``ssl`` (465) is implicit TLS from the first byte; ``plain`` is unencrypted and only
#: sane against a relay on loopback. Defined here — the module that ACTS on them — and
#: imported by ``settings.py`` for validation, so there is one definition of the set.
SMTP_STARTTLS = "starttls"
SMTP_SSL = "ssl"
SMTP_PLAIN = "plain"
VALID_SMTP_SECURITY = frozenset({SMTP_STARTTLS, SMTP_SSL, SMTP_PLAIN})

#: Default submission port for the default (STARTTLS) mode.
DEFAULT_SMTP_PORT = 587

_REDACTED = "***"


def scrub_secret(text: str, secret: str) -> str:
    """Replace *secret* wherever it appears in *text*.

    Applied to every error string this module raises. Short secrets are not scrubbed —
    a 1-3 character "password" is a misconfiguration, and substituting it would blank
    out unrelated characters of the server dialogue and make the error unreadable."""
    if not secret or len(secret) < 4:
        return text
    return text.replace(secret, _REDACTED)


class SmtpError(Exception):
    """Any SMTP transport/auth failure. The caller degrades (drafts), never crashes."""


class SmtpSender(Protocol):
    """The narrow surface the outbound path needs. A fake in tests implements just this."""

    def send(self, msg: EmailMessage) -> None: ...


class SmtplibSender:
    """The real sender — wraps ``smtplib.SMTP`` / ``SMTP_SSL``.

    :meth:`send` is BLOCKING and must be called from a thread executor (the provider
    hands it to ``asyncio.to_thread``, exactly as it does the IMAP poll)."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        *,
        security: str = SMTP_STARTTLS,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._security = security

    def send(self, msg: EmailMessage) -> None:
        client: smtplib.SMTP | None = None
        try:
            if self._security == SMTP_SSL:
                client = smtplib.SMTP_SSL(self._host, self._port, timeout=SMTP_TIMEOUT_SECS)
            else:
                client = smtplib.SMTP(self._host, self._port, timeout=SMTP_TIMEOUT_SECS)
                client.ehlo()
                if self._security == SMTP_STARTTLS:
                    # No try/except around the upgrade on purpose: a failure must abort
                    # the send, never fall through to a plaintext AUTH.
                    client.starttls()
                    # RFC 3207: the session resets on upgrade — re-EHLO so the AUTH
                    # capabilities read are the post-TLS ones.
                    client.ehlo()
            if self._username and self._password:
                client.login(self._username, self._password)
            client.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            raise SmtpError(scrub_secret(f"SMTP send failed: {exc}", self._password)) from exc
        finally:
            if client is not None:
                try:
                    client.quit()
                except (smtplib.SMTPException, OSError):
                    logger.debug("mail-inbox: SMTP quit error", exc_info=True)


def probe_login(
    host: str, port: int, username: str, password: str, *, security: str = SMTP_STARTTLS
) -> tuple[bool, str]:
    """The doctor probe: connect, upgrade, log in. **No mail is sent.** BLOCKING."""
    client: smtplib.SMTP | None = None
    try:
        if security == SMTP_SSL:
            client = smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT_SECS)
        else:
            client = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_SECS)
            client.ehlo()
            if security == SMTP_STARTTLS:
                client.starttls()
                client.ehlo()
        if username and password:
            client.login(username, password)
        return True, f"SMTP login OK ({security})"
    except (smtplib.SMTPException, OSError) as exc:
        return False, scrub_secret(f"SMTP login failed: {exc}", password)
    finally:
        if client is not None:
            try:
                client.quit()
            except (smtplib.SMTPException, OSError):
                logger.debug("mail-inbox: SMTP probe quit error", exc_info=True)
