"""A first connection answers nobody whose mail was already there.

The UID cursor started at 0, so the first poll dispatched the whole folder: on the fake
mailbox, "I don't recognize you yet…" went from the owner's own address to every one of the
ten senders already in the inbox — a school newsletter and a no-reply address among them,
one sender twice — and each raised an owner notification. The UIDVALIDITY branch already
started from the newest message; a first connection now does the same. Mail that arrives
after it is the channel's, and a stranger's first message still gets its one reply.

Trust runs through the REAL core door into the isolated tmp home, as in test_transport.py;
IMAP and SMTP are the injected fakes.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from personalclaw.sdk.channel import ProviderSettings, save_credential

from email_runtime.delivery import EmailDelivery, ThreadStore
from email_runtime.settings import CRED_IMAP_PASS, reload_settings
from email_runtime.transport import EmailTransport
from _fakes import FakeImapServer, FakeSmtpServer, FakeState, build_message

_APP = "email-channel"
NOOR = "noor@haddad-okoye.test"

#: What a real inbox holds when the channel is set up: people, a list, a no-reply sender,
#: and one person who wrote twice. Eleven messages from ten senders, like the fake mailbox.
SEEDED = [
    ("office@school.test", {"List-Id": "<news.school.test>"}),
    ("mateus@dufferin.test", {}),
    ("quotes@birchline.test", {}),
    ("estimates@carrow.test", {}),
    ("talks@pyto.test", {}),
    ("noreply@bookings.test", {}),
    ("sam@haddad-okoye.test", {}),
    ("sam@haddad-okoye.test", {}),
    ("notifications@github.test", {"Precedence": "bulk"}),
    ("hello@cp.test", {}),
    ("frank@example.test", {}),
]


class _Services:
    """The gateway-services handle, faked at the seam the transport calls: the REAL door."""

    def __init__(self, state, captured) -> None:
        self.dashboard_state = state
        self._captured = captured

    def register_channel_delivery(self, delivery) -> None:
        return None

    async def deliver_channel_inbound(self, provider, msg, *, is_dm=True):
        from personalclaw.channel_inbound import deliver_inbound

        async def turn_runner(state, session, text):
            self._captured.append(text)

        return await deliver_inbound(self, provider, msg, is_dm=is_dm, turn_runner=turn_runner)


@pytest.fixture
def fresh(tmp_path):
    """A transport set up on a mailbox that already holds mail, and never connected before."""
    ProviderSettings.update(
        _APP,
        {
            "imap_host": "imap.test", "imap_port": 993, "imap_user": NOOR,
            "smtp_host": "smtp.test", "smtp_port": 587, "smtp_user": NOOR,
            "address": NOOR, "folder": "INBOX", "poll_secs": 60, "dm_activation": "always",
        },
    )
    save_credential(CRED_IMAP_PASS, "app-password")
    reload_settings()
    from personalclaw.channel_inbound import reset_admissions

    reset_admissions()
    imap = FakeImapServer()
    for uid, (sender, headers) in enumerate(SEEDED, start=1):
        imap.add(
            uid,
            build_message(
                from_addr=sender, to_addr=NOOR, message_id=f"<seed{uid}@test>",
                plain=f"message {uid}", extra_headers=headers,
            ),
        )
    smtp = FakeSmtpServer()
    state = FakeState()
    captured: list[str] = []
    transport = EmailTransport()
    transport._client_factory = lambda settings, password: imap
    transport._sender_factory = lambda settings, password: smtp
    transport._services = _Services(state, captured)
    transport._delivery = EmailDelivery(
        smtp, NOOR, owner_id=NOOR,
        threads=ThreadStore(path_provider=lambda: tmp_path / "threads.json"),
    )
    # What start_inbound does: the cursor comes from the app's data dir, and there is none.
    transport._cursor, transport._uidvalidity = transport._load_cursor()
    yield transport, imap, smtp, state, captured
    reset_admissions()


@pytest.mark.asyncio
async def test_the_first_poll_answers_nobody_whose_mail_was_already_there(fresh):
    transport, imap, smtp, state, captured = fresh
    await transport._poll_once(transport._settings())
    await asyncio.sleep(0)

    assert smtp.sent == [], f"mailed {[str(m['To']) for m in smtp.sent]} from {NOOR}"
    assert state.notified == [], "the owner was told about senders who never wrote to the agent"
    assert captured == [], "mail that was already there became a turn"
    assert imap.fetch_calls == [], "a first connection reads nothing"
    assert transport._cursor == len(SEEDED)
    stored = json.loads(transport._cursor_path().read_text(encoding="utf-8"))
    assert stored["last_uid"] == len(SEEDED)


@pytest.mark.asyncio
async def test_mail_after_the_first_poll_is_the_channels(fresh):
    """The floor: the channel still answers — once — a stranger who writes after setup."""
    transport, imap, smtp, state, _ = fresh
    await transport._poll_once(transport._settings())
    imap.add(len(SEEDED) + 1, build_message(from_addr="dana@example.test", to_addr=NOOR,
                                            message_id="<new1@test>", plain="hello?"))
    await transport._poll_once(transport._settings())
    await asyncio.sleep(0)

    assert [str(m["To"]) for m in smtp.sent] == ["dana@example.test"]
    assert "pairing code" in smtp.body_text()
    assert len(state.notified) == 1


@pytest.mark.asyncio
async def test_a_restart_keeps_the_start_it_recorded(fresh):
    transport, imap, smtp, state, _ = fresh
    await transport._poll_once(transport._settings())

    restarted = EmailTransport()
    restarted._client_factory = lambda settings, password: imap
    restarted._sender_factory = lambda settings, password: smtp
    restarted._services = transport._services
    restarted._delivery = transport._delivery
    restarted._cursor, restarted._uidvalidity = restarted._load_cursor()
    assert restarted._cursor == len(SEEDED)

    await restarted._poll_once(restarted._settings())
    assert smtp.sent == [], "a restart re-answered the mail that was already there"


@pytest.mark.asyncio
async def test_a_first_connection_that_fails_sets_no_start(fresh):
    """Nothing was read, so nothing is recorded: the next cycle that connects starts after
    the newest message, and still answers nobody."""
    transport, imap, smtp, _, _ = fresh
    imap.fail_connect = True
    await transport._poll_once(transport._settings())
    assert transport._cursor is None
    assert not transport._cursor_path().exists()

    imap.fail_connect = False
    await transport._poll_once(transport._settings())
    assert transport._cursor == len(SEEDED) and smtp.sent == []


@pytest.mark.asyncio
async def test_an_empty_folder_starts_at_zero_and_answers_its_first_mail(fresh):
    transport, imap, smtp, _, _ = fresh
    imap.messages["INBOX"] = {}
    await transport._poll_once(transport._settings())
    assert transport._cursor == 0

    imap.add(1, build_message(from_addr="dana@example.test", to_addr=NOOR,
                              message_id="<first@test>", plain="hi"))
    await transport._poll_once(transport._settings())
    assert [str(m["To"]) for m in smtp.sent] == ["dana@example.test"]


@pytest.mark.asyncio
async def test_start_inbound_reads_no_cursor_as_never_connected(fresh, monkeypatch):
    transport, _, _, state, _ = fresh
    transport._cursor = 99  # whatever the instance held before

    async def _no_loop():
        return None

    monkeypatch.setattr(transport, "_poll_loop", _no_loop)
    await transport.start_inbound(_Services(state, []))
    assert transport._cursor is None
    await transport.stop_inbound()
