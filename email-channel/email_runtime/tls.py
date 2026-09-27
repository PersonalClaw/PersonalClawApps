"""The TLS context every IMAP and SMTP connection this app makes is made with.

``imaplib.IMAP4_SSL``, ``smtplib.SMTP_SSL`` and ``starttls()`` given no context build one
with ``ssl._create_stdlib_context``, which checks neither the certificate nor the host
name. This app used exactly that, so it logged in to any server that answered on the
configured host and port, and handed it the app password. :func:`client_context` is the
one context now: the system's certificate authorities, the host name checked, a
certificate nothing vouches for refused.

A mail server whose certificate comes from a private certificate authority (a company
relay, a home server, a test server) is reached by naming that authority's certificate in
the ``tls_ca_file`` setting. It is trusted IN ADDITION to the system's, never instead of
them, and there is no setting that turns verification off.

:func:`describe_failure` turns what a connection attempt raised into the sentence the
channel's health, Test and doctor show: the certificate refused, the TLS handshake failed,
the server unreachable, or the login refused.
"""

from __future__ import annotations

import os
import smtplib
import socket
import ssl


class CaFileError(OSError):
    """The ``tls_ca_file`` setting names a file that is not a usable CA certificate."""


def client_context(ca_file: str = "") -> ssl.SSLContext:
    """A verifying client context: system CAs, host name checked, plus ``ca_file`` if set.

    Raises :class:`CaFileError` when ``ca_file`` cannot be read as PEM certificates, so a
    typo in the setting refuses the connection with its own sentence instead of falling
    back to anything weaker."""
    context = ssl.create_default_context()
    path = os.path.expanduser(ca_file.strip()) if ca_file else ""
    if path:
        try:
            context.load_verify_locations(cafile=path)
        except (OSError, ssl.SSLError, ValueError) as exc:
            reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
            raise CaFileError(
                f"the CA certificate file {path} could not be loaded ({reason}), so no "
                "connection was made"
            ) from exc
    return context


def describe_failure(exc: BaseException, *, protocol: str, host: str, port: int) -> str:
    """What went wrong connecting to ``host:port``, as a sentence for the owner.

    ``protocol`` is ``"IMAP"`` or ``"SMTP"``. A refused certificate says the password was
    not sent — true, because verification happens in the handshake, before any login."""
    where = f"{protocol} server {host}:{port}"
    if isinstance(exc, CaFileError):
        return str(exc)
    if isinstance(exc, ssl.SSLCertVerificationError):
        why = exc.verify_message or exc.reason or "certificate verify failed"
        return (
            f"the TLS certificate of the {where} is not trusted ({why}), so the password was "
            "not sent. If the server uses your own certificate authority, set CA Certificate "
            "File to that authority's certificate"
        )
    if isinstance(exc, ssl.SSLError):
        return f"the TLS handshake with the {where} failed ({exc.reason or exc})"
    # Before OSError, which every smtplib exception also is: an SMTP reply is an answer from
    # a server that was reached, not an unreachable one.
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return f"the {where} closed the connection ({exc})"
    if isinstance(exc, smtplib.SMTPResponseException):
        said = exc.smtp_error
        text = said.decode("utf-8", "replace") if isinstance(said, bytes) else str(said)
        return f"the {where} answered {exc.smtp_code} {text}"
    if isinstance(exc, smtplib.SMTPException):
        return f"the {where} failed ({exc})"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return f"the {where} did not answer in time"
    if isinstance(exc, socket.gaierror):
        return f"the {where} is unreachable: its name does not resolve ({exc.strerror or exc})"
    if isinstance(exc, OSError):
        return f"the {where} is unreachable ({exc.strerror or exc})"
    return f"the {where} failed ({exc})"
