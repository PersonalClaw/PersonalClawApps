"""Outbound replies (EIAT-3, contract C3, guardrail 4).

Covers the EIAT-3 done-when clause by clause:

- a reply is COMPOSED but NOT sent while draft-mode is on — and the vacuity floor for that
  claim is asserted too: a real, parseable ``.eml`` with the reply body and the threading
  headers exists on disk, so "nothing was sent" cannot pass because nothing happened;
- enabling sending delivers and threads correctly: the captured message's ``In-Reply-To``
  and ``References`` values are asserted, not merely the fact that a send was attempted;
- the platform's live-writes/dry-run posture is honoured — an explicit ``dry_run``, and
  ``PERSONALCLAW_DISABLE_LIVE_WRITES``, each force a draft and construct no sender at all.

**No test here sends real mail.** Every send goes through the injected
``FakeSmtpSender``, which captures the ``EmailMessage`` in memory; a socket is never
opened. ``sender.sent == []`` is therefore the assertion that nothing left the machine.
"""

from __future__ import annotations

import asyncio
import email
import email.policy
from pathlib import Path

from mail_inbox_runtime import outbound
from mail_inbox_runtime.outbound import (
    DRAFT_DRY_RUN,
    DRAFT_LIVE_WRITES_DISABLED,
    DRAFT_NO_CREDENTIAL,
    DRAFT_NO_SMTP_CONFIG,
    DRAFT_SEND_DISABLED,
    NO_TARGET,
    lookup_target,
    reference_chain,
    reply_subject,
)
from mail_inbox_runtime.provider import MailInboxProvider
from mail_inbox_runtime.settings import CRED_MAIL_PASSWORD, CRED_SMTP_PASSWORD, _APP
from mail_inbox_runtime.smtp_client import SmtpError

from _fakes import FakeImapClient, FakeSmtpSender, build_message

FOLDER = "INBOX"
MAILBOX = "me@example.com"
CORRESPONDENT = "sender@example.com"
PARENT_ID = "<parent@example.com>"
ROOT_ID = "<root@example.com>"
LIVE_WRITES_ENV = "PERSONALCLAW_DISABLE_LIVE_WRITES"


def _configure(
    *,
    send_enabled: bool | None = False,
    smtp_host: str = "smtp.example.com",
    smtp_password: str = "smtp-app-password",
    bound: list | None = None,
) -> None:
    """Write a fully configured mailbox + outbound transport. ``send_enabled=None`` OMITS
    the key entirely, which is how a real fresh install looks — the state the shipped
    default has to cover."""
    from personalclaw.config.loader import save_credential
    from personalclaw.sdk.settings import ProviderSettings

    cfg = {
        "host": "imap.example.com",
        "port": 993,
        "username": MAILBOX,
        "address": MAILBOX,
        "folder": FOLDER,
        "allow_senders": ["*@example.com"],
        "smtp_host": smtp_host,
        "smtp_port": 587,
        "smtp_security": "starttls",
    }
    if send_enabled is not None:
        cfg["send_enabled"] = send_enabled
    if bound is not None:
        cfg["bound_addresses"] = bound
    ProviderSettings.update(_APP, cfg)
    save_credential(CRED_MAIL_PASSWORD, "imap-app-password")
    if smtp_password:
        save_credential(CRED_SMTP_PASSWORD, smtp_password)


def _provider(**kwargs) -> tuple[MailInboxProvider, FakeSmtpSender]:
    _configure(**kwargs)
    provider = MailInboxProvider()
    sender = FakeSmtpSender()
    provider._sender_factory = lambda settings, password: sender
    return provider, sender


def _poll_one(provider: MailInboxProvider, *, to_addr: str = MAILBOX, references: str = ""):
    """Poll one parent message, which is what records the reply target."""
    raw = build_message(
        from_addr=CORRESPONDENT,
        to_addr=to_addr,
        subject="Quarterly report",
        message_id=PARENT_ID,
        plain="the original body",
        in_reply_to=ROOT_ID,
        references=references,
    )
    provider._client_factory = lambda settings, password: FakeImapClient({FOLDER: {5: raw}})
    messages, _ = asyncio.run(provider.poll([], {}, MAILBOX))
    return messages


def _reply(provider, *args, **kwargs):
    return asyncio.run(provider.reply(*args, **kwargs))


# ── the guardrail: draft-by-default ─────────────────────────────────────────────────
def test_reply_is_composed_but_not_sent_while_draft_mode_is_on():
    """The headline clause. Nothing is sent — AND something was really composed."""
    provider, sender = _provider()  # send_enabled defaults to False: the shipped posture
    assert _poll_one(provider), "precondition: the parent message must have been surfaced"

    outcome = _reply(provider, MAILBOX, "Thanks - reading it now.")

    # Nothing left the machine.
    assert sender.sent == []
    assert outcome.sent is False
    assert outcome.drafted is True
    assert outcome.reason == DRAFT_SEND_DISABLED

    # VACUITY FLOOR: a real message was composed and persisted, so "nothing sent" cannot
    # pass by nothing having happened at all.
    assert outcome.message is not None
    assert outcome.draft_path, "draft mode must leave the composed reply on disk"
    draft = email.message_from_string(
        Path(outcome.draft_path).read_text(encoding="utf-8"), policy=email.policy.default
    )
    assert draft["In-Reply-To"] == PARENT_ID
    assert draft["To"] == CORRESPONDENT
    assert draft["Subject"] == "Re: Quarterly report"
    assert draft.get_content().strip() == "Thanks - reading it now."


def test_send_enabled_defaults_to_false_in_a_fully_configured_app():
    """Guardrail 4 as a property of the settings, not only of the send path: a mailbox with
    a valid SMTP host and password but NO ``send_enabled`` key — a fresh install — still
    reads False, and a non-boolean value stays on the safe side."""
    from mail_inbox_runtime.settings import MailInboxSettings
    from personalclaw.sdk.settings import ProviderSettings

    _configure(send_enabled=None)  # the key is absent, as it is on a fresh install
    assert "send_enabled" not in ProviderSettings.load(_APP)
    assert MailInboxSettings.load().send_enabled is False
    assert MailInboxSettings.load().smtp_ready is True  # configured, but still drafting

    ProviderSettings.update(_APP, {"send_enabled": "yes"})  # not a real boolean
    assert MailInboxSettings.load().send_enabled is False


def test_send_reply_bool_is_false_while_drafting_and_true_once_enabled():
    """The ABC's ``bool`` never claims delivery for a draft, and ``send_enabled`` is read
    live — flipping it between calls changes the outcome with no restart."""
    from personalclaw.sdk.settings import ProviderSettings

    provider, sender = _provider()
    _poll_one(provider)

    assert asyncio.run(provider.send_reply(MAILBOX, "drafted")) is False
    assert sender.sent == []

    ProviderSettings.update(_APP, {"send_enabled": True})
    assert asyncio.run(provider.send_reply(MAILBOX, "sent for real")) is True
    assert len(sender.sent) == 1


# ── threading ───────────────────────────────────────────────────────────────────────
def test_enabling_sending_delivers_and_threads_via_in_reply_to():
    provider, sender = _provider(send_enabled=True)
    _poll_one(provider)

    outcome = _reply(provider, MAILBOX, "On it.")

    assert outcome.sent is True and outcome.drafted is False
    assert len(sender.sent) == 1
    msg = sender.sent[0]
    # The exact header VALUES — this is what makes a real client thread the reply.
    assert msg["In-Reply-To"] == PARENT_ID
    assert msg["References"].split() == [ROOT_ID, PARENT_ID]
    assert msg["Subject"] == "Re: Quarterly report"
    assert msg["To"] == CORRESPONDENT
    assert msg["From"] == MAILBOX
    assert msg["Date"]
    # The reply gets its own id — reusing the parent's would collide in every client.
    assert msg["Message-ID"] and msg["Message-ID"] != PARENT_ID
    assert msg.get_content().strip() == "On it."


def test_references_chain_extends_the_parents_chain():
    """RFC 5322 §3.6.4: the parent's chain, then the parent's own id, in order."""
    provider, sender = _provider(send_enabled=True)
    _poll_one(provider, references=f"{ROOT_ID} <mid@example.com>")

    _reply(provider, MAILBOX, "ack")

    assert sender.sent[0]["References"].split() == [ROOT_ID, "<mid@example.com>", PARENT_ID]


def test_reply_from_a_bound_address_keeps_that_identity():
    """Mail to a prompt-bound address is answered AS that address, not as the mailbox
    login — otherwise a purpose address leaks the account behind it on first reply."""
    bound = [
        {
            "name": "Travel",
            "address": "me+travel@example.com",
            "default_prompt": "Build my itinerary.",
            "enabled": True,
            "allow_senders": ["*@example.com"],
        }
    ]
    provider, sender = _provider(send_enabled=True, bound=bound)
    _poll_one(provider, to_addr="me+travel@example.com")

    outcome = _reply(provider, "me+travel@example.com", "ack")

    assert outcome.sent is True
    assert sender.sent[0]["From"] == "me+travel@example.com"
    assert sender.sent[0]["To"] == CORRESPONDENT


def test_reply_subject_does_not_stack_re():
    assert reply_subject("Hello") == "Re: Hello"
    assert reply_subject("Re: Hello") == "Re: Hello"
    assert reply_subject("RE: Hello") == "RE: Hello"
    assert reply_subject("") == "Re:"


def test_reference_chain_dedupes_without_reordering():
    target = outbound.ReplyTarget(
        channel_id=MAILBOX, message_id=PARENT_ID, to_addr=CORRESPONDENT,
        references=[ROOT_ID, PARENT_ID, ROOT_ID],
    )
    assert reference_chain(target) == [ROOT_ID, PARENT_ID]


# ── the platform's live-writes / dry-run posture ────────────────────────────────────
def test_provider_declares_dry_run_support():
    assert MailInboxProvider().supports_dry_run is True


def test_dry_run_drafts_and_constructs_no_sender_at_all():
    """The strongest form of "nothing leaves the machine": the SMTP sender is never even
    built, so no socket can be opened by any code path."""
    provider, sender = _provider(send_enabled=True)
    _poll_one(provider)
    built: list[str] = []

    def factory(settings, password):
        built.append(password)
        return sender

    provider._sender_factory = factory

    outcome = _reply(provider, MAILBOX, "preview only", dry_run=True)

    assert built == []
    assert sender.sent == []
    assert outcome.drafted is True and outcome.sent is False
    assert outcome.reason == DRAFT_DRY_RUN
    assert outcome.message is not None  # composed anyway — the preview is real


def test_live_writes_disabled_forces_a_draft(monkeypatch):
    monkeypatch.setenv(LIVE_WRITES_ENV, "1")
    provider, sender = _provider(send_enabled=True)
    _poll_one(provider)

    outcome = _reply(provider, MAILBOX, "hi")

    assert sender.sent == []
    assert outcome.drafted is True and outcome.sent is False
    assert outcome.reason == DRAFT_LIVE_WRITES_DISABLED


def test_unknown_live_writes_token_fails_safe(monkeypatch):
    """A typo in the guard flag keeps the guard ON — mirroring core's ``guard_flag``."""
    monkeypatch.setenv(LIVE_WRITES_ENV, "maybe")
    provider, sender = _provider(send_enabled=True)
    _poll_one(provider)

    assert _reply(provider, MAILBOX, "hi").reason == DRAFT_LIVE_WRITES_DISABLED
    assert sender.sent == []


def test_explicitly_falsy_live_writes_flag_does_not_block(monkeypatch):
    monkeypatch.setenv(LIVE_WRITES_ENV, "0")
    provider, sender = _provider(send_enabled=True)
    _poll_one(provider)

    assert _reply(provider, MAILBOX, "hi").sent is True
    assert len(sender.sent) == 1


# ── fail-closed on incomplete outbound config ───────────────────────────────────────
def test_missing_smtp_credential_fails_closed_and_never_borrows_the_imap_one():
    provider, sender = _provider(send_enabled=True, smtp_password="")
    _poll_one(provider)

    outcome = _reply(provider, MAILBOX, "hi")

    assert sender.sent == []
    assert outcome.reason == DRAFT_NO_CREDENTIAL
    # The IMAP password IS present — it must not be reused for the outbound transport.
    assert MailInboxProvider._resolve_password() == "imap-app-password"
    assert MailInboxProvider._resolve_smtp_password() == ""


def test_unconfigured_smtp_host_fails_closed():
    provider, sender = _provider(send_enabled=True, smtp_host="")
    _poll_one(provider)

    assert _reply(provider, MAILBOX, "hi").reason == DRAFT_NO_SMTP_CONFIG
    assert sender.sent == []


# ── refusals: never guess a recipient ───────────────────────────────────────────────
def test_unknown_channel_is_refused_never_guessed():
    provider, sender = _provider(send_enabled=True)
    _poll_one(provider)

    outcome = _reply(provider, "stranger@example.com", "hi")

    assert outcome.sent is False and outcome.drafted is False
    assert outcome.reason == NO_TARGET
    assert outcome.message is None  # nothing composed against a guessed address
    assert sender.sent == []
    assert list(outbound.drafts_dir().glob("*.eml")) == []


def test_explicit_unknown_thread_id_is_refused_not_downgraded():
    """An explicit ``thread_ts`` that is not known must NOT fall back to the channel's most
    recent message — that would answer the wrong mail while looking correct."""
    provider, sender = _provider(send_enabled=True)
    _poll_one(provider)

    outcome = _reply(provider, MAILBOX, "hi", "<never-seen@example.com>")

    assert outcome.reason == NO_TARGET
    assert sender.sent == []


def test_smtp_failure_keeps_the_reply_as_a_draft():
    provider, _ = _provider(send_enabled=True)
    _poll_one(provider)
    failing = FakeSmtpSender(error=SmtpError("SMTP send failed: 421 service unavailable"))
    provider._sender_factory = lambda settings, password: failing

    outcome = _reply(provider, MAILBOX, "hi")

    assert outcome.sent is False
    assert outcome.drafted is True  # the composed reply is preserved, not lost
    assert "SMTP send failed" in outcome.reason
    assert Path(outcome.draft_path).exists()


# ── the reply-target store ──────────────────────────────────────────────────────────
def test_poll_records_the_reply_target():
    provider, _ = _provider()
    _poll_one(provider)

    target = lookup_target(MAILBOX)

    assert target is not None
    assert target.to_addr == CORRESPONDENT
    assert target.message_id == PARENT_ID
    assert target.subject == "Quarterly report"
    assert target.references == [ROOT_ID]
    assert target.channel_id == MAILBOX


def test_target_lookup_never_crosses_channels():
    provider, _ = _provider()
    _poll_one(provider)
    assert lookup_target("someone-else@example.com") is None


def test_targets_are_deduped_on_message_id():
    provider, _ = _provider()
    _poll_one(provider)
    _poll_one(provider)  # the same Message-ID again
    assert [t.message_id for t in outbound.load_targets()] == [PARENT_ID]


# ── audit ───────────────────────────────────────────────────────────────────────────
def test_drafted_reply_is_audited_in_the_sel():
    provider, _ = _provider()
    _poll_one(provider)
    _reply(provider, MAILBOX, "hi")

    from personalclaw.sel import sel

    rows = [e for e in sel().recent(limit=100) if e.get("operation") == "mail_reply_drafted"]
    assert len(rows) == 1
    assert CORRESPONDENT in rows[0].get("resources", "")
    assert PARENT_ID in rows[0].get("resources", "")
    assert DRAFT_SEND_DISABLED in rows[0].get("error", "")


def test_sent_reply_is_audited_in_the_sel():
    provider, _ = _provider(send_enabled=True)
    _poll_one(provider)
    _reply(provider, MAILBOX, "hi")

    from personalclaw.sel import sel

    rows = [e for e in sel().recent(limit=100) if e.get("operation") == "mail_reply_sent"]
    assert len(rows) == 1
    assert CORRESPONDENT in rows[0].get("resources", "")


def test_refused_reply_is_audited_in_the_sel():
    provider, _ = _provider(send_enabled=True)
    _reply(provider, "nobody@example.com", "hi")

    from personalclaw.sel import sel

    rows = [e for e in sel().recent(limit=100) if e.get("operation") == "mail_reply_refused"]
    assert len(rows) == 1
    assert NO_TARGET in rows[0].get("error", "")


def test_no_credential_ever_reaches_the_audit_trail_or_the_draft():
    """The SMTP password must not appear in any artifact a reply produces."""
    provider, _ = _provider(send_enabled=True)
    _poll_one(provider)
    outcome = _reply(provider, MAILBOX, "hi")

    from personalclaw.sel import sel

    blob = repr(sel().recent(limit=100))
    assert "smtp-app-password" not in blob
    assert "imap-app-password" not in blob
    if outcome.draft_path:
        assert "smtp-app-password" not in Path(outcome.draft_path).read_text(encoding="utf-8")
