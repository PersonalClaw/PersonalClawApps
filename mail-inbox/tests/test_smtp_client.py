"""The SMTP transport seam: TLS is verified rather than attempted, and a failure never
leaks the password.

No socket is opened and **no mail is sent**: ``smtplib.SMTP`` / ``SMTP_SSL`` are replaced
with an in-process double that records the dialogue, so the assertions are about what the
sender WOULD do on the wire.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

import pytest

from mail_inbox_runtime.smtp_client import (
    SMTP_SSL,
    SMTP_STARTTLS,
    SmtpError,
    SmtplibSender,
    scrub_secret,
)

PASSWORD = "correct-horse-battery-staple"


class FakeSmtp:
    """Records the call sequence. ``fail_on`` raises at the named step."""

    last: "FakeSmtp | None" = None

    def __init__(self, host, port, timeout=None, fail_on: str = "", error_text: str = ""):
        self.host, self.port, self.timeout = host, port, timeout
        self.fail_on, self.error_text = fail_on, error_text
        self.calls: list[str] = []
        self.sent: list[EmailMessage] = []
        FakeSmtp.last = self

    def _step(self, name: str) -> None:
        self.calls.append(name)
        if self.fail_on == name:
            raise smtplib.SMTPException(self.error_text or f"{name} failed")

    def ehlo(self):
        self._step("ehlo")

    def starttls(self):
        self._step("starttls")

    def login(self, username, password):
        self.calls.append(f"login:{username}")
        if self.fail_on == "login":
            raise smtplib.SMTPAuthenticationError(535, self.error_text or "auth failed")

    def send_message(self, msg):
        self._step("send_message")
        self.sent.append(msg)

    def quit(self):
        self.calls.append("quit")


def _install(monkeypatch, **kwargs):
    """Point both smtplib entry points at the double; return the factory's call log."""
    made: list[str] = []

    def factory(kind):
        def build(host, port, timeout=None):
            made.append(kind)
            return FakeSmtp(host, port, timeout, **kwargs)

        return build

    monkeypatch.setattr(smtplib, "SMTP", factory("plain"))
    monkeypatch.setattr(smtplib, "SMTP_SSL", factory("ssl"))
    return made


def _message() -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = "me@example.com"
    msg["To"] = "you@example.com"
    msg["Subject"] = "Re: hi"
    msg.set_content("body")
    return msg


def test_starttls_upgrade_precedes_login_and_send(monkeypatch):
    made = _install(monkeypatch)
    SMTPLIB = SmtplibSender(
        "smtp.example.com", 587, "me@example.com", PASSWORD, security=SMTP_STARTTLS
    )
    SMTPLIB.send(_message())

    assert made == ["plain"]
    calls = FakeSmtp.last.calls
    assert calls.index("starttls") < calls.index("login:me@example.com")
    assert calls.index("login:me@example.com") < calls.index("send_message")
    # RFC 3207: EHLO again after the upgrade.
    assert calls[:4] == ["ehlo", "starttls", "ehlo", "login:me@example.com"]
    assert len(FakeSmtp.last.sent) == 1


def test_starttls_failure_aborts_before_any_credential_or_message_is_sent(monkeypatch):
    """A failed upgrade must ABORT, never continue in the clear: neither the password nor
    the message may reach a plaintext session."""
    _install(monkeypatch, fail_on="starttls")
    sender = SmtplibSender(
        "smtp.example.com", 587, "me@example.com", PASSWORD, security=SMTP_STARTTLS
    )

    with pytest.raises(SmtpError):
        sender.send(_message())

    calls = FakeSmtp.last.calls
    assert not any(c.startswith("login") for c in calls)
    assert "send_message" not in calls
    assert FakeSmtp.last.sent == []


def test_ssl_mode_uses_implicit_tls_and_never_calls_starttls(monkeypatch):
    made = _install(monkeypatch)
    SmtplibSender("smtp.example.com", 465, "me@example.com", PASSWORD, security=SMTP_SSL).send(
        _message()
    )

    assert made == ["ssl"]
    assert "starttls" not in FakeSmtp.last.calls


def test_send_failure_scrubs_the_password_out_of_the_error(monkeypatch):
    """An SMTP failure echoes the server dialogue; a mis-set login can put the password in
    it. The error a caller (and the log) sees must not carry the secret."""
    _install(monkeypatch, fail_on="login", error_text=f"bad credentials: {PASSWORD}")
    sender = SmtplibSender(
        "smtp.example.com", 587, "me@example.com", PASSWORD, security=SMTP_STARTTLS
    )

    with pytest.raises(SmtpError) as exc:
        sender.send(_message())

    assert PASSWORD not in str(exc.value)
    assert "***" in str(exc.value)


def test_quit_failure_does_not_mask_a_successful_send(monkeypatch):
    _install(monkeypatch)
    sender = SmtplibSender("smtp.example.com", 587, "", "", security=SMTP_STARTTLS)
    sender.send(_message())
    assert FakeSmtp.last.calls[-1] == "quit"
    # No credentials configured ⇒ no AUTH attempted (an open relay on loopback).
    assert not any(c.startswith("login") for c in FakeSmtp.last.calls)


def test_scrub_secret_leaves_short_or_empty_values_alone():
    assert scrub_secret("no secret here", "") == "no secret here"
    # A 1-3 char "password" is a misconfiguration; substituting it would blank out
    # unrelated characters and make the error unreadable.
    assert scrub_secret("421 abc service", "abc") == "421 abc service"
    assert scrub_secret(f"failed for {PASSWORD}", PASSWORD) == "failed for ***"
