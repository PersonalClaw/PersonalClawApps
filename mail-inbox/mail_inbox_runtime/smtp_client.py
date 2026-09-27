"""A thin SMTP client the outbound path sends through — a seam over ``smtplib`` so the
reply logic (draft-by-default posture, threading headers, composition) is testable
without a live server and, critically, without ever putting a message on a socket.

Mirrors ``imap_client.py``: :mod:`mail_inbox_runtime.outbound` depends on the narrow
:class:`SmtpSender` protocol, not on ``smtplib``, so a test injects a fake that captures
the composed ``EmailMessage``. The real :class:`SmtplibSender` wraps
``smtplib.SMTP_SSL`` / ``smtplib.SMTP`` + STARTTLS.

**Every session is TLS before anything is sent.** ``ssl`` is implicit TLS from the first byte;
``starttls`` upgrades the session, and a server that does not offer STARTTLS, or an upgrade that
fails, ABORTS before the login and before the mail: a downgrade would put the app password and
the reply body on the wire in plaintext. There is no mode without TLS. A ``plain`` mode used to
send both in the clear to whatever answered, and a setting that still names it reads as
``starttls``. And the server is verified before it gets the password: implicit TLS and STARTTLS
both use :func:`~mail_inbox_runtime.tls.client_context` (system CAs, host name checked, plus
the ``tls_ca_file`` setting's authority), where both used to build a context that checks
nothing.

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

from mail_inbox_runtime.tls import client_context, describe_failure

logger = logging.getLogger(__name__)

#: Socket timeout for every SMTP operation — a half-open connection must not park a
#: worker thread forever.
SMTP_TIMEOUT_SECS = 60

#: Transport modes. ``starttls`` (587) upgrades the session before anything is sent; ``ssl``
#: (465) is implicit TLS from the first byte. Defined here — the module that ACTS on them — and
#: imported by ``settings.py`` for validation, so there is one definition of the set.
SMTP_STARTTLS = "starttls"
SMTP_SSL = "ssl"
VALID_SMTP_SECURITY = frozenset({SMTP_STARTTLS, SMTP_SSL})

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


def _quit_quietly(client: smtplib.SMTP) -> None:
    try:
        client.quit()
    except (smtplib.SMTPException, OSError):
        logger.debug("mail-inbox: SMTP quit error", exc_info=True)


def _connect(
    host: str, port: int, username: str, password: str, *, security: str, ca_file: str
) -> smtplib.SMTP:
    """Open a session, verify the server, upgrade, and log in. BLOCKING.

    Every error is raised as :class:`SmtpError` with the password scrubbed out of it."""
    try:
        client: smtplib.SMTP = (
            smtplib.SMTP_SSL(
                host, port, timeout=SMTP_TIMEOUT_SECS, context=client_context(ca_file)
            )
            if security == SMTP_SSL
            else smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_SECS)
        )
    except (smtplib.SMTPException, OSError) as exc:
        raise SmtpError(
            scrub_secret(describe_failure(exc, protocol="SMTP", host=host, port=port), password)
        ) from exc
    try:
        if security != SMTP_SSL:
            client.ehlo()
            if not client.has_extn("starttls"):
                _quit_quietly(client)
                raise SmtpError(
                    f"the SMTP server {host}:{port} doesn't offer STARTTLS, so nothing was sent "
                    "to it: the connection must be upgraded to TLS before the login and the "
                    "mail. Set SMTP TLS Mode to ssl (usually port 465), or use a server that "
                    "offers STARTTLS"
                )
            # No fallback on purpose: a failed upgrade aborts, never continues to a
            # plaintext AUTH.
            client.starttls(context=client_context(ca_file))
            # RFC 3207: the session resets on upgrade — re-EHLO so the AUTH
            # capabilities read are the post-TLS ones.
            client.ehlo()
        if username and password:
            client.login(username, password)
    except (smtplib.SMTPException, OSError) as exc:
        _quit_quietly(client)
        if isinstance(exc, smtplib.SMTPAuthenticationError):
            why = f"SMTP login failed: {exc}"
        else:
            why = describe_failure(exc, protocol="SMTP", host=host, port=port)
        raise SmtpError(scrub_secret(why, password)) from exc
    return client


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
        ca_file: str = "",
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._security = security
        self._ca_file = ca_file

    def send(self, msg: EmailMessage) -> None:
        client = _connect(
            self._host, self._port, self._username, self._password,
            security=self._security, ca_file=self._ca_file,
        )
        try:
            client.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            raise SmtpError(scrub_secret(f"SMTP send failed: {exc}", self._password)) from exc
        finally:
            _quit_quietly(client)


def probe_login(
    host: str, port: int, username: str, password: str, *, security: str = SMTP_STARTTLS,
    ca_file: str = "",
) -> tuple[bool, str]:
    """The doctor probe: connect, upgrade, log in. **No mail is sent.** BLOCKING."""
    try:
        client = _connect(host, port, username, password, security=security, ca_file=ca_file)
    except SmtpError as exc:
        return False, str(exc)
    _quit_quietly(client)
    return True, f"SMTP login OK ({security})"
