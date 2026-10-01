"""EmailTransport — the ChannelTransportProvider that owns the email channel.

Outbound + health/test are configuration-gated and always available. Inbound is an IMAP
poll loop started by :meth:`start_inbound`, which the gateway calls once at boot with a
:class:`GatewayServices` handle. Each cycle:

1. UID-SEARCHes the folder for messages newer than the persisted cursor (every IMAP call
   runs in a thread executor — ``imaplib`` blocks);
2. parses each message to an :class:`~email_runtime.mime.InboundMail` (fail-closed: an
   unparseable message or one with no ``From`` address surfaces nothing);
3. drops our OWN mail (the mailbox receives copies of what we send, and any auto-reply
   from the far side would otherwise loop);
4. hands the message to the platform's guarded door (``services.deliver_channel_inbound``,
   provider ``"email"``) — the address allowlist, the pairing flow, fencing, redaction,
   session linking and the turn itself all live in core, so this transport can't forget
   any of them. A reply containing an active pairing code redeems it BEFORE the door
   (the plan's "pairing code = a reply containing the code" — an in-body search core's
   whole-message check cannot do). A stranger is sent nothing: this mailbox is the
   owner's, so the channel declares ``speaks_as_owner`` and core holds the stranger's
   mail in the Inbox for the owner to answer. Core mirrors agent replies back out
   through the
   :class:`~email_runtime.delivery.EmailDelivery` this transport registers at boot.

**Why not IMAP IDLE.** IDLE would give push-latency instead of the plan's 60s poll, but
``imaplib`` has no IDLE support at all (it would mean hand-rolling the command plus its
29-minute re-issue cycle and the dead-connection detection that comes with it), and a
held-open connection is a second failure mode to supervise. DEFERRED per the plan
("IDLE optional later"); the poll cadence is user-configurable.

Four inbound facts shape this file:

* **A first connection starts after the newest message.** The folder already holds mail
  when the channel is set up — often a whole inbox — and none of it was written to the
  agent. Starting the cursor at 0 answered all of it: the pairing nudge from the owner's
  own address to every sender, no-reply addresses included, and an owner notification for
  each. The first cycle records the newest UID as the cursor and dispatches nothing; mail
  that arrives after it is the channel's.
* **Mail a program sent is never answered** (RFC 3834): auto-replies, bulk and list mail,
  delivery reports and no-reply senders (:func:`~email_runtime.mime.automated_reason`).
  It never reaches the door, which would answer it; an allowed correspondent's still
  reaches this bundle's automations, which answer nobody.

* **The mailbox sees its own mail.** Most providers copy sent mail into the account, and
  an auto-responder on the far side mails straight back. :meth:`_is_self_authored` drops
  anything whose ``From`` is our own mailbox address. Discord had the same trap on
  ``MESSAGE_CREATE``; here it can also loop through a THIRD party's vacation responder,
  which is why the drop is on the address, not on a message id we remember.
* **A ``From`` display name is attacker-controlled.** Trust is keyed on the
  ``parseaddr`` address ONLY (:func:`~email_runtime.mime.sender_address`); the display
  name never reaches a trust check.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
import sys as _sys
from pathlib import Path as _Path
from typing import Any, NamedTuple

# The app loader only keeps this app's dir on sys.path while it execs the entry module.
# This is a multi-module package whose modules import each other for the life of the
# process (the poll loop, delivery and settings all resolve ``email_runtime.*`` long
# after boot). Pin the app dir on sys.path so those imports keep resolving — a real
# installed package would be permanently importable.
_APP_DIR = str(_Path(__file__).resolve().parents[1])
if _APP_DIR not in _sys.path:
    _sys.path.insert(0, _APP_DIR)

from personalclaw.sdk.channel import (
    ChannelCapabilities,
    ChannelMessage,
    ChannelTransportProvider,
    OutboundMessage,
    redeem_owner_pairing_code,
    redeem_pairing_code,
)
from personalclaw.sdk.util import app_data_dir

# Import ALL runtime deps at MODULE level (not lazily inside methods): the loader only
# keeps this app's dir on sys.path while it execs this module, so a
# ``from email_runtime.X import`` inside a method would run LATER, off the path, and
# fail. Binding them here, during exec, captures them for the process life.
from email_runtime.delivery import EmailDelivery, ThreadStore
from email_runtime.imap_client import Imap4Client, ImapClient, ImapError
from email_runtime.imap_client import probe_login as imap_probe
from email_runtime.inbound_tap import publish as publish_inbound
from email_runtime.mime import listing, parse_inbound, strip_quoted_reply
from email_runtime.settings import (
    ACTIVATION_OFF,
    EmailSettings,
    LiveConfig,
    get_settings,
    load_credentials,
    load_raw_settings,
    reload_settings,
)
from email_runtime.smtp_client import SmtplibSender
from email_runtime.smtp_client import probe_login as smtp_probe
from email_runtime.writes import SendRefused, live_writes_disabled

logger = logging.getLogger(__name__)

PROVIDER = "email"
_APP = "email-channel"
_CURSOR_FILE = "imap_cursor.json"
#: Longest backoff between failed poll cycles. A mail server that is down for an hour
#: must not be retried every 60s, but the channel must recover without a restart.
_MAX_BACKOFF = 900.0


class _Batch(NamedTuple):
    """What one blocking IMAP cycle brought back (:meth:`EmailTransport._fetch_batch`)."""

    messages: list[tuple[int, bytes]]
    uidvalidity: int
    #: The newest UID, to start after without reading anything: on a first connection, and
    #: when UIDVALIDITY changed. ``None`` in an ordinary cycle.
    restart_at: int | None
    #: Why the cycle could not read the folder (the sentence health shows); ``""`` when it could.
    failure: str


class EmailTransport(ChannelTransportProvider):
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        # Per-instance config wins for the non-secret connection fields until the app store is
        # written after this transport was built — then the store does (see LiveConfig), so a
        # Configure → Save reaches the next Test/Connect/health/send.
        self._config = LiveConfig(config or {})
        self._services: Any = None
        self._delivery: EmailDelivery | None = None
        self._poll_task: asyncio.Task | None = None
        self._stopping = False
        #: The last UID dispatched, or ``None`` before the first connection has set one.
        self._cursor: int | None = None
        self._uidvalidity = 0
        #: Why the last IMAP cycle failed, ``""`` when it read the folder. Health reports it.
        self._poll_failure = ""
        #: Whether this instance's IMAP poll loop is running (``start_inbound`` → ``stop_inbound``).
        self._receiving = False
        # Test seams: inject a fake IMAP client factory / a fake SMTP sender, so no test
        # touches a socket. Dependency injection rather than monkeypatching the stdlib.
        self._client_factory: Any = None
        self._sender_factory: Any = None

    # ── identity ──

    @property
    def name(self) -> str:
        return PROVIDER

    @property
    def display_name(self) -> str:
        return "Email"

    def capabilities(self) -> ChannelCapabilities:
        """Honest capabilities — every ``True`` has an implementation behind it.

        * ``inbound`` → the IMAP poll loop in :meth:`start_inbound`.
        * ``threads`` → ``Message-ID``/``In-Reply-To``/``References`` chains, kept by
          :class:`~email_runtime.delivery.ThreadStore`.
        * ``attachments`` → ``EmailDelivery.upload_attachment`` adds a MIME part.
        * ``rich_text`` → ``EmailDelivery.deliver_rich`` sends an HTML alternative.
        * ``reactions`` → email has no reaction concept.
        * ``typing_indicator`` → nothing to show between messages.
        * ``edits`` → **False, and this is how "streaming=false" is declared.** The
          shipped ``ChannelCapabilities`` dataclass has no ``streaming`` field; in every
          other channel a stream IS a repeatedly-edited message, so no-edits means
          no-streaming. The plan's C3 row (streaming trio MUST-NOT for email) is
          implemented as ``start_stream`` returning ``""`` with no-op append/stop, and a
          test pins both halves of that mapping together.
        * ``max_text_len`` → 0 (unbounded): SMTP imposes no practical body limit that a
          chat reply would hit, so claiming a number would be a lie in the other
          direction.
        * ``owner_pairing`` → the code Configure → Pair as owner shows is mailed to the
          mailbox from the owner's address, anywhere in the message, and redeemed by
          :meth:`_try_pairing` (``redeem_owner_pairing_code``); core reads the owner with
          ``owner_id_for`` each time it asks (:meth:`owner_pairing_hint` says how, over the
          code). The owner id was otherwise a variable set by hand.
        * ``speaks_as_owner`` → a mail from this app goes out from the owner's own mailbox, so
          core hands it no pairing note for a stranger and holds the stranger's mail in the
          Inbox as someone new, for the owner to reply to, pair or ignore.
        * ``groups`` → **False**: a mail to the mailbox is one person writing to the owner, so
          every message crosses the door with ``is_dm=True`` and Sender trust shows no group rule.
        """
        return ChannelCapabilities(
            inbound=True, threads=True, attachments=True, reactions=False,
            edits=False, rich_text=True, typing_indicator=False, max_text_len=0,
            owner_pairing=True, speaks_as_owner=True,
        )

    def validate_target(self, target: str) -> str:
        """Whether email can send to ``target``: one address, ``someone@example.com``.

        An address is the only thing this channel sends to (``EmailDelivery.open_dm`` answers
        ``""`` for anything else), so it claims nothing else. Core asks every chat channel set up
        whether an id sent without naming its channel is one of its own, and the default check
        takes any id at all, which made every chat or channel id look like it might be email's.
        """
        value = str(target or "").strip()
        local, at, domain = value.partition("@")
        if (
            at
            and local
            and domain
            and "@" not in domain
            and len(value) <= 254
            and not any(ch.isspace() or ch in ',;<>"' or ord(ch) < 32 for ch in value)
        ):
            return ""
        return "An email address is what email sends to, like someone@example.com."

    def owner_pairing_hint(self) -> str:
        """How the owner sends the pairing code here: a mail to the mailbox, not a DM to a bot."""
        address = self._settings().mailbox_address or "the mailbox this channel reads"
        return (
            f"Mail this code to {address} from the address that should get your approvals. "
            "It can be anywhere in the message"
        )

    def sender_pairing_hint(self) -> str:
        """How someone the owner pairs sends their code here: a mail to the mailbox from the
        address being let in (:meth:`_try_pairing` finds it anywhere in the message)."""
        address = self._settings().mailbox_address or "the mailbox this channel reads"
        return (
            f"Have them mail this code from the address you want to let in, to {address}. "
            "It can be anywhere in the message"
        )

    # ── settings + credentials ──

    def _settings(self) -> EmailSettings:
        """Live settings with any per-instance config overlaid.

        The registry builds this provider with the app store's dict; the overlay is that dict
        only until the store is written again (a Configure save), after which it IS the store,
        so a saved host or port reaches the next probe. It otherwise matters only for a test or
        a second instance handed an explicit dict. Everything goes through
        :meth:`EmailSettings.from_dict`, so instance config gets the SAME coercion the stored
        config does — an overlaid port or cadence can't skip validation."""
        return EmailSettings.from_dict(self._raw_settings())

    def _raw_settings(self) -> dict:
        return {**load_raw_settings(), **self._config.current()}

    def _passwords(self) -> tuple[str, str]:
        """``(imap, smtp)`` from the same overlay :meth:`_settings` reads, so a password saved
        on the Configure form reaches the next probe or send together with the host it is for."""
        return load_credentials(self._raw_settings())

    async def connect(self) -> bool:
        settings = self._settings()
        return settings.inbound_configured or settings.outbound_configured

    async def disconnect(self) -> None:
        return None

    @property
    def connected(self) -> bool:
        settings = self._settings()
        return settings.inbound_configured or settings.outbound_configured

    # ── UID cursor persistence (resume the poll across restarts) ──

    def _cursor_path(self) -> _Path:
        return app_data_dir(_APP) / _CURSOR_FILE

    def _load_cursor(self) -> tuple[int | None, int]:
        """``(last_uid, uidvalidity)`` from the app's data dir.

        ``(None, 0)`` when there is none — never connected — and when it cannot be read:
        ``0`` would mean "answer everything in the folder", and a lost cursor must not turn
        into a reply to every mail the mailbox holds. The next cycle starts after the newest
        message instead."""
        try:
            data = json.loads(self._cursor_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None, 0
        if not isinstance(data, dict):
            return None, 0
        try:
            return int(data["last_uid"]), int(data.get("uidvalidity", 0))
        except (KeyError, TypeError, ValueError):
            return None, 0

    def _save_cursor(self, last_uid: int, uidvalidity: int) -> None:
        from personalclaw.sdk.channel import atomic_write

        try:
            atomic_write(
                self._cursor_path(),
                json.dumps({"last_uid": int(last_uid), "uidvalidity": int(uidvalidity)}) + "\n",
            )
        except OSError:
            logger.debug("email: failed to persist IMAP cursor", exc_info=True)

    # ── factories (real, or the injected test fakes) ──

    def _make_client(self, settings: EmailSettings, password: str) -> ImapClient:
        if self._client_factory is not None:
            return self._client_factory(settings, password)
        return Imap4Client(
            settings.imap_host, settings.imap_port, settings.imap_user, password,
            use_ssl=settings.imap_use_ssl, ca_file=settings.tls_ca_file,
        )

    def _make_sender(self, settings: EmailSettings, password: str) -> Any:
        if self._sender_factory is not None:
            return self._sender_factory(settings, password)
        return SmtplibSender(
            settings.smtp_host, settings.smtp_port, settings.smtp_user, password,
            security=settings.smtp_security, ca_file=settings.tls_ca_file,
        )

    # ── Inbound: the gateway drives this once at boot ──

    async def start_inbound(self, services: Any) -> None:
        self._services = services
        settings = reload_settings()
        imap_pass, smtp_pass = load_credentials()

        # Register outbound delivery on the gateway + dashboard. Core delivers every
        # channel result through this ONE provider-agnostic ChannelDelivery handle — it
        # never sees an SMTP client. Registered even when inbound can't start, so a
        # send-only configuration still receives cron/heartbeat results.
        if settings.outbound_configured and smtp_pass:
            self._delivery = EmailDelivery(
                self._make_sender(settings, smtp_pass),
                settings.mailbox_address,
                owner_id=settings.mailbox_address,
                threads=ThreadStore(),
            )
            if hasattr(services, "register_channel_delivery"):
                services.register_channel_delivery(self._delivery)
            if getattr(services, "dashboard_state", None) is not None:
                services.dashboard_state.channel_delivery = self._delivery
        else:
            logger.info("EmailTransport: SMTP not configured — outbound delivery unavailable")

        if not (settings.inbound_configured and imap_pass):
            logger.info("EmailTransport: IMAP not configured — inbound stays offline")
            return
        if settings.dm_activation == ACTIVATION_OFF:
            logger.info("EmailTransport: dm_activation=off — inbound disabled by settings")
            return

        self._cursor, self._uidvalidity = self._load_cursor()
        self._stopping = False
        self._poll_failure = ""
        self._receiving = True
        self._poll_task = asyncio.ensure_future(self._poll_loop())
        logger.info(
            "EmailTransport: IMAP poll inbound started (folder=%s cursor=%s every %ds)",
            settings.folder,
            "none yet: starts after the newest message" if self._cursor is None else self._cursor,
            settings.poll_secs,
        )

    async def stop_inbound(self) -> None:
        self._stopping = True
        self._receiving = False
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(self._poll_task), timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception:
                logger.debug("email: poll task stop error", exc_info=True)

    async def _poll_loop(self) -> None:
        """Poll IMAP on the configured cadence. Degrades on error, never crashes.

        A cycle that could not read the folder — the server unreachable, its certificate
        refused, the login refused — backs off like one that raised: a mail server down
        for an hour is not retried every minute, and a wrong password is not tried against
        the account sixty times an hour. It used to be logged and retried at the plain
        cadence, and health went on reading ready."""
        backoff = 0.0
        while not self._stopping:
            settings = get_settings()
            raised: BaseException | None = None
            try:
                await self._poll_once(settings)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raised = exc
                self._poll_failure = (
                    f"an unexpected error ({exc.__class__.__name__}); the gateway log has it"
                )
            if self._poll_failure:
                backoff = min(backoff * 2, _MAX_BACKOFF) if backoff else float(settings.poll_secs)
                logger.warning(
                    "email: IMAP poll failed: %s — trying again in %gs",
                    self._poll_failure, backoff, exc_info=raised,
                )
                delay = backoff
            else:
                backoff = 0.0
                delay = float(settings.poll_secs)
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise

    async def _poll_once(self, settings: EmailSettings) -> None:
        """One IMAP cycle: fetch new UIDs, dispatch each, advance the cursor.

        The blocking IMAP work happens in a thread executor and returns plain data; the
        dispatch (which touches the trust seam, sessions and the event loop) happens back
        on the loop. That split is the reason no ``imaplib`` call can stall the gateway."""
        imap_pass, _ = load_credentials()
        if not imap_pass:
            logger.warning("email: no IMAP password in the credential store — cannot poll")
            return

        batch = await asyncio.to_thread(
            self._fetch_batch, settings, imap_pass, self._cursor, self._uidvalidity
        )
        self._poll_failure = batch.failure

        # Start after the newest message without reading anything. On a first connection,
        # so no mail that was there before the channel is answered. On a UIDVALIDITY
        # change, because every stored UID is then meaningless: the worker re-derived the
        # newest UID under the NEW numbering (a search from the stale cursor would return
        # nothing and leave the mailbox skipped forever). 0 means "server didn't report
        # it" — not a change.
        if batch.restart_at is not None:
            if self._cursor is None:
                logger.info(
                    "email: first connection to %s — starting after uid %d, so the mail "
                    "already there is not answered",
                    settings.folder, batch.restart_at,
                )
            else:
                logger.warning(
                    "email: UIDVALIDITY changed (%d → %d) — cursor reset to %d",
                    self._uidvalidity, batch.uidvalidity, batch.restart_at,
                )
            self._cursor = batch.restart_at
            self._uidvalidity = batch.uidvalidity
            self._save_cursor(self._cursor, self._uidvalidity)
            return
        if self._cursor is None:
            return  # the first connection failed: nothing was read, no start was set
        if batch.uidvalidity and not self._uidvalidity:
            self._uidvalidity = batch.uidvalidity

        advanced = False
        try:
            for uid, raw in batch.messages:
                # Advance PAST this uid BEFORE dispatch so a handler that dies can't wedge
                # the loop on the same message forever (the offset-before-dispatch rule the
                # Telegram/Discord transports follow). ``except Exception`` covers an
                # ordinary handler error; the ``finally`` below covers the rest, because
                # ``asyncio.CancelledError`` is a BaseException and would otherwise carry
                # the whole batch's advance away unsaved — and every message in it would be
                # replayed on the next boot.
                if uid > self._cursor:
                    self._cursor = uid
                    advanced = True
                try:
                    await self._dispatch(raw, uid, settings)
                except Exception:
                    logger.warning(
                        "email: message dispatch failed for uid %s", uid, exc_info=True
                    )
        finally:
            if advanced:
                self._save_cursor(self._cursor, self._uidvalidity)

    def _fetch_batch(
        self, settings: EmailSettings, password: str, last_uid: int | None,
        known_uidvalidity: int,
    ) -> _Batch:
        """BLOCKING: connect, select, search, fetch.

        ``restart_at`` is the folder's newest UID, and nothing is fetched, when there is no
        cursor yet (``last_uid`` is ``None``: the first connection) or UIDVALIDITY changed —
        the check happens HERE, before the search, because a search from a stale cursor
        under new numbering returns nothing and would leave the mailbox stuck forever.
        ``failure`` is the sentence for a cycle that could not read the folder.

        Runs on a worker thread. A per-UID fetch that comes back empty STOPS the batch so
        the cursor never advances past a message we never read — the next cycle resumes
        there."""
        out: list[tuple[int, bytes]] = []
        uidvalidity = 0
        client = self._make_client(settings, password)
        try:
            client.connect()
            uidvalidity = client.select_folder(settings.folder)
            renumbered = bool(
                uidvalidity and known_uidvalidity and uidvalidity != known_uidvalidity
            )
            if last_uid is None or renumbered:
                return _Batch(out, uidvalidity, client.newest_uid(settings.folder), "")
            for uid in client.fetch_uids_since(settings.folder, last_uid):
                raw = client.fetch_message(settings.folder, uid)
                if not raw:
                    logger.debug("email: empty fetch for uid %s — pausing at cursor", uid)
                    break
                out.append((uid, raw))
        except ImapError as exc:
            return _Batch(out, uidvalidity, None, str(exc))
        finally:
            try:
                client.close()
            except Exception:
                logger.debug("email: IMAP client close error", exc_info=True)
        return _Batch(out, uidvalidity, None, "")

    # ── per-message handling ──

    def _is_self_authored(self, from_addr: str, settings: EmailSettings) -> bool:
        """Whether this message is our own outbound mail coming back.

        Most providers file a copy of sent mail into the account, and an auto-responder
        anywhere in the chain mails straight back at us. Without this the agent answers
        its own reply, forever. Matched on the ADDRESS (not a remembered message id) so
        a third party's vacation responder quoting our address is caught too."""
        mailbox = (settings.mailbox_address or "").strip().lower()
        return bool(mailbox) and from_addr.strip().lower() == mailbox

    def _to_channel_message(self, mail: Any) -> ChannelMessage:
        """Normalize an :class:`InboundMail` to the canonical inbound shape.

        ``channel_id`` is the correspondent's address — that IS how core addresses a
        reply back — and ``thread_id`` is the chain root, which is the session key. The
        mail's attachments are its ``files``, which core keeps with what the mail becomes."""
        return ChannelMessage(
            channel_id=mail.from_addr,
            text=mail.body,
            sender=mail.from_addr,
            thread_id=mail.thread_root,
            message_id=mail.message_id,
            ts=mail.ts,
            metadata={
                "sender_name": mail.from_name,
                "subject": mail.subject,
                "uid": str(mail.uid),
                "references": mail.references,
                "in_reply_to": mail.in_reply_to,
            },
            files=list(mail.attachments),
        )

    async def _dispatch(self, raw: bytes, uid: int, settings: EmailSettings) -> None:
        """Run one raw message through parse → self-filter → trust → session."""
        mail = parse_inbound(raw, uid)
        if mail is None:
            return  # fail-closed: unparseable / no From ⇒ nothing surfaces
        if self._is_self_authored(mail.from_addr, settings):
            logger.debug("email: dropped our own message uid %s", uid)
            return

        # Only the new text of a reply becomes the turn — the quoted history below it is
        # the previous conversation (often including our own words).
        text = strip_quoted_reply(mail.body).strip()
        if not text and mail.attachments:
            # A mail that is only its attachments still says something: that these came. Its
            # files ride with it (`_to_channel_message`), and this line names them.
            text = f"(No message text. It came with:)\n{listing(mail.attachments)}"
        if not text:
            return  # nothing to act on: no text and nothing attached

        cm = self._to_channel_message(mail)

        from personalclaw.sdk.channel import is_allowed_sender

        if mail.automated:
            # RFC 3834: nothing may answer mail a program sent. The door would — the agent's
            # answer to a correspondent — and would hold a stranger's in the Inbox and tell
            # the owner someone is writing, so the mail never reaches it: an answer to an
            # auto-reply loops, and one to a list or a no-reply address is backscatter. An
            # allowed correspondent's mail still reaches this bundle's automations (the same
            # allowlist the door reads), which answer nobody.
            if is_allowed_sender(PROVIDER, cm.sender):
                publish_inbound(cm, text=text)
            logger.info(
                "email: not answering uid %s from %s: %s", uid, mail.from_addr, mail.automated
            )
            return

        # A reply from a not-yet-allowed sender that CONTAINS an active pairing code
        # redeems it (the plan's "pairing code = a reply containing the code"). Checked
        # before the door so the pairing reply doesn't get the canned nudge again —
        # the code is searched INSIDE the body, which core's whole-message digit
        # check cannot do through a mail client's quoting and signature.
        if await self._try_pairing(cm, text, settings):
            return

        # Remember the inbound message FIRST so any reply — the agent's, a pairing
        # confirmation, or the owner's own from the Inbox — threads under the sender's
        # own message.
        if self._delivery is not None:
            self._delivery.note_inbound(mail)
            # An ALLOWED sender's body may carry an approval reply-token; an answer is
            # consumed (it is not a new turn), and only the owner's decides anything. Gated
            # on the allowlist read, never on the raw body: an unknown sender must not be
            # able to resolve an approval by mailing a token.
            if is_allowed_sender(PROVIDER, cm.sender) and self._delivery.resolve_reply_token(
                text, cm.sender
            ):
                return

        # The guarded door. Core applies the trust gate, the non-owner-content
        # fence, redaction, session linking and the turn itself — the routing this
        # transport used to carry a copy of. An email to our mailbox is a direct
        # message by construction (no "room" concept), so is_dm is always True. A
        # stranger's mail is held in the Inbox for the owner (``speaks_as_owner``).
        cm_for_door = cm if text == cm.text else replace(cm, text=text)
        verdict = await self._services.deliver_channel_inbound(
            PROVIDER, cm_for_door, is_dm=True
        )
        # The one reply this app sends on its own to someone PersonalClaw did not know: that
        # they just paired, with a code the owner gave them. It goes in-thread. Any other
        # reply the door hands back stays unsent: a mail from here goes out as the owner,
        # and a stranger is answered by nobody but the owner.
        if verdict.canned_reply and verdict.meta.get("paired") and self._delivery is not None:
            try:
                await self._delivery.deliver_text(
                    cm.channel_id, verdict.canned_reply, cm.thread_id
                )
            except Exception:
                logger.debug("email: canned reply send failed", exc_info=True)

        # Hand the mail to this bundle's own trigger source, if one is attached.
        # AFTER the door and gated on `verdict.allowed`, which is the entire security
        # property: a `From` address is trivially forged, so only core's allowlist decision
        # may admit a sender — and a denied sender must arm no automation either. The
        # quote-STRIPPED `text` is published rather than `cm.text`, so a reply's event
        # carries its new prose and not the history of our own previous words. The tap never
        # raises and knows nothing about events; see inbound_tap's docstring.
        if verdict.allowed:
            publish_inbound(cm, text=text)

    async def _try_pairing(self, cm: ChannelMessage, text: str, settings: EmailSettings) -> bool:
        """Redeem a pairing code found in *text*. Returns whether pairing happened.

        The plan's pairing UX for email is "a reply containing the code", so the code is
        searched for inside the body rather than required to be the whole message — a
        mail client's quoting and signature make an exact-match rule unusable.

        Two codes, in the gate's order for each code-shaped word: a sender's code
        (``personalclaw pair email``), for a sender not yet allowed, which core redeems only while
        Email's rule for strangers asks for a code (``redeem_pairing_code``); then the OWNER's code
        (Configure → Pair as owner), for anyone, so a correspondent already allowed can
        become the owner. The owner's code makes the sender the channel's owner, the one
        address approvals go to, and a word that matches neither counts against its five
        wrong guesses (``redeem_owner_pairing_code``)."""
        from personalclaw.sdk.channel import is_allowed_sender

        allowed = is_allowed_sender(PROVIDER, cm.sender)
        import re

        for candidate in re.findall(r"\b\d{8}\b", text):
            if not allowed and redeem_pairing_code(PROVIDER, cm.sender, candidate):
                await self._confirm_pairing(
                    cm, "Paired — you can talk to me by replying to this thread."
                )
                logger.info("email: sender %s paired via code", cm.sender)
                return True
            name = str((cm.metadata or {}).get("sender_name") or "")
            if redeem_owner_pairing_code(PROVIDER, cm.sender, candidate, name):
                await self._confirm_pairing(
                    cm,
                    "Paired — you're my owner here now. Approvals and anything else for you "
                    "come to this address.",
                )
                logger.info("email: %s paired as the owner", cm.sender)
                return True
        return False

    async def _confirm_pairing(self, cm: ChannelMessage, sentence: str) -> None:
        """Say in the sender's thread that the pairing happened."""
        if self._delivery is None:
            return
        try:
            await self._delivery.deliver_text(cm.channel_id, sentence, cm.thread_id)
        except Exception:
            logger.debug("email: pairing confirmation send failed", exc_info=True)

    # ── outbound / health ──

    async def send(self, message: OutboundMessage) -> bool | SendRefused:
        """Send one message. Used by the generic Channels surface, not the reply path.

        ``True`` delivered, ``False`` failed/unconfigured, or a :class:`SendRefused`
        when the platform's live-writes kill switch is on. The refusal is checked AFTER
        the configuration gate on purpose: an unconfigured transport could not have
        written anything, so reporting "refused" there would claim the guard suppressed
        a write that was never possible. Only a transport that WOULD have transmitted
        reports a refusal.
        """
        settings = self._settings()
        _, smtp_pass = self._passwords()
        if not (settings.outbound_configured and smtp_pass):
            return False
        # DISABLE_LIVE_WRITES. An SMTP hand-off is the least reversible write
        # this repo makes — once the server accepts the message there is no recall — so
        # it is squarely the class core refuses for non-GET egress and model deletion.
        # Typed refusal, never a silent no-op: a test (or an operator) asserting a send
        # must be able to see that the guard, not the network, stopped it. Checked
        # BEFORE the sender is constructed so a suppressed send opens no SMTP session.
        if live_writes_disabled():
            refusal = SendRefused(channel=PROVIDER, target=message.channel_id)
            logger.warning("EmailTransport.send refused: %s", refusal)
            return refusal
        delivery = self._delivery or EmailDelivery(
            self._make_sender(settings, smtp_pass),
            settings.mailbox_address,
            owner_id=settings.mailbox_address,
        )
        sent = await delivery.deliver_text(message.channel_id, message.text, message.thread_id)
        return bool(sent)

    async def health(self) -> dict[str, Any]:
        """What the channel is doing, from what its connections last did (the F-24 contract).

        It read "ready" whenever the settings and passwords were there — so it said ready
        while every IMAP login was refused. Now it is ready only while the poll loop runs
        and its last cycle read the folder, and the last send (SMTP holds no connection, so
        the last send is its state) went out. Anything else is ``error`` with the sentence
        saying what failed: the certificate refused, the server unreachable, the login
        refused. Reads state only — core asks it with a five-second budget."""
        settings = self._settings()
        if not settings.inbound_configured and not settings.outbound_configured:
            return {"state": "offline", "detail": "No IMAP/SMTP configuration"}
        imap_pass, smtp_pass = self._passwords()
        missing = []
        if settings.inbound_configured and not imap_pass:
            missing.append("IMAP password")
        if settings.outbound_configured and not smtp_pass:
            missing.append("SMTP password")
        if missing:
            return {"state": "error", "detail": f"Missing: {', '.join(missing)}"}
        inbound = settings.inbound_configured and settings.dm_activation != ACTIVATION_OFF
        if not inbound and not settings.outbound_configured:
            return {
                "state": "offline",
                "detail": "Inbound Activation is off and no SMTP server is configured",
            }
        problems = []
        if inbound and not self._receiving:
            problems.append(
                "Inbound NOT STARTED — PersonalClaw starts the IMAP poll when it turns the "
                "channel on, and Configure → Save starts it now"
            )
        elif inbound and self._poll_failure:
            problems.append(
                f"Inbound NOT RECEIVING — the last IMAP poll failed: {self._poll_failure}. "
                "It tries again, waiting longer after each failure, up to "
                f"{_MAX_BACKOFF / 60:g} minutes"
            )
        send_failure = self._delivery.send_failure if self._delivery is not None else ""
        if settings.outbound_configured and send_failure:
            problems.append(f"Outbound NOT SENDING — the last send failed: {send_failure}")
        if problems:
            return {"state": "error", "detail": " · ".join(problems) + "."}
        halves = []
        if inbound:
            halves.append(f"IMAP {settings.imap_host}")
        if settings.outbound_configured:
            halves.append(f"SMTP {settings.smtp_host}")
        return {"state": "ready", "detail": " · ".join(halves)}

    async def test(self) -> dict[str, Any]:
        """The Channels-page Test action: the plan's ``probe = login+select``.

        Both halves are probed — IMAP login plus a SELECT of the polled folder, and an
        SMTP login — because a channel with one working half is still broken, and the
        two most common misconfigurations (wrong folder, SMTP port/security mismatch)
        each hide behind a green login on the other protocol. Both probes block, so both
        run in a thread executor."""
        settings = self._settings()
        imap_pass, smtp_pass = self._passwords()
        results: list[str] = []
        ok = True

        if settings.inbound_configured:
            if not imap_pass:
                ok = False
                results.append("IMAP: no password configured")
            else:
                good, detail = await asyncio.to_thread(
                    imap_probe, settings.imap_host, settings.imap_port, settings.imap_user,
                    imap_pass, settings.folder, use_ssl=settings.imap_use_ssl,
                    ca_file=settings.tls_ca_file,
                )
                ok = ok and good
                results.append(detail)
        if settings.outbound_configured:
            if not smtp_pass:
                ok = False
                results.append("SMTP: no password configured")
            else:
                good, detail = await asyncio.to_thread(
                    smtp_probe, settings.smtp_host, settings.smtp_port, settings.smtp_user,
                    smtp_pass, security=settings.smtp_security, ca_file=settings.tls_ca_file,
                )
                ok = ok and good
                results.append(detail)

        if not results:
            return {"ok": False, "detail": "No IMAP/SMTP configuration"}
        # The channel contract: Test is not ok while health is not ready. Two logins that
        # work now say nothing about a poll loop that is not running, or that the next
        # cycle has not caught up with — so a green Test never sits beside a red status.
        health = await self.health()
        if ok and health["state"] != "ready":
            ok = False
            results.append(f"but {health['detail']}")
        return {"ok": ok, "detail": " · ".join(results)}


def create_provider(config: dict[str, Any] | None = None) -> "EmailTransport":
    return EmailTransport(config)
