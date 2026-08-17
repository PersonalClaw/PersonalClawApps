"""The outbound half of mail-inbox (EIAT-3, contract C3): compose a threaded reply, and
**draft it by default**.

Guardrail 4 — draft-by-default — is the shape of this module, not a flag inside it. Every
reply is COMPOSED first and only then asked whether it may leave the machine, so
"composed but not sent" is the normal path and a real send is the exception a user opted
into. Three independent conditions each force a draft, and they are checked in this order
so the reason a user is shown is the most specific true one:

1. an explicit ``dry_run=True`` from the caller (the observe-mode request the provider's
   ``supports_dry_run`` advertises);
2. the PLATFORM's live-writes posture — ``PERSONALCLAW_DISABLE_LIVE_WRITES`` (core's
   AUTONOMY-GUARDRAILS §1.4 process-wide destructive-write kill);
3. the app's own ``send_enabled`` setting, which **defaults to False** — so a freshly
   installed, fully configured mailbox with a valid SMTP password still sends nothing;
4. an incomplete SMTP config or a missing SMTP credential — fail closed, and say which.

In every one of those cases NOTHING reaches a socket: no connection is opened, no
sender is constructed. The composed message is written to
``<app data>/drafts/*.eml`` so the work is not lost and the outcome is inspectable — that
file is also what makes "nothing was sent" distinguishable from "nothing happened".

Threading is real, not decorative. A reply carries ``In-Reply-To: <parent Message-ID>``
and a ``References`` chain built per RFC 5322 §3.6.4 (the parent's own chain, then the
parent's id), which is what makes a real client thread it under the original instead of
starting a new conversation. The parent's identity comes from a small persisted
reply-target store written while polling — the ``send_reply`` signature carries only
``(channel_id, text, thread_ts)``, and a recipient can never be guessed from a channel
id, so an unknown target is a REFUSAL rather than a mail to the wrong person.
"""

from __future__ import annotations

import email.utils
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from personalclaw.sdk.channel import atomic_write
from personalclaw.sdk.util import app_data_dir

logger = logging.getLogger(__name__)

_APP = "mail-inbox"

#: Where the reply-target store and the drafts live (under the app's own data dir, which
#: is what the manifest's ``storage`` permission grants).
_TARGETS_FILE = "reply_targets.json"
_DRAFTS_DIR = "drafts"

#: Bound both stores so a long-lived mailbox (or a runaway caller) cannot grow them
#: without limit. Oldest entries/files age out.
_MAX_TARGETS = 500
_MAX_DRAFTS = 200

#: Cap on the persisted References chain. A long-running thread's chain grows without
#: bound otherwise, and clients only need enough to place the message.
_MAX_REFERENCES = 20

# ── the platform's live-writes posture ──────────────────────────────────────────────
#: Core's process-wide destructive-write kill (AUTONOMY-GUARDRAILS §1.4). Read from the
#: environment because that IS its wire contract: ``personalclaw.guardrails.writes`` is
#: not an SDK export, and an app may import core only via ``personalclaw.sdk.*``, so the
#: fail-safe parse below MIRRORS ``personalclaw.guardrails.flags.guard_flag`` rather than
#: calling it. Kept deliberately identical: absent ⇒ writes allowed (this is an opt-in
#: ops/test toggle, not a guard-class default-on flag); PRESENT ⇒ only an explicit falsy
#: token turns it off, so a typo keeps the guard ON.
_LIVE_WRITES_ENV = "PERSONALCLAW_DISABLE_LIVE_WRITES"
_EXPLICIT_FALSE = frozenset({"0", "false", "no", "off", "disable", "disabled", "n", "f"})


def live_writes_disabled() -> bool:
    """True when the platform has switched live, hard-to-reverse writes OFF.

    Sending mail is exactly that class of write: outward-facing and irreversible. When
    this is on, a reply is drafted and no SMTP connection is opened at all."""
    raw = os.environ.get(_LIVE_WRITES_ENV)
    if raw is None:
        return False
    return raw.strip().lower() not in _EXPLICIT_FALSE


# ── posture ─────────────────────────────────────────────────────────────────────────
#: Reasons a reply was drafted instead of sent. Carried on :class:`ReplyOutcome` and into
#: the SEL row, so "why did my reply not go out" has one answer per cause.
DRAFT_DRY_RUN = "dry_run requested by the caller"
DRAFT_LIVE_WRITES_DISABLED = f"{_LIVE_WRITES_ENV} is set — live writes are disabled"
DRAFT_SEND_DISABLED = "sending is off (send_enabled=false) — draft-by-default"
DRAFT_NO_SMTP_CONFIG = "SMTP host/login not configured"
DRAFT_NO_CREDENTIAL = "no SMTP password in the credential store"
SEND_FAILED = "SMTP send failed"
NO_TARGET = "no known reply target for this channel/thread"


def draft_reason(
    *,
    send_enabled: bool,
    smtp_ready: bool,
    has_credential: bool,
    dry_run: bool = False,
) -> str:
    """The reason this reply must be DRAFTED, or ``""`` when it may be sent.

    The single decision point for guardrail 4. Every unknown or incomplete state resolves
    to a draft — there is no input combination that sends by accident, and the default
    (``send_enabled=False``) sends nothing."""
    if dry_run:
        return DRAFT_DRY_RUN
    if live_writes_disabled():
        return DRAFT_LIVE_WRITES_DISABLED
    if not send_enabled:
        return DRAFT_SEND_DISABLED
    if not smtp_ready:
        return DRAFT_NO_SMTP_CONFIG
    if not has_credential:
        return DRAFT_NO_CREDENTIAL
    return ""


@dataclass
class ReplyOutcome:
    """What actually happened to one reply. ``send_reply`` narrows this to its ``bool``.

    ``sent`` and ``drafted`` are independent on purpose: a send that FAILED is
    ``sent=False, drafted=True`` (the composed message is preserved rather than lost),
    and a refusal with no known recipient is neither."""

    sent: bool = False
    drafted: bool = False
    reason: str = ""
    draft_path: str = ""
    #: The composed message. Present whenever composition happened — the proof that
    #: draft mode composed something rather than doing nothing at all.
    message: EmailMessage | None = None


# ── the reply-target store ──────────────────────────────────────────────────────────
@dataclass
class ReplyTarget:
    """Everything needed to answer one polled message, remembered at poll time.

    ``channel_id`` is the address the mail arrived AT (a bound address or the mailbox
    address) — the same value the provider puts on ``IncomingMessage.channel_id``, and
    the ``From`` the reply goes out as, so a reply to a purpose address keeps that
    address's identity instead of leaking the mailbox login."""

    channel_id: str
    #: The parent's ``Message-ID`` — the ``In-Reply-To`` value and the lookup key.
    message_id: str
    to_addr: str
    subject: str = ""
    references: list[str] = field(default_factory=list)
    ts: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "message_id": self.message_id,
            "to_addr": self.to_addr,
            "subject": self.subject,
            "references": list(self.references),
            "ts": self.ts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReplyTarget":
        refs = data.get("references") or []
        return cls(
            channel_id=str(data.get("channel_id", "")),
            message_id=str(data.get("message_id", "")),
            to_addr=str(data.get("to_addr", "")),
            subject=str(data.get("subject", "")),
            references=[str(r) for r in refs if r][-_MAX_REFERENCES:],
            ts=float(data.get("ts", 0.0) or 0.0),
        )


def _targets_path() -> Path:
    return app_data_dir(_APP) / _TARGETS_FILE


def load_targets() -> list[ReplyTarget]:
    """Every remembered reply target, oldest first. A missing/corrupt file reads empty."""
    try:
        data = json.loads(_targets_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = data.get("targets", []) if isinstance(data, dict) else []
    return [ReplyTarget.from_dict(r) for r in rows if isinstance(r, dict)]


def remember_target(target: ReplyTarget) -> None:
    """Persist one reply target (newest last, bounded, de-duplicated on Message-ID)."""
    if not target.message_id or not target.to_addr:
        # Without both there is nothing to reply TO and nothing to thread UNDER; a
        # half-row would later look like a usable target.
        return
    rows = [r for r in load_targets() if r.message_id != target.message_id]
    rows.append(target)
    if len(rows) > _MAX_TARGETS:
        rows = rows[-_MAX_TARGETS:]
    try:
        atomic_write(
            _targets_path(), json.dumps({"targets": [r.to_dict() for r in rows]}) + "\n"
        )
    except OSError:
        logger.debug("mail-inbox: failed to persist reply targets", exc_info=True)


def lookup_target(channel_id: str, thread_ts: str | None = None) -> ReplyTarget | None:
    """The message a reply on *channel_id* should answer, or None.

    ``thread_ts`` carries the parent's ``Message-ID`` when the caller knows it (that is
    what the provider puts on ``IncomingMessage.id``). Without it, fall back to the most
    recent message on that channel — never to "some message", and never across channels:
    replying to the wrong sender is worse than not replying."""
    rows = load_targets()
    if thread_ts:
        wanted = thread_ts.strip()
        for row in reversed(rows):
            if row.message_id == wanted:
                return row
        # An explicit id that is not known is a refusal, not an invitation to guess.
        return None
    for row in reversed(rows):
        if row.channel_id == channel_id:
            return row
    return None


# ── composition ─────────────────────────────────────────────────────────────────────
def reply_subject(subject: str) -> str:
    """``Re:``-prefix the parent subject, without stacking ``Re: Re:``."""
    base = (subject or "").strip()
    if not base:
        return "Re:"
    if base[:3].lower() == "re:":
        return base
    return f"Re: {base}"


def reference_chain(target: ReplyTarget) -> list[str]:
    """The reply's ``References``: the parent's chain, then the parent's own id.

    RFC 5322 §3.6.4. Order is preserved and duplicates are dropped — a client walks this
    to place the message, and a repeated id in a long thread makes the chain grow without
    adding information."""
    chain: list[str] = []
    for ref in list(target.references) + [target.message_id]:
        ref = (ref or "").strip()
        if ref and ref not in chain:
            chain.append(ref)
    return chain[-_MAX_REFERENCES:]


def compose_reply(target: ReplyTarget, text: str, *, from_addr: str = "") -> EmailMessage:
    """Build the reply. Pure — it touches no network and no disk.

    Called BEFORE the posture is resolved, which is what makes draft-by-default provable:
    the message exists either way, and only delivery is conditional."""
    sender = (from_addr or target.channel_id).strip()
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = target.to_addr
    msg["Subject"] = reply_subject(target.subject)
    msg["Date"] = email.utils.formatdate(localtime=True)
    domain = sender.partition("@")[2] or None
    msg["Message-ID"] = email.utils.make_msgid(domain=domain)
    if target.message_id:
        # The two headers that make this a REPLY rather than a new thread.
        msg["In-Reply-To"] = target.message_id
        msg["References"] = " ".join(reference_chain(target))
    msg.set_content(text or "")
    return msg


# ── drafts ──────────────────────────────────────────────────────────────────────────
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def drafts_dir() -> Path:
    return app_data_dir(_APP) / _DRAFTS_DIR


def _draft_name(msg: EmailMessage) -> str:
    # The reply's own Message-ID is unique by construction, so two drafts can never
    # collide; the timestamp prefix just makes the directory readable in order.
    token = _SAFE_NAME.sub("-", str(msg.get("Message-ID", "")).strip("<>")) or "reply"
    return f"{int(time.time())}-{token[:80]}.eml"


def _prune_drafts(directory: Path) -> None:
    try:
        files = sorted(directory.glob("*.eml"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for stale in files[:-_MAX_DRAFTS] if len(files) > _MAX_DRAFTS else []:
        try:
            stale.unlink()
        except OSError:
            logger.debug("mail-inbox: could not prune draft %s", stale, exc_info=True)


def save_draft(msg: EmailMessage) -> str:
    """Write the composed message as a real ``.eml`` and return its path (``""`` on
    failure — a draft that cannot be written must not read as one that was)."""
    directory = drafts_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / _draft_name(msg)
        atomic_write(path, msg.as_string())
    except OSError:
        logger.warning("mail-inbox: could not write the reply draft", exc_info=True)
        return ""
    _prune_drafts(directory)
    return str(path)
