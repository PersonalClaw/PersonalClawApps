"""The mail server is verified before it is trusted with the app password (ledger 252).

``imaplib.IMAP4_SSL``, ``smtplib.SMTP_SSL`` and ``starttls()`` were called with no context,
and the stdlib's default for that (``ssl._create_stdlib_context``) checks neither the
certificate nor the host name. So the channel logged in to whatever answered on the
configured host and port, and gave it the password. Every connection now uses one verifying
context — the system's authorities and the host name — plus the authority the
``tls_ca_file`` setting names, and nothing turns verification off.

The first tests complete REAL TLS handshakes against loopback servers whose certificate a
throwaway authority signed (``_tls_servers``): refused without that authority, with the
password never sent; accepted once the setting names it; refused when the certificate is
for another name. The rest pin the context every path hands the stdlib.
"""

from __future__ import annotations

import imaplib
import smtplib
import ssl

import pytest

from email_runtime.imap_client import Imap4Client, ImapError, probe_login
from email_runtime.smtp_client import SmtpError, SmtplibSender
from email_runtime.smtp_client import probe_login as smtp_probe

from _tls_servers import ImapsServer, StarttlsSmtpServer, mint


@pytest.fixture
def ca(tmp_path):
    return mint(tmp_path / "tls")


@pytest.fixture
def imaps(ca):
    server = ImapsServer(ca)
    yield server
    server.close()


@pytest.fixture
def submission(ca):
    server = StarttlsSmtpServer(ca)
    yield server
    server.close()


class TestAServerNothingVouchesForIsRefused:
    def test_imap_refuses_it_before_the_login(self, imaps):
        client = Imap4Client("127.0.0.1", imaps.port, "noor@example.test", "app password")
        with pytest.raises(ImapError) as refused:
            client.connect()
        assert refused.value.kind == "tls"
        sentence = str(refused.value)
        assert f"the TLS certificate of the IMAP server 127.0.0.1:{imaps.port} is not trusted" in sentence
        assert "so the password was not sent" in sentence
        assert "CA Certificate File" in sentence
        assert imaps.logins == [], "the password reached a server nothing vouches for"

    def test_the_probe_says_the_same_sentence(self, imaps):
        ok, detail = probe_login("127.0.0.1", imaps.port, "u", "pw", "INBOX")
        assert ok is False and "is not trusted" in detail
        assert imaps.logins == []

    def test_smtp_starttls_refuses_it_before_the_login(self, submission):
        sender = SmtplibSender("127.0.0.1", submission.port, "u", "pw", security="starttls")
        with pytest.raises(SmtpError) as refused:
            sender.send(_message())
        assert submission.logins == [], "the password reached a server nothing vouches for"
        assert "is not trusted" in str(refused.value)
        assert refused.value.kind == "tls"

    def test_a_certificate_for_another_name_is_refused_even_with_its_authority(self, tmp_path):
        minted = mint(tmp_path / "other", names=("imap.elsewhere.test",))
        server = ImapsServer(minted)
        try:
            client = Imap4Client("127.0.0.1", server.port, "u", "pw", ca_file=str(minted.ca))
            with pytest.raises(ImapError) as refused:
                client.connect()
            assert refused.value.kind == "tls"
            assert "not trusted" in str(refused.value)
            assert server.logins == []
        finally:
            server.close()


class TestItsAuthorityInTheSettingIsTrusted:
    def test_imap_logs_in_once_the_ca_file_names_it(self, imaps, ca):
        client = Imap4Client("127.0.0.1", imaps.port, "u", "pw", ca_file=str(ca.ca))
        client.connect()
        client.close()
        assert len(imaps.logins) == 1

    def test_the_probe_passes_with_it(self, imaps, ca):
        ok, detail = probe_login("127.0.0.1", imaps.port, "u", "pw", "INBOX", ca_file=str(ca.ca))
        assert ok is True, detail

    def test_smtp_starttls_logs_in_once_the_ca_file_names_it(self, submission, ca):
        ok, detail = smtp_probe(
            "127.0.0.1", submission.port, "u", "pw", security="starttls", ca_file=str(ca.ca)
        )
        assert ok is True, detail
        assert submission.logins == ["u"]

    def test_it_is_trusted_in_addition_to_the_system_authorities(self, ca):
        from email_runtime.tls import client_context

        system = client_context()
        extra = client_context(str(ca.ca))
        assert extra.cert_store_stats()["x509_ca"] == system.cert_store_stats()["x509_ca"] + 1


class TestTheContextEveryPathUses:
    def test_it_verifies_the_certificate_and_the_name(self):
        from email_runtime.tls import client_context

        context = client_context()
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True

    def test_imap_hands_imaplib_the_verifying_context(self, monkeypatch):
        seen = {}

        class Capture:
            def __init__(self, host, port, ssl_context=None, timeout=None):
                seen["context"] = ssl_context

            def login(self, user, password):
                return ("OK", [b""])

        monkeypatch.setattr(imaplib, "IMAP4_SSL", Capture)
        Imap4Client("mail.test", 993, "u", "p").connect()
        assert isinstance(seen["context"], ssl.SSLContext)
        assert seen["context"].verify_mode == ssl.CERT_REQUIRED and seen["context"].check_hostname

    @pytest.mark.parametrize("security", ["ssl", "starttls"])
    def test_smtp_hands_smtplib_the_verifying_context(self, monkeypatch, security):
        contexts = []

        class Capture:
            def __init__(self, host, port, timeout=None, context=None):
                if context is not None:
                    contexts.append(context)

            def ehlo(self):
                return (250, b"ok")

            def starttls(self, context=None):
                contexts.append(context)
                return (220, b"go")

            def login(self, user, password):
                return (235, b"ok")

            def quit(self):
                return (221, b"bye")

        monkeypatch.setattr(smtplib, "SMTP", Capture)
        monkeypatch.setattr(smtplib, "SMTP_SSL", Capture)
        ok, _ = smtp_probe("mail.test", 587, "u", "p", security=security)
        assert ok is True
        assert len(contexts) == 1 and isinstance(contexts[0], ssl.SSLContext)
        assert contexts[0].verify_mode == ssl.CERT_REQUIRED and contexts[0].check_hostname


class TestAnUnusableCaFile:
    def test_it_refuses_the_connection_with_its_own_sentence(self, tmp_path):
        from email_runtime.tls import CaFileError, client_context

        missing = tmp_path / "nope.pem"
        with pytest.raises(CaFileError) as refused:
            client_context(str(missing))
        assert f"the CA certificate file {missing} could not be loaded" in str(refused.value)

    def test_not_a_certificate_is_refused_too(self, tmp_path):
        from email_runtime.tls import CaFileError, client_context

        bogus = tmp_path / "bogus.pem"
        bogus.write_text("not a certificate", encoding="utf-8")
        with pytest.raises(CaFileError):
            client_context(str(bogus))

    def test_the_imap_client_reports_it_as_a_tls_failure(self, tmp_path):
        client = Imap4Client("127.0.0.1", 1, "u", "pw", ca_file=str(tmp_path / "nope.pem"))
        with pytest.raises(ImapError) as refused:
            client.connect()
        assert refused.value.kind == "tls" and "could not be loaded" in str(refused.value)


class TestTheSentences:
    def test_an_unreachable_server(self):
        from email_runtime.tls import describe_failure

        sentence = describe_failure(
            ConnectionRefusedError(61, "Connection refused"),
            protocol="IMAP", host="imap.test", port=993,
        )
        assert sentence == "the IMAP server imap.test:993 is unreachable (Connection refused)"

    def test_a_refused_certificate(self):
        from email_runtime.tls import describe_failure

        exc = ssl.SSLCertVerificationError(1, "certificate verify failed")
        exc.verify_message = "self-signed certificate"
        sentence = describe_failure(exc, protocol="SMTP", host="smtp.test", port=587)
        assert sentence.startswith(
            "the TLS certificate of the SMTP server smtp.test:587 is not trusted "
            "(self-signed certificate), so the password was not sent."
        )


def _message():
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = "noor@example.test"
    msg["To"] = "sam@example.test"
    msg["Subject"] = "hi"
    msg.set_content("hello")
    return msg
