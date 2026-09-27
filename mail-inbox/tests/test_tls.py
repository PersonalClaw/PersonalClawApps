"""Mail Inbox verifies the mail server before trusting it with the password (ledger 252).

The same defect as Email Channel's: ``imaplib.IMAP4_SSL``, ``smtplib.SMTP_SSL`` and
``starttls()`` were called with no context, and the stdlib's default for that
(``ssl._create_stdlib_context``) checks neither the certificate nor the host name. So the
source logged in to whatever answered on the configured host and port, and gave it the
password. Every connection now uses one verifying context — the system's authorities and the
host name — plus the authority the ``tls_ca_file`` setting names, and nothing turns
verification off.

The first tests complete REAL TLS handshakes against loopback servers whose certificate a
throwaway authority signed (``_tls_servers``). The rest pin the context each path hands the
stdlib, the setting's path through the source, and the doctor's line.
"""

from __future__ import annotations

import asyncio
import imaplib
import smtplib
import ssl
from email.message import EmailMessage

import pytest

from mail_inbox_runtime.imap_client import Imap4Client, ImapError
from mail_inbox_runtime.provider import MailInboxProvider
from mail_inbox_runtime.settings import MailInboxSettings, _APP
from mail_inbox_runtime.smtp_client import SmtpError, SmtplibSender
from mail_inbox_runtime.smtp_client import probe_login as smtp_probe

from _tls_servers import ImapsServer, StarttlsImapServer, StarttlsSmtpServer, mint


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


def _message() -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = "me@example.com"
    msg["To"] = "sam@example.com"
    msg["Subject"] = "Re: hi"
    msg.set_content("hello")
    return msg


class TestAServerNothingVouchesForIsRefused:
    def test_imap_refuses_it_before_the_login(self, imaps):
        client = Imap4Client("127.0.0.1", imaps.port, "me@example.com", "app password")
        with pytest.raises(ImapError) as refused:
            client.connect()
        sentence = str(refused.value)
        assert f"the TLS certificate of the IMAP server 127.0.0.1:{imaps.port} is not trusted" in sentence
        assert "so the password was not sent" in sentence
        assert "CA Certificate File" in sentence
        assert imaps.logins == [], "the password reached a server nothing vouches for"

    def test_smtp_starttls_refuses_it_before_the_login(self, submission):
        sender = SmtplibSender("127.0.0.1", submission.port, "u", "pw", security="starttls")
        with pytest.raises(SmtpError) as refused:
            sender.send(_message())
        assert submission.logins == [], "the password reached a server nothing vouches for"
        assert "is not trusted" in str(refused.value)

    def test_a_certificate_for_another_name_is_refused_even_with_its_authority(self, tmp_path):
        minted = mint(tmp_path / "other", names=("imap.elsewhere.test",))
        server = ImapsServer(minted)
        try:
            client = Imap4Client("127.0.0.1", server.port, "u", "pw", ca_file=str(minted.ca))
            with pytest.raises(ImapError) as refused:
                client.connect()
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

    def test_smtp_starttls_logs_in_once_the_ca_file_names_it(self, submission, ca):
        ok, detail = smtp_probe(
            "127.0.0.1", submission.port, "u", "pw", security="starttls", ca_file=str(ca.ca)
        )
        assert ok is True, detail
        assert submission.logins == ["u"]


class TestTheSourceUsesTheSetting:
    """The setting reaches the connection the source really makes."""

    @staticmethod
    def _configure(port: int, ca_file: str = "") -> None:
        from personalclaw.sdk.settings import ProviderSettings

        ProviderSettings.update(
            _APP,
            {
                "host": "127.0.0.1", "port": port, "use_ssl": True,
                "username": "me@example.com", "address": "me@example.com", "folder": "INBOX",
                "allow_senders": ["*@example.com"], "password": "secret",
                "tls_ca_file": ca_file,
            },
        )

    def test_refused_without_it_then_polling_with_it(self, imaps, ca):
        """Refused, the poll fails with the sentence core's inbox shows for this source."""
        self._configure(imaps.port)
        with pytest.raises(ImapError) as refused:
            asyncio.run(MailInboxProvider().poll([], {}, "me"))
        assert imaps.logins == []
        said = str(refused.value)
        assert "is not trusted" in said and "so the password was not sent" in said

        self._configure(imaps.port, str(ca.ca))
        messages, checkpoints = asyncio.run(MailInboxProvider().poll([], {}, "me"))
        assert len(imaps.logins) == 1
        settings = MailInboxSettings.load()
        assert checkpoints == {
            MailInboxProvider._checkpoint_key(settings): "0",
            MailInboxProvider._validity_key(settings): "7",
        }, "the empty folder was read and its start recorded, in the numbering it is in"

    def test_the_sender_gets_it_too(self, ca):
        self._configure(993, str(ca.ca))
        sender = MailInboxProvider()._make_sender(MailInboxSettings.load(), "pw")
        assert isinstance(sender, SmtplibSender) and sender._ca_file == str(ca.ca)


class TestTheContextEveryPathUses:
    def test_it_verifies_the_certificate_and_the_name(self):
        from mail_inbox_runtime.tls import client_context

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
        missing = tmp_path / "nope.pem"
        client = Imap4Client("127.0.0.1", 1, "u", "pw", ca_file=str(missing))
        with pytest.raises(ImapError) as refused:
            client.connect()
        assert f"the CA certificate file {missing} could not be loaded" in str(refused.value)

    def test_not_a_certificate_is_refused_too(self, tmp_path):
        from mail_inbox_runtime.tls import CaFileError, client_context

        bogus = tmp_path / "bogus.pem"
        bogus.write_text("not a certificate", encoding="utf-8")
        with pytest.raises(CaFileError):
            client_context(str(bogus))

    def test_the_sender_refuses_it_before_connecting(self, tmp_path):
        sender = SmtplibSender(
            "127.0.0.1", 1, "u", "pw", security="ssl", ca_file=str(tmp_path / "nope.pem")
        )
        with pytest.raises(SmtpError) as refused:
            sender.send(_message())
        assert "could not be loaded" in str(refused.value)


class TestTheDoctorSaysWhatIsTrusted:
    @staticmethod
    def _line(ca_file: str):
        from personalclaw.sdk.settings import ProviderSettings

        from cli_doctor import _tls_line

        ProviderSettings.update(_APP, {"host": "imap.example.com", "tls_ca_file": ca_file})
        return _tls_line(MailInboxSettings.load())

    def test_the_system_authorities_by_default(self):
        line = self._line("")
        assert line.status == "ok" and "this machine's authorities" in line.detail

    def test_and_the_named_authority(self, ca):
        line = self._line(str(ca.ca))
        assert line.status == "ok" and line.detail.endswith(str(ca.ca))

    def test_a_ca_file_that_cannot_be_loaded_fails(self, tmp_path):
        line = self._line(str(tmp_path / "nope.pem"))
        assert line.status == "fail" and "could not be loaded" in line.detail


class TestWithSslOffTheConnectionIsUpgradedFirst:
    """Ledger 282: with Use SSL off, ``imaplib.IMAP4`` logged in on the plain connection, so
    the password crossed the network in the clear. It is upgraded with STARTTLS through the
    verifying context first now, and a server that cannot upgrade is refused before the
    login. There is no setting that sends the password in the clear."""

    @pytest.fixture
    def plain(self, ca):
        server = StarttlsImapServer(ca)
        yield server
        server.close()

    @pytest.fixture
    def no_upgrade(self, ca):
        server = StarttlsImapServer(ca, offer_starttls=False)
        yield server
        server.close()

    def test_the_login_goes_over_tls(self, plain, ca):
        client = Imap4Client("127.0.0.1", plain.port, "u", "pw", use_ssl=False, ca_file=str(ca.ca))
        client.connect()
        assert client.select_folder("INBOX") == 7
        client.close()
        assert len(plain.logins) == 1
        assert plain.clear_logins == []

    def test_a_server_that_cannot_upgrade_never_gets_the_password(self, no_upgrade, ca):
        client = Imap4Client(
            "127.0.0.1", no_upgrade.port, "u", "pw", use_ssl=False, ca_file=str(ca.ca)
        )
        with pytest.raises(ImapError) as refused:
            client.connect()
        assert str(refused.value) == (
            f"the IMAP server 127.0.0.1:{no_upgrade.port} doesn't offer STARTTLS, so the "
            "password was not sent: with Use SSL off, the connection must be upgraded to TLS "
            "before the login. Turn Use SSL on (usually port 993), or use a server that offers "
            "STARTTLS"
        )
        assert no_upgrade.clear_logins == [] and no_upgrade.logins == []

    def test_the_upgrade_checks_the_certificate_like_implicit_tls(self, plain):
        client = Imap4Client("127.0.0.1", plain.port, "u", "pw", use_ssl=False)
        with pytest.raises(ImapError) as refused:
            client.connect()
        assert "is not trusted" in str(refused.value)
        assert "so the password was not sent" in str(refused.value)
        assert plain.clear_logins == [] and plain.logins == []

    def test_the_source_polls_through_the_upgrade(self, plain, ca):
        from personalclaw.sdk.settings import ProviderSettings

        ProviderSettings.update(
            _APP,
            {
                "host": "127.0.0.1", "port": plain.port, "use_ssl": False,
                "username": "me@example.com", "address": "me@example.com", "folder": "INBOX",
                "allow_senders": ["*@example.com"], "password": "secret",
                "tls_ca_file": str(ca.ca),
            },
        )
        messages, checkpoints = asyncio.run(MailInboxProvider().poll([], {}, "me"))
        assert messages == [] and len(plain.logins) == 1 and plain.clear_logins == []
        settings = MailInboxSettings.load()
        assert checkpoints == {
            MailInboxProvider._checkpoint_key(settings): "0",
            MailInboxProvider._validity_key(settings): "7",
        }

    def test_imaplib_is_given_a_timeout_both_ways(self, monkeypatch):
        seen = []

        class Capture:
            capabilities = ("IMAP4REV1", "STARTTLS")

            def __init__(self, host, port, ssl_context=None, timeout=None):
                seen.append(timeout)

            def starttls(self, ssl_context=None):
                return ("OK", [b""])

            def login(self, user, password):
                return ("OK", [b""])

        monkeypatch.setattr(imaplib, "IMAP4_SSL", Capture)
        monkeypatch.setattr(imaplib, "IMAP4", Capture)
        Imap4Client("mail.test", 993, "u", "p").connect()
        Imap4Client("mail.test", 143, "u", "p", use_ssl=False).connect()
        from mail_inbox_runtime.imap_client import IMAP_TIMEOUT_SECS

        assert seen == [IMAP_TIMEOUT_SECS, IMAP_TIMEOUT_SECS]
