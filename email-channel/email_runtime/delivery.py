"""EmailDelivery — the app-side ChannelDelivery the gateway delivers through.

All email rendering (subject construction, MIME assembly, the HTML alternative, the
reply-token approval prompt) lives HERE, so core delivers plain text plus structured
intent and never imports an email module. The transport registers an instance onto the
gateway + dashboard at ``start_inbound``.

**Every send crosses a thread boundary.** ``smtplib`` blocks, so :meth:`_send` hands the
message to :func:`asyncio.to_thread`; nothing in this module touches a socket on the
event loop. That is not a nicety — one blocking ``sendmail`` against a slow relay would
freeze every session and WebSocket in the gateway for the duration.

**There is no streaming.** The plan's C3 table marks the streaming trio MUST-NOT for
email, and rightly: a "live-updating message" would mean re-sending a mail per token.
:meth:`start_stream` returns ``""`` and the append/stop calls are no-ops, which is
exactly what core's mirror path checks before animating (``_mirror_stream_ts = await
start_stream(...) or ""``). Because ``ChannelCapabilities`` has no ``streaming`` field,
that falsity is declared as ``edits=False`` — in every other channel streaming rides on
message edits, so no-edits IS no-streaming. The transport's ``capabilities()`` docstring
and a test pin that mapping.

**Threading is the channel's identity.** :class:`ThreadStore` remembers, per thread root
``Message-ID``, the last message id in the chain, the accumulated ``References``, the
subject and the correspondent. An outbound reply sets ``In-Reply-To`` to the last id and
``References`` to the chain, then records its own id — so the third message in a
conversation references both prior ones, in order, and a mail client shows one thread.

**Core masks every text it hands this handle**, keys and exfiltration URLs included, the
subject's words as well as the body and the HTML alternative, before any method here is
called, so nothing here masks it again.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections import OrderedDict
from email.message import EmailMessage
from typing import Any, NamedTuple
from urllib.parse import quote

from personalclaw.sdk.channel import (
    AppConfig,
    approval_brief_for,
    atomic_write,
    is_tracked_channel,
    owner_id_for,
    sel,
)
from personalclaw.sdk.util import app_data_dir

from email_runtime.mime import build_outbound, build_references, reply_subject
from email_runtime.smtp_client import SmtpError, SmtpSender

logger = logging.getLogger(__name__)

_APP = "email-channel"
#: The channel's provider key: core keeps this channel's owner under it (``owner_id_for``).
_PROVIDER = "email"
_THREADS_FILE = "threads.json"
#: Bound the persisted thread map so a long-lived mailbox can't grow it without limit.
#: Oldest entries age out; a thread that falls out simply starts a fresh chain.
MAX_THREADS = 500
#: Reply-token vocabulary. The owner replies with either word plus the token.
APPROVE_WORD = "APPROVE"
DENY_WORD = "DENY"
_TOKEN_BYTES = 4  # 8 hex chars — short enough to retype, wide enough not to collide
#: How many ended approvals are remembered, so a reply to one is answered with how it ended.
_ENDED_KEPT = 256

#: What a reply to an approval that has ended is told, for each ending
#: (``ChannelDelivery.request_approval``). Expired and cancelled are not a Deny: nobody decided.
_LATE_ANSWERS = {
    "approved": "This approval was already approved. Your reply changes nothing.",
    "rejected": "This approval was already rejected. Your reply changes nothing.",
    "expired": (
        "Nobody answered this approval in time, so it did not run. Your reply changes nothing."
    ),
    "cancelled": (
        "This approval was cancelled: the work that asked for it stopped first, so it did not "
        "run. Your reply changes nothing."
    ),
}


def _how_long(minutes: int) -> str:
    """*minutes* in the unit a person says it in: 2 hours, 90 minutes, 7 days."""
    if minutes >= 1440 and minutes % 1440 == 0:
        n, unit = minutes // 1440, "day"
    elif minutes >= 60 and minutes % 60 == 0:
        n, unit = minutes // 60, "hour"
    else:
        n, unit = minutes, "minute"
    return f"{n} {unit}{'' if n == 1 else 's'}"


def _approval_window() -> str:
    """How long PersonalClaw waits for an answer at most (Settings → Approval wait), in words. It
    is what the approval mail says, since this channel keeps no clock of its own. An approval that
    subagent or workflow work asks for can end sooner, with that work's own time limit."""
    try:
        return _how_long(int(AppConfig.load().agent.approval_timeout_minutes))
    except Exception:  # noqa: BLE001 - an unreadable config leaves the sentence without a length
        return ""


class ThreadState:
    """What one email thread needs for the next reply to land in the same conversation."""

    __slots__ = ("root", "last_message_id", "references", "subject", "correspondent", "name")

    def __init__(
        self, root: str, *, last_message_id: str = "", references: str = "",
        subject: str = "", correspondent: str = "", name: str = "",
    ) -> None:
        self.root = root
        self.last_message_id = last_message_id
        self.references = references
        self.subject = subject
        self.correspondent = correspondent
        self.name = name

    def to_dict(self) -> dict[str, str]:
        return {
            "last_message_id": self.last_message_id,
            "references": self.references,
            "subject": self.subject,
            "correspondent": self.correspondent,
            "name": self.name,
        }

    @classmethod
    def from_dict(cls, root: str, data: dict[str, Any]) -> "ThreadState":
        return cls(
            root,
            last_message_id=str(data.get("last_message_id", "")),
            references=str(data.get("references", "")),
            subject=str(data.get("subject", "")),
            correspondent=str(data.get("correspondent", "")),
            name=str(data.get("name", "")),
        )


class ThreadStore:
    """Per-thread reply state, persisted so a restart keeps threading correctly.

    Without persistence an outbound-initiated delivery after a restart (a cron result
    into an existing thread) would have no parent id and would start a NEW conversation
    in the user's client — the thread state is as load-bearing as the UID cursor."""

    def __init__(self, path_provider: Any = None) -> None:
        self._threads: dict[str, ThreadState] = {}
        self._path_provider = path_provider or (lambda: app_data_dir(_APP) / _THREADS_FILE)
        self._loaded = False

    def _path(self):
        return self._path_provider()

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            data = json.loads(self._path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        for root, entry in data.items():
            if isinstance(entry, dict):
                self._threads[str(root)] = ThreadState.from_dict(str(root), entry)

    def _persist(self) -> None:
        # Trim oldest-first (dict preserves insertion order) before writing.
        if len(self._threads) > MAX_THREADS:
            for root in list(self._threads)[: len(self._threads) - MAX_THREADS]:
                del self._threads[root]
        try:
            payload = {root: st.to_dict() for root, st in self._threads.items()}
            atomic_write(self._path(), json.dumps(payload) + "\n")
        except OSError:
            logger.debug("email: failed to persist thread state", exc_info=True)

    def get(self, root: str) -> ThreadState | None:
        self._ensure_loaded()
        return self._threads.get(root)

    def note_message(
        self, root: str, *, message_id: str, references: str = "", subject: str = "",
        correspondent: str = "", name: str = "",
    ) -> ThreadState:
        """Record a message (inbound or outbound) as the newest link in *root*'s chain."""
        self._ensure_loaded()
        st = self._threads.pop(root, None) or ThreadState(root)
        if message_id:
            st.last_message_id = message_id
            st.references = build_references(references or st.references, message_id)
        elif references:
            st.references = references
        if subject:
            st.subject = subject
        if correspondent:
            st.correspondent = correspondent
        if name:
            st.name = name
        # Re-insert at the end so the trim in _persist drops genuinely-cold threads.
        self._threads[root] = st
        self._persist()
        return st


class _PendingApproval:
    __slots__ = ("future", "token", "request_id", "channel")

    def __init__(self, request_id: str, token: str, channel: str) -> None:
        self.future: asyncio.Future = asyncio.get_event_loop().create_future()
        self.token = token
        self.request_id = request_id
        self.channel = channel


class _EndedApproval(NamedTuple):
    """How an approval asked by mail ended, and the thread it was asked in."""

    outcome: str
    request_id: str
    channel: str
    #: the thread's root id: a reply to the prompt, and the answer to it, belong to it
    thread: str


class EmailDelivery:
    """Renders + delivers gateway results over SMTP. Implements ChannelDelivery."""

    def __init__(
        self, sender: SmtpSender, from_addr: str, owner_id: str = "",
        threads: ThreadStore | None = None,
    ) -> None:
        self._sender = sender
        self._from = from_addr
        self._owner_id = owner_id
        self._threads = threads if threads is not None else ThreadStore()
        # keyed by the uppercase reply token; the transport matches an inbound body
        # against these to resolve an approval.
        self._pending: dict[str, _PendingApproval] = {}
        # How each ended approval ended, by its token, with where it was asked, so a reply to it
        # is answered with that instead of becoming a message to the agent.
        self._ended: OrderedDict[str, _EndedApproval] = OrderedDict()
        # Background sends of those answers, kept until they are sent.
        self._answering: set[asyncio.Task[Any]] = set()
        #: Why the most recent send failed, or "" when it went out (or none was tried). The
        #: channel's health reads it: SMTP holds no connection, so the last send IS its state.
        self.send_failure = ""

    # ── thread bookkeeping (the transport feeds inbound; sends feed themselves) ──

    @property
    def threads(self) -> ThreadStore:
        return self._threads

    def note_inbound(self, mail: Any) -> None:
        """Record an inbound message so the next reply threads onto it."""
        root = getattr(mail, "thread_root", "") or getattr(mail, "message_id", "")
        if not root:
            return
        self._threads.note_message(
            root,
            message_id=getattr(mail, "message_id", ""),
            references=getattr(mail, "references", ""),
            subject=getattr(mail, "subject", ""),
            correspondent=getattr(mail, "from_addr", ""),
            name=getattr(mail, "from_name", ""),
        )

    # ── the one send path ──

    async def _send(self, msg: EmailMessage) -> bool:
        """Hand one built message to SMTP in a thread executor. Never raises."""
        try:
            await asyncio.to_thread(self._sender.send, msg)
        except SmtpError as exc:
            logger.warning("email: send failed: %s", exc)
            self.send_failure = str(exc)
            return False
        except Exception as exc:
            logger.warning("email: unexpected send failure", exc_info=True)
            self.send_failure = f"an unexpected error ({exc.__class__.__name__}); the log has it"
            return False
        self.send_failure = ""
        return True

    async def _deliver(
        self, channel: str, thread_ts: str, subject: str, body: str, *,
        html_body: str = "", attachments: list[tuple[str, str, bytes]] | None = None,
    ) -> str:
        """Build + send one message into *channel*, threaded onto *thread_ts*.

        Returns the sent ``Message-ID`` (the "ts" core threads follow-ups on), or ``""``
        when there was nothing to send to or the send failed."""
        to_addr = self._resolve_recipient(channel, thread_ts)
        if not to_addr:
            logger.debug("email: no recipient for channel=%r thread=%r", channel, thread_ts)
            return ""

        state = self._threads.get(thread_ts) if thread_ts else None
        in_reply_to = state.last_message_id if state else ""
        references = build_references(
            state.references if state else "", in_reply_to
        ) if state else ""
        subj = subject or (reply_subject(state.subject) if state else "PersonalClaw")

        msg = build_outbound(
            from_addr=self._from, to_addr=to_addr, subject=subj, body=body or "",
            html_body=html_body,
            in_reply_to=in_reply_to, references=references, attachments=attachments,
        )
        if not await self._send(msg):
            return ""

        sent_id = str(msg["Message-ID"])
        # Record OUR message as the newest link, so the next reply in this thread
        # references it too — this is what makes three-message continuity hold.
        root = thread_ts or sent_id
        self._threads.note_message(
            root, message_id=sent_id, references=references, subject=subj, correspondent=to_addr
        )
        return sent_id

    def _resolve_recipient(self, channel: str, thread_ts: str = "") -> str:
        """The address to send to: the channel id when it is one, else the thread's peer.

        Core addresses a channel by the id the transport put on ``ChannelMessage`` — for
        email that IS the correspondent's address. A caller that only has a thread id
        (a notification threaded onto an old conversation) falls back to the recorded
        peer, then to the owner."""
        if channel and "@" in channel:
            return channel.strip()
        if thread_ts:
            state = self._threads.get(thread_ts)
            if state is not None and state.correspondent:
                return state.correspondent
        return self._owner_id if "@" in (self._owner_id or "") else ""

    # ── DM resolution ──

    async def open_dm(self, user_id: str) -> str:
        """An email "DM channel" IS the address — there is nothing to open.

        Returns "" for a non-address so a caller degrades instead of handing an opaque
        user id to SMTP as a recipient."""
        addr = str(user_id or "").strip()
        return addr if "@" in addr else ""

    # ── text / rich ──

    async def deliver_text(
        self, channel: str, text: str, thread_ts: str = "", *,
        unfurl_links: bool | None = None, unfurl_media: bool | None = None,
        reply_broadcast: bool | None = None,
    ) -> str:
        """Post plain text. The link-preview/broadcast hints have no email analogue."""
        return await self._deliver(channel, thread_ts, "", text)

    async def deliver_rich(
        self, channel: str, payload: Any, fallback_text: str, *,
        thread_ts: str = "", unfurl_links: bool = True, unfurl_media: bool = True,
        reply_broadcast: bool = False,
    ) -> str:
        """Deliver a rich payload as an HTML alternative (C3: MAY, and we do).

        A caller handing through ``{"html": "<p>…</p>"}`` gets it as the HTML part with
        ``fallback_text`` as the plain part; anything else falls back to plain text
        only, per the contract."""
        html = ""
        if isinstance(payload, dict) and isinstance(payload.get("html"), str):
            html = payload["html"]
        return await self._deliver(channel, thread_ts, "", fallback_text, html_body=html)

    async def deliver_cron_result(
        self, channel: str, job_name: str, job_id: str, text: str, thread_ts: str = ""
    ) -> str:
        subject = f"[PersonalClaw] Cron: {job_name}"
        return await self._deliver(channel, thread_ts, subject, text)

    async def deliver_notification(
        self, channel: str, title: str, text: str, thread_ts: str = ""
    ) -> str:
        """Deliver a titled notification. This is also plan 42's ``channel_dm`` /
        digest delivery target for email — see the README's deferral note."""
        return await self._deliver(channel, thread_ts, f"[PersonalClaw] {title}", text)

    async def deliver_chat_mirror(self, channel: str, text: str, thread_ts: str = "") -> None:
        """Mirror a dashboard reply into the mail thread.

        A trailing ``[OPTIONS: …]`` block becomes a numbered list the recipient answers
        by replying — email's only interactive affordance is a reply, so rendering the
        options as anything else would promise a button that does not exist."""
        from personalclaw.sdk.channel import extract_options

        body, options = extract_options(text)
        if options:
            listed = "\n".join(f"  {i + 1}. {opt}" for i, opt in enumerate(options))
            body = f"{body}\n\nReply with one of:\n{listed}"
        await self._deliver(channel, thread_ts, "", body)

    async def deliver_subagent_reply(
        self, channel: str, text: str, thread_ts: str = "", elapsed_secs: float = 0.0
    ) -> None:
        body = text
        if elapsed_secs:
            body = f"{text}\n\n(took {elapsed_secs:.1f}s)"
        await self._deliver(channel, thread_ts, "", body)

    # ── identity resolution (email gives no directory; be honest) ──

    async def resolve_user_name(self, user_id: str) -> str:
        """A display name if a thread ever carried one, else the address itself."""
        addr = str(user_id or "").strip()
        for root in reversed(list(self._threads._threads)):  # newest thread first
            st = self._threads.get(root)
            if st is not None and st.correspondent == addr.lower() and st.name:
                return st.name
        return addr

    async def resolve_user_profile(self, user_id: str) -> dict:
        name = await self.resolve_user_name(user_id)
        return {"id": str(user_id), "name": name, "email": str(user_id)}

    async def channel_info(self, channel_id: str) -> dict:
        """A mail conversation is 1:1 with the correspondent, so it IS a DM."""
        return {"name": str(channel_id), "is_im": True}

    def list_reply_channels(self) -> list[dict]:
        """The addresses this delivery can post into for the dashboard picker.

        The allowed-sender list lives in the core trust seam (CE-1 owns it) and the SDK
        exposes only a membership check — no enumeration — so the picker offers the
        owner's own address when one is configured. Deliberately minimal, per the
        ChannelDelivery contract ("may be empty")."""
        if self._owner_id and "@" in self._owner_id:
            return [{"id": self._owner_id, "name": self._owner_id}]
        return []

    def is_tracked_channel(self, channel_id: str) -> bool:
        return is_tracked_channel("email", channel_id)

    def build_thread_link(self, channel: str, ts: str) -> str:
        """An RFC 2392 ``mid:`` anchor for a message id (C3: MAY, message-id anchor).

        ``mid:`` is the standard URL form for "this message", and mail clients that
        register the scheme jump straight to it. The angle brackets are stripped (they
        are ``Message-ID`` syntax, not part of the id) and the id is percent-encoded.
        Returns "" without a message id — honest over a URL that resolves to nothing."""
        mid = (ts or "").strip().strip("<>")
        if not mid:
            return ""
        return f"mid:{quote(mid, safe='@.')}"

    # ── attachments (C3: SHOULD — MIME parts) ──

    async def upload_attachment(
        self, channel: str, file_path: str, *, filename: str = "", thread_ts: str = "",
        title: str = "", initial_comment: str = "",
    ) -> str:
        """Attach a file as a MIME part on a message into the thread.

        Reading the file blocks, so it goes through the executor too — the same reason
        the send does."""
        import mimetypes
        import os

        name = filename or os.path.basename(file_path)
        try:
            payload = await asyncio.to_thread(_read_bytes, file_path)
        except OSError:
            logger.warning("email: cannot read attachment %s", file_path, exc_info=True)
            return ""
        mimetype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        body = initial_comment or title or f"Attached: {name}"
        return await self._deliver(
            channel, thread_ts, title or "", body, attachments=[(name, mimetype, payload)]
        )

    # ── streaming: MUST-NOT for email (C3). Explicit no-ops, not accidents. ──

    async def start_stream(self, channel: str, thread_ts: str = "", initial_text: str = "") -> str:
        """No streaming affordance: returns "" so core skips live animation entirely.

        Sending a mail per token would be absurd, and core's mirror path treats "" as
        "this channel cannot stream" (``await start_stream(...) or ""``)."""
        return ""

    async def append_stream_task(
        self, channel: str, stream_ts: str, task_id: str, title: str, status: str,
    ) -> None:
        """No-op: there is no in-flight message to append to (see :meth:`start_stream`)."""
        return None

    async def stop_stream(self, channel: str, stream_ts: str) -> None:
        """No-op: nothing was started (see :meth:`start_stream`)."""
        return None

    # ── approval via reply token (C3: SHOULD) ──

    def _owner_address(self) -> str:
        """The owner's own address, or ``""`` when this channel knows none.

        Core's owner id for this channel, read each time so an owner set after start is the one
        asked, when it is an address. Never the mailbox's own address: mail from it is our own
        coming back and is dropped unread, so a prompt there asks nobody."""
        addr = str(owner_id_for(_PROVIDER) or "").strip().lower()
        if "@" not in addr or addr == (self._from or "").strip().lower():
            return ""
        return addr

    async def request_approval(
        self, event: Any, *, source: str, parent_session_key: str = "",
        sessions: Any = None, on_prompted: Any = None,
    ) -> bool | None:
        """Mail the owner an approve/deny prompt and wait for their reply.

        Only the owner is asked, and only the owner answers (:meth:`resolve_reply_token`): the
        token IS the answer, so the prompt goes to the owner's own address and nowhere else. A
        chat linked to a thread with the owner is asked in that thread. One with anyone else,
        a paired correspondent included, is asked in a new mail to the owner. Returns
        approved/rejected, or ``None`` when there is no owner address to ask (the gateway then
        falls back to the dashboard). ``on_prompted(pending)`` lets core race a dashboard
        prompt against this one: core resolves the same future with how the approval ended
        wherever it ended, so the wait keeps no timer of its own, and the mail says how long
        PersonalClaw waits. A reply after it ended is answered with how it ended."""
        owner = self._owner_address()
        if not owner:
            return None
        channel, thread_ts = owner, ""
        if parent_session_key and sessions is not None:
            try:
                linked_thread, linked = sessions.get_channel_link(parent_session_key)
            except Exception:
                linked_thread, linked = "", ""
            if str(linked or "").strip().lower() == owner:
                channel, thread_ts = str(linked), str(linked_thread or "")

        request_id = str(getattr(event, "request_id", ""))
        brief = approval_brief_for(event) or {}
        title = str(brief.get("tool") or "") or "a tool"
        token = secrets.token_hex(_TOKEN_BYTES).upper()
        pending = _PendingApproval(request_id, token, channel)
        self._pending[token] = pending

        window = _approval_window()
        body = (
            _approval_text(brief, source)
            + "\n\nReply to this message with exactly one of:\n"
            f"    {APPROVE_WORD} {token}\n"
            f"    {DENY_WORD} {token}\n\n"
            + (
                f"PersonalClaw waits up to {window} for your answer. If nobody answers by "
                "then, it does not run."
                if window
                else "If nobody answers before PersonalClaw stops waiting, it does not run."
            )
        )
        sent = await self._deliver(
            channel, thread_ts or "", f"[PersonalClaw] Approval needed: {title}"[:200], body
        )
        if not sent:
            self._pending.pop(token, None)
            return None

        if on_prompted:
            try:
                on_prompted(pending)
            except Exception:
                logger.debug("email: on_prompted hook failed", exc_info=True)

        outcome = "cancelled"
        try:
            outcome = await pending.future
        finally:
            self._pending.pop(token, None)
            self._ended[token] = _EndedApproval(outcome, request_id, channel, thread_ts or sent)
            while len(self._ended) > _ENDED_KEPT:
                self._ended.popitem(last=False)
        return outcome == "approved"

    def _answer_late(self, ended: _EndedApproval) -> None:
        """Mail the owner how the approval they replied to ended, in its thread. Sent on its own,
        so the inbound poll is not held up by the send."""
        text = _LATE_ANSWERS.get(ended.outcome) or (
            "This approval is no longer waiting. Your reply changes nothing."
        )
        try:
            task = asyncio.get_running_loop().create_task(
                self._deliver(ended.channel, ended.thread, "", text)
            )
        except RuntimeError:
            return
        self._answering.add(task)
        task.add_done_callback(self._answering.discard)

    @staticmethod
    def _refuse(who: str, request_id: str) -> None:
        """A reply to the owner's approval from someone else: it decides nothing, and the SEL
        keeps the attempt."""
        logger.warning("email: refused an approval reply from %s, not the owner", who)
        sel().log_api_access(
            caller=f"email:{who or 'unknown'}",
            operation="email.approval_reply",
            outcome="denied",
            source="email",
            resources=request_id,
            error="not the owner",
        )

    def resolve_reply_token(self, text: str, sender: str) -> bool:
        """Answer a pending approval from a reply body. Returns whether the body was an answer.

        A reply to an approval that has already ended is an answer too, one that decides nothing:
        the owner is mailed how it ended, and it does not reach the agent as a new message.

        Only the owner answers. A reply from any other address, an allowed correspondent's
        included, decides nothing: it is logged to the SEL and still returns True, because an
        answer is not a new turn. The token must appear alongside the verb, so an unrelated
        mail that happens to contain the word "approve" cannot decide anything. Called by the
        transport ONLY for a sender the trust seam already allowed — an approval is the
        highest-value thing a channel can carry, so it never rides an unauthenticated
        message."""
        upper = (text or "").upper()
        who = str(sender or "").strip().lower()
        is_owner = bool(who) and who == self._owner_address()
        for token, pending in list(self._pending.items()):
            if token not in upper:
                continue
            approved = f"{APPROVE_WORD} {token}" in upper or f"{APPROVE_WORD}{token}" in upper
            denied = f"{DENY_WORD} {token}" in upper or f"{DENY_WORD}{token}" in upper
            if not approved and not denied:
                continue
            if not is_owner:
                self._refuse(who, pending.request_id)
                return True
            if not pending.future.done():
                # An explicit DENY wins over a body that somehow contains both — a
                # request to stop must never be read as consent.
                pending.future.set_result("rejected" if denied else "approved")
            return True
        for token, ended in list(self._ended.items()):
            if not _answers(upper, token):
                continue
            # A reply to an approval that has ended decides nothing, and it is not a new message
            # to the agent either: the owner is told how it ended.
            if not is_owner:
                self._refuse(who, ended.request_id)
                return True
            self._answer_late(ended)
            return True
        return False


def _answers(upper: str, token: str) -> bool:
    """Whether the uppercased body *upper* answers the approval *token* (either word with it)."""
    return any(
        f"{word} {token}" in upper or f"{word}{token}" in upper
        for word in (APPROVE_WORD, DENY_WORD)
    )


def _approval_text(brief: dict, source: str) -> str:
    """What the approval mail says will run, from core's brief (``approval_brief_for``): the
    tool, its arguments, the purpose the runner gave and the summary line (what the call can
    touch, and its risk), which is what the dashboard's approval card shows. It used to name the
    tool alone. Every string is already masked; the arguments are indented, as a mail client
    shows code."""
    tool = str(brief.get("tool") or "") or "a tool"
    parts = [f"PersonalClaw needs your approval for a {source} action: {tool}"]
    arguments = str(brief.get("input") or "")
    if arguments:
        indented = "\n".join(f"    {line}" for line in arguments.split("\n"))
        parts.append(f"What will run:\n\n{indented}")
    extra = [str(brief[k]) for k in ("purpose", "summary") if brief.get(k)]
    if extra:
        parts.append("\n".join(extra))
    return "\n\n".join(parts)


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()
