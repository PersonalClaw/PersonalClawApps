"""Mail a program sent is never answered (RFC 3834; ledger 250).

The channel answered every message that reached it: an unknown sender got the pairing nudge,
an allowed one a turn whose reply went back to them. For mail a program sent that is
backscatter at best — a reply to a no-reply address or to a list's thousand readers — and a
loop at worst, when the other side is an auto-responder too. RFC 3834 §2 forbids answering
``Auto-Submitted`` mail (other than ``no``) and mail with a null ``Return-Path``, and names
the conventions beside them: ``Precedence: bulk/list/junk``, mailing-list headers, and
no-reply or daemon senders. Such mail never reaches the door now: no reply, no turn, no
owner notification — while a person's mail, and an explicit ``Auto-Submitted: no``, still do.
"""

from __future__ import annotations

import asyncio

import pytest

from personalclaw.sdk.channel import ProviderSettings, allow_sender, create_pairing_code
from personalclaw.sdk.channel import is_allowed_sender, save_credential

from email_runtime import inbound_tap
from email_runtime.delivery import EmailDelivery, ThreadStore
from email_runtime.mime import parse_inbound
from email_runtime.settings import CRED_IMAP_PASS, reload_settings
from email_runtime.transport import EmailTransport
from _fakes import FakeImapServer, FakeSmtpServer, FakeState, build_message, raw_message

_APP = "email-channel"
AGENT = "agent@example.test"

AUTOMATED = [
    pytest.param("dana@example.test", {"Auto-Submitted": "auto-replied"}, id="auto-replied"),
    pytest.param("dana@example.test", {"Auto-Submitted": "auto-generated"}, id="auto-generated"),
    pytest.param("dana@example.test", {"Auto-Submitted": "auto-notified; owner-email=x@y"},
                 id="auto-notified-with-params"),
    pytest.param("news@example.test", {"Precedence": "bulk"}, id="precedence-bulk"),
    pytest.param("news@example.test", {"Precedence": "list"}, id="precedence-list"),
    pytest.param("news@example.test", {"Precedence": "junk"}, id="precedence-junk"),
    pytest.param("news@example.test", {"List-Id": "<parents.school.test>"}, id="list-id"),
    pytest.param("news@example.test", {"List-Unsubscribe": "<mailto:u@example.test>"},
                 id="list-unsubscribe"),
    pytest.param("news@example.test", {"List-Post": "<mailto:list@example.test>"}, id="list-post"),
    pytest.param("noreply@bookings.test", {}, id="noreply"),
    pytest.param("no-reply@accounts.test", {}, id="no-reply"),
    pytest.param("no_reply+42@shop.test", {}, id="no_reply-tagged"),
    pytest.param("do-not-reply@bank.test", {}, id="do-not-reply"),
    pytest.param("donotreply@bank.test", {}, id="donotreply"),
    pytest.param("MAILER-DAEMON@mx.test", {}, id="mailer-daemon"),
    pytest.param("postmaster@mx.test", {}, id="postmaster"),
    pytest.param("parents-request@lists.test", {}, id="list-request"),
    pytest.param("owner-parents@lists.test", {}, id="list-owner"),
    pytest.param("dana@example.test", {"Return-Path": "<>"}, id="null-return-path"),
]


class _Services:
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
def wired(tmp_path):
    ProviderSettings.update(
        _APP,
        {
            "imap_host": "imap.test", "imap_user": AGENT, "smtp_host": "smtp.test",
            "smtp_user": AGENT, "address": AGENT, "folder": "INBOX",
        },
    )
    save_credential(CRED_IMAP_PASS, "app-password")
    reload_settings()
    from personalclaw.channel_inbound import reset_admissions

    reset_admissions()
    imap, smtp, state, captured = FakeImapServer(), FakeSmtpServer(), FakeState(), []
    transport = EmailTransport()
    transport._client_factory = lambda settings, password: imap
    transport._sender_factory = lambda settings, password: smtp
    transport._cursor = 0  # connected once, to an empty folder
    transport._services = _Services(state, captured)
    transport._delivery = EmailDelivery(
        smtp, AGENT, owner_id=AGENT,
        threads=ThreadStore(path_provider=lambda: tmp_path / "threads.json"),
    )
    yield transport, imap, smtp, state, captured
    reset_admissions()


async def _deliver(transport, imap, sender, headers, *, uid=1, body="please answer"):
    imap.add(uid, build_message(from_addr=sender, to_addr=AGENT, message_id=f"<a{uid}@test>",
                                plain=body, extra_headers=headers))
    await transport._poll_once(transport._settings())
    await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize(("sender", "headers"), AUTOMATED)
async def test_automated_mail_from_a_stranger_is_not_answered(wired, sender, headers):
    transport, imap, smtp, state, captured = wired
    await _deliver(transport, imap, sender, headers)
    assert smtp.sent == [], f"answered {sender} {headers}"
    assert state.notified == [], "the owner was told a program is writing to the agent"
    assert captured == []
    assert transport._cursor == 1, "the cursor still moves past it"


@pytest.mark.asyncio
async def test_an_allowed_correspondents_auto_reply_is_no_turn(wired):
    """A vacation reply to something the agent sent. Answering it is the loop RFC 3834 is for."""
    transport, imap, smtp, _, captured = wired
    allow_sender("email", "sam@example.test", "Sam")
    await _deliver(transport, imap, "sam@example.test", {"Auto-Submitted": "auto-replied"},
                   body="I'm away until Monday.")
    assert captured == [] and smtp.sent == []


@pytest.mark.asyncio
async def test_an_allowed_correspondents_automated_mail_still_reaches_the_automations(wired):
    """The trigger tap answers nobody, so an allowed sender's automated mail is still an event —
    the allowlist gate the door would have applied still applies."""
    transport, imap, _, _, _ = wired
    seen: list[str] = []

    def observer(cm, *, text):
        seen.append(text)

    inbound_tap.subscribe(observer)
    try:
        allow_sender("email", "alerts@github.test", "GitHub")
        await _deliver(transport, imap, "alerts@github.test", {"List-Id": "<repo.github.test>"},
                       body="Build failed on main.", uid=1)
        await _deliver(transport, imap, "stranger@lists.test", {"List-Id": "<x.lists.test>"},
                       body="Spam for everyone.", uid=2)
    finally:
        inbound_tap.unsubscribe(observer)
    assert seen == ["Build failed on main."], "a stranger's automated mail armed an automation"


@pytest.mark.asyncio
async def test_a_code_in_automated_mail_pairs_nobody(wired):
    """An outstanding pairing code quoted by an auto-responder (or a list) must not pair."""
    transport, imap, _, _, _ = wired
    code = create_pairing_code("email")
    await _deliver(transport, imap, "dana@example.test", {"Auto-Submitted": "auto-replied"},
                   body=f"Your message said {code}")
    assert is_allowed_sender("email", "dana@example.test") is False


@pytest.mark.asyncio
async def test_a_person_and_an_explicit_no_are_still_answered(wired):
    """The floor: the same stranger, writing as a person, gets the one pairing reply."""
    transport, imap, smtp, state, _ = wired
    await _deliver(transport, imap, "dana@example.test", {"Auto-Submitted": "no"})
    assert [str(m["To"]) for m in smtp.sent] == ["dana@example.test"]
    assert len(state.notified) == 1


class TestAutomatedReason:
    @pytest.mark.parametrize(("sender", "headers"), AUTOMATED)
    def test_each_kind_is_named(self, sender, headers):
        mail = parse_inbound(build_message(from_addr=sender, extra_headers=headers), 1)
        assert mail is not None and mail.automated

    def test_the_reason_says_why(self):
        raw = build_message(from_addr="news@example.test", extra_headers={"Precedence": "Bulk"})
        mail = parse_inbound(raw, 1)
        assert mail is not None
        assert mail.automated == "it was sent to many (Precedence: bulk)"

    @pytest.mark.parametrize(
        "sender",
        ["sam@example.test", "replies@example.test", "norah@example.test", "noreplyfan@x.test"],
    )
    def test_a_person_is_not_automated(self, sender):
        mail = parse_inbound(build_message(from_addr=sender), 1)
        assert mail is not None and mail.automated == ""

    def test_a_hand_rolled_header_is_read_too(self):
        raw = raw_message(
            "From: Dana <dana@example.test>\r\nTo: agent@example.test\r\n"
            "Subject: Out of office\r\nAuto-Submitted: auto-replied",
            "Back Monday.",
        )
        import email
        import email.policy

        from email_runtime.mime import automated_reason

        msg = email.message_from_bytes(raw, policy=email.policy.default)
        assert automated_reason(msg, "dana@example.test").startswith("it is automatic")
