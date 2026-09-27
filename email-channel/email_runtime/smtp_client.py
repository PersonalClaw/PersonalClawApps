"""The blocking SMTP mechanics, behind a narrow protocol the delivery can fake.

``smtplib`` is a **synchronous** API, exactly like ``imaplib``: every call blocks the
calling thread. :class:`~email_runtime.delivery.EmailDelivery` therefore hands each send
to a thread executor and never calls into this module from the event loop.

A connection is opened per send rather than held open. That is deliberate: providers
drop idle SMTP sessions aggressively (Gmail at ~a minute), so a cached connection is
usually dead by the time the next result needs delivering, and the failure surfaces as a
lost message instead of a retry. Sends here are occasional, not a stream.

**Authentication is an app password.** OAuth2 (XOAUTH2) is DEFERRED — see the DISCOVERY
note in the app README. Every provider whose flow the setup step documents
(Gmail/Fastmail/iCloud) issues a per-application password precisely for clients like
this, and it needs no token-refresh machinery, no client registration, and no browser
round-trip in a headless gateway.

**The server is verified before it is trusted with the password.** Implicit TLS and
STARTTLS both use :func:`~email_runtime.tls.client_context` (system CAs, host name checked,
plus the ``tls_ca_file`` setting's authority), where both used to build a context that
checks nothing. A failure is raised as a :class:`SmtpError` whose ``kind`` says which stage
failed and whose text is the sentence the channel's health shows.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import Protocol

from email_runtime.tls import CaFileError, client_context, describe_failure

logger = logging.getLogger(__name__)

#: Socket timeout for every SMTP operation — a half-open connection must not park a
#: worker thread forever.
SMTP_TIMEOUT_SECS = 60


class SmtpError(Exception):
    """Any SMTP transport/auth failure. The caller degrades, never crashes.

    ``kind`` names the stage: ``tls`` (the certificate, the handshake, or a server that
    offers no STARTTLS), ``unreachable``, ``auth`` (the login was refused) or ``protocol``
    (the server refused the message itself)."""

    def __init__(self, message: str, *, kind: str = "protocol") -> None:
        super().__init__(message)
        self.kind = kind


class SmtpSender(Protocol):
    """The narrow surface delivery needs. A fake in tests implements just this."""

    def send(self, msg: EmailMessage) -> None: ...


def _stage_kind(exc: BaseException) -> str:
    if isinstance(exc, (ssl.SSLError, CaFileError)):
        return "tls"
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return "unreachable"
    if isinstance(exc, smtplib.SMTPException):
        return "protocol"  # an answer from a server that was reached (it is an OSError too)
    if isinstance(exc, OSError):
        return "unreachable"
    return "protocol"


def _quit_quietly(client: smtplib.SMTP) -> None:
    try:
        client.quit()
    except (smtplib.SMTPException, OSError):
        logger.debug("email: SMTP quit error", exc_info=True)


def _connect(
    host: str, port: int, username: str, password: str, *, security: str, ca_file: str
) -> smtplib.SMTP:
    """Open a session, verify the server, and log in. BLOCKING.

    ``security`` is ``"ssl"`` (implicit TLS, 465), ``"starttls"`` (587) or ``"plain"``.
    STARTTLS is required rather than attempted: a server that does not offer it, or an
    upgrade that fails, aborts before the login, so an app password never crosses in the
    clear. Raises :class:`SmtpError` naming the failed stage."""

    def describe(exc: BaseException) -> str:
        return describe_failure(exc, protocol="SMTP", host=host, port=port)

    try:
        client: smtplib.SMTP = (
            smtplib.SMTP_SSL(
                host, port, timeout=SMTP_TIMEOUT_SECS, context=client_context(ca_file)
            )
            if security == "ssl"
            else smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_SECS)
        )
    except (smtplib.SMTPException, OSError) as exc:
        raise SmtpError(describe(exc), kind=_stage_kind(exc)) from exc
    try:
        if security != "ssl":
            client.ehlo()
            if security == "starttls":
                client.starttls(context=client_context(ca_file))
                # RFC 3207: the session resets on upgrade — re-EHLO so the AUTH
                # capabilities read are the post-TLS ones.
                client.ehlo()
    except smtplib.SMTPNotSupportedError as exc:
        _quit_quietly(client)
        raise SmtpError(
            f"the SMTP server {host}:{port} does not offer STARTTLS, so nothing was sent "
            "(choose ssl for port 465)",
            kind="tls",
        ) from exc
    except (smtplib.SMTPException, OSError) as exc:
        _quit_quietly(client)
        raise SmtpError(describe(exc), kind=_stage_kind(exc)) from exc
    if username and password:
        try:
            client.login(username, password)
        except smtplib.SMTPAuthenticationError as exc:
            _quit_quietly(client)
            said = exc.smtp_error.decode("utf-8", errors="replace") if isinstance(
                exc.smtp_error, bytes
            ) else str(exc.smtp_error)
            raise SmtpError(
                f"SMTP login failed: {host}:{port} refused the login for {username} "
                f"({exc.smtp_code} {said})",
                kind="auth",
            ) from exc
        except (smtplib.SMTPException, OSError) as exc:
            _quit_quietly(client)
            raise SmtpError(describe(exc), kind=_stage_kind(exc)) from exc
    return client


class SmtplibSender:
    """The real sender — wraps ``smtplib.SMTP`` / ``SMTP_SSL``.

    :meth:`send` is BLOCKING and must be called from a thread executor.

    ``security`` selects the transport: ``"ssl"`` (implicit TLS, port 465), ``"starttls"``
    (upgrade an established plaintext session, port 587), or ``"plain"``. STARTTLS is
    verified rather than attempted: if the upgrade fails the send is ABORTED, never
    retried in the clear — silently downgrading would put an app password on the wire in
    plaintext, which is the whole failure this check exists to prevent."""

    def __init__(
        self, host: str, port: int, username: str, password: str, *, security: str = "starttls",
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
            raise SmtpError(f"SMTP send failed: {exc}", kind=_stage_kind(exc)) from exc
        finally:
            _quit_quietly(client)


def probe_login(
    host: str, port: int, username: str, password: str, *, security: str = "starttls",
    ca_file: str = "",
) -> tuple[bool, str]:
    """The doctor/Test probe: connect, upgrade, log in. No mail is sent. BLOCKING."""
    try:
        client = _connect(host, port, username, password, security=security, ca_file=ca_file)
    except SmtpError as exc:
        return False, str(exc)
    _quit_quietly(client)
    return True, f"SMTP login OK ({security})"
