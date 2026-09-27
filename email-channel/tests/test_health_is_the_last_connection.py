"""Email Channel's status says what its connections last did (ledger 251, the F-24 contract).

``health()`` read "ready" whenever the settings and passwords were saved. It never looked at
the poll loop, whose IMAP failures ``_fetch_batch`` logged and swallowed, so the card read
ready while every login was refused — the gateway log's ``AUTHENTICATIONFAILED`` was the only
trace. The same swallow kept the loop's backoff from ever engaging: a refused password was
tried again every minute. Now health is ready only while the receiver runs, its last IMAP
cycle read the folder and the last send went out; otherwise it is ``error`` with the sentence
naming what failed. Test agrees with it, as the channel contract requires.

The receiver runs for real here (``start_inbound`` → ``_poll_loop``) against the injected
fake IMAP server; nothing opens a socket.
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from personalclaw.sdk.channel import ProviderSettings, save_credential

from email_runtime.settings import CRED_IMAP_PASS, reload_settings
from email_runtime.transport import EmailTransport
from _fakes import FakeImapServer, FakeSmtpServer, FakeState

_APP = "email-channel"
NOOR = "noor@example.test"

REFUSALS = [
    pytest.param(
        "auth",
        "the IMAP server imap.test:993 refused the login for noor@example.test "
        "([AUTHENTICATIONFAILED] Invalid credentials (Failure))",
        id="login-refused",
    ),
    pytest.param(
        "unreachable", "the IMAP server imap.test:993 is unreachable (Connection refused)",
        id="unreachable",
    ),
    pytest.param(
        "tls",
        "the TLS certificate of the IMAP server imap.test:993 is not trusted (unable to get "
        "local issuer certificate), so the password was not sent",
        id="certificate-refused",
    ),
]


class _Services:
    def __init__(self) -> None:
        self.dashboard_state = FakeState()
        self.registered = None

    def register_channel_delivery(self, delivery) -> None:
        self.registered = delivery

    async def deliver_channel_inbound(self, provider, msg, *, is_dm=True):  # pragma: no cover
        raise AssertionError("these tests deliver no mail")


@pytest_asyncio.fixture
async def channel():
    ProviderSettings.update(
        _APP,
        {
            "imap_host": "imap.test", "imap_port": 993, "imap_user": NOOR,
            "smtp_host": "smtp.test", "smtp_port": 587, "smtp_user": NOOR,
            "address": NOOR, "folder": "INBOX", "poll_secs": 60,
        },
    )
    save_credential(CRED_IMAP_PASS, "app-password")
    reload_settings()
    imap, smtp = FakeImapServer(), FakeSmtpServer()
    transport = EmailTransport()
    transport._client_factory = lambda settings, password: imap
    transport._sender_factory = lambda settings, password: smtp
    yield transport, imap, smtp
    await transport.stop_inbound()


async def _until(check, what: str) -> None:
    for _ in range(300):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"never: {what}")


async def _start(transport, imap) -> None:
    await transport.start_inbound(_Services())
    await _until(lambda: imap.connect_calls >= 1 and imap.closed, "the first cycle ran")
    await asyncio.sleep(0.05)  # the cycle's result lands back on the loop


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "sentence"), REFUSALS)
async def test_a_failing_imap_poll_reads_not_receiving(channel, monkeypatch, kind, sentence):
    transport, imap, _ = channel
    imap.fail_connect, imap.fail_connect_kind, imap.fail_connect_with = True, kind, sentence
    await _start(transport, imap)

    health = await transport.health()
    assert health["state"] == "error", health
    assert health["detail"].startswith(
        f"Inbound NOT RECEIVING — the last IMAP poll failed: {sentence}. It tries again"
    ), health["detail"]

    # Test agrees with it, even when both logins work right now.
    monkeypatch.setattr("email_runtime.transport.imap_probe", lambda *a, **k: (True, "IMAP ok"))
    monkeypatch.setattr("email_runtime.transport.smtp_probe", lambda *a, **k: (True, "SMTP ok"))
    probe = await transport.test()
    assert probe["ok"] is False and "Inbound NOT RECEIVING" in probe["detail"]


@pytest.mark.asyncio
async def test_it_reads_ready_again_once_a_poll_reads_the_folder(channel):
    transport, imap, _ = channel
    imap.fail_connect = True
    await _start(transport, imap)
    assert (await transport.health())["state"] == "error"

    imap.fail_connect = False
    await transport._poll_once(transport._settings())
    health = await transport.health()
    assert health == {"state": "ready", "detail": "IMAP imap.test · SMTP smtp.test"}


@pytest.mark.asyncio
async def test_a_receiver_that_does_not_run_is_not_ready(channel, monkeypatch):
    transport, _, _ = channel
    health = await transport.health()
    assert health["state"] == "error"
    assert health["detail"].startswith("Inbound NOT STARTED")

    monkeypatch.setattr("email_runtime.transport.imap_probe", lambda *a, **k: (True, "IMAP ok"))
    monkeypatch.setattr("email_runtime.transport.smtp_probe", lambda *a, **k: (True, "SMTP ok"))
    probe = await transport.test()
    assert probe["ok"] is False and "but Inbound NOT STARTED" in probe["detail"]


@pytest.mark.asyncio
async def test_a_failed_send_reads_not_sending_until_one_goes_out(channel):
    transport, imap, smtp = channel
    await _start(transport, imap)
    assert (await transport.health())["state"] == "ready"

    smtp.fail = True
    assert await transport._delivery.deliver_text("sam@example.test", "hello") == ""
    health = await transport.health()
    assert health["state"] == "error"
    assert "Outbound NOT SENDING — the last send failed: fake: relay refused" in health["detail"]

    smtp.fail = False
    assert await transport._delivery.deliver_text("sam@example.test", "hello again")
    assert (await transport.health())["state"] == "ready"


@pytest.mark.asyncio
async def test_a_failing_poll_backs_off_instead_of_retrying_every_minute(channel, monkeypatch):
    """A refused password was tried again at the plain cadence, sixty times an hour."""
    transport, imap, _ = channel
    imap.fail_connect = True
    transport._cursor = 0
    transport._receiving = True
    sleeps: list[float] = []

    async def fake_sleep(secs):
        sleeps.append(secs)
        if len(sleeps) >= 3:
            transport._stopping = True

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await transport._poll_loop()
    assert sleeps == [60.0, 120.0, 240.0]


@pytest.mark.asyncio
async def test_inbound_off_reports_the_outbound_half(channel):
    transport, _, _ = channel
    ProviderSettings.update(_APP, {"dm_activation": "off"})
    reload_settings()
    health = await transport.health()
    assert health == {"state": "ready", "detail": "SMTP smtp.test"}
