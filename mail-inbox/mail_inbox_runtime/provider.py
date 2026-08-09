"""MailInboxProvider — a MessageSourceProvider that polls an IMAP mailbox.

This is the whole inbound story for EIAT-2 (send_reply/SMTP is EIAT-3, prompt-bound
addresses are EIAT-4). On each ``poll`` the provider:

1. reads the latest app settings + the IMAP password from the SDK credential store
   (NEVER from app.json/ProviderSettings — EIAT guardrail);
2. **fails closed on the allowlist** — an empty/absent ``allow_senders`` surfaces ZERO
   messages and never even connects; the posture is logged once so a silent empty inbox
   is diagnosable (§2.7, guardrail 1);
3. UID-SEARCHes the folder for messages newer than the checkpoint cursor (the dict
   ``poll`` returns is the resume mechanism — the highest processed UID per folder), so a
   restart neither reprocesses nor skips;
4. drops a message whose ``From`` is not allowlisted (a per-rejection SEL
   ``mail_sender_rejected`` event fires) and a duplicate ``Message-ID`` (a second belt
   over the UID cursor);
5. extracts the body per T2.3 (prefer text/plain, sanitize HTML, pull attachment text
   through core's document readers) and maps the mail onto ``IncomingMessage`` — RAW;
   fencing happens downstream at prompt time, never here.

The gateway's app loader keeps this app's dir on sys.path only while it execs THIS
module, so pin it back (mirrors telegram-channel/transport.py) to keep the sibling
``mail_inbox_runtime.*`` imports resolving for the process life.
"""

from __future__ import annotations

import asyncio
import email
import email.policy
import email.utils
import fnmatch
import json
import logging
import sys as _sys
import time
from pathlib import Path as _Path
from typing import Any

_APP_DIR = str(_Path(__file__).resolve().parents[1])
if _APP_DIR not in _sys.path:
    _sys.path.insert(0, _APP_DIR)

from personalclaw.sdk.channel import AppConfig, atomic_write, sel
from personalclaw.sdk.inbox import IncomingMessage, MessageSourceProvider
from personalclaw.sdk.util import app_data_dir

from mail_inbox_runtime.imap_client import ImapClient, ImapError, Imap4Client
from mail_inbox_runtime.mime import extract_body
from mail_inbox_runtime.settings import CRED_MAIL_PASSWORD, MailInboxSettings, reload_settings

logger = logging.getLogger(__name__)

_APP = "mail-inbox"
SOURCE_NAME = "mail"
_SEEN_IDS_FILE = "seen_message_ids.json"
# Bound the persisted dedup set so a long-lived mailbox can't grow it without limit.
_MAX_SEEN_IDS = 5000


def _parse_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _sender_allowed(from_addr: str, allow_senders: list[str]) -> bool:
    """Fail-closed glob match. An empty allowlist allows NOTHING (never reached — the
    provider short-circuits before fetching — but defensive here too)."""
    if not allow_senders:
        return False
    addr = from_addr.strip().lower()
    if not addr:
        return False
    return any(fnmatch.fnmatch(addr, pattern) for pattern in allow_senders)


class MailInboxProvider(MessageSourceProvider):
    """Polls an IMAP mailbox and surfaces allowlisted mail as inbox items."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        # config is the per-instance override the loader may pass; mail-inbox keeps ALL
        # non-secret config in its own ProviderSettings store (read fresh each poll) and
        # the password in the credential store — so nothing is taken from here.
        self._posture_logged = False
        self._client_factory = None  # test seam: inject a fake ImapClient factory

    @property
    def source_name(self) -> str:
        return SOURCE_NAME

    # ── credentials (SDK credential store ONLY) ──
    @staticmethod
    def _resolve_password() -> str:
        """The IMAP password from the shared credential store, by this app's own key.
        Never read from app.json/ProviderSettings."""
        try:
            return AppConfig.load().load_credentials().get(CRED_MAIL_PASSWORD, "")
        except Exception:
            logger.debug("mail-inbox: credential load failed", exc_info=True)
            return ""

    # ── Message-ID dedup belt (persisted, bounded) ──
    def _seen_ids_path(self) -> _Path:
        return app_data_dir(_APP) / _SEEN_IDS_FILE

    def _load_seen_ids(self) -> list[str]:
        try:
            data = json.loads(self._seen_ids_path().read_text(encoding="utf-8"))
            ids = data.get("ids", []) if isinstance(data, dict) else []
            return [str(i) for i in ids]
        except (OSError, ValueError, json.JSONDecodeError):
            return []

    def _save_seen_ids(self, existing: list[str], new_ids: list[str]) -> None:
        # Newest last; keep the tail so recent messages stay deduped, older ones age out.
        merged = existing + [i for i in new_ids if i not in existing]
        if len(merged) > _MAX_SEEN_IDS:
            merged = merged[-_MAX_SEEN_IDS:]
        try:
            atomic_write(self._seen_ids_path(), json.dumps({"ids": merged}) + "\n")
        except OSError:
            logger.debug("mail-inbox: failed to persist seen message-ids", exc_info=True)

    # ── SEL ──
    @staticmethod
    def _log_rejection(from_addr: str, uid: int) -> None:
        """Emit the mail_sender_rejected security event (per-rejection audit trail)."""
        try:
            sel().log_api_access(
                caller=_APP,
                operation="mail_sender_rejected",
                outcome="rejected",
                source="channel",
                resources=f"from={from_addr or '?'} uid={uid}",
                error="sender not in allowlist",
            )
        except Exception:
            logger.debug("mail-inbox: SEL log failed", exc_info=True)

    def _log_posture_once(self, settings: MailInboxSettings) -> None:
        if self._posture_logged:
            return
        self._posture_logged = True
        if settings.allow_senders:
            logger.info(
                "mail-inbox: allowlist active with %d pattern(s) — only matching senders "
                "are surfaced (fail-closed)",
                len(settings.allow_senders),
            )
        else:
            logger.warning(
                "mail-inbox: NO allowlist configured — surfacing ZERO messages (fail-closed). "
                "Add allow_senders in the app settings to enable triggering."
            )

    # ── client factory (real, or the injected test fake) ──
    def _make_client(self, settings: MailInboxSettings, password: str) -> ImapClient:
        if self._client_factory is not None:
            return self._client_factory(settings, password)
        return Imap4Client(
            settings.host, settings.port, settings.username, password, use_ssl=settings.use_ssl
        )

    @staticmethod
    def _checkpoint_key(settings: MailInboxSettings) -> str:
        # Per (account, folder): monotonic UID cursor. Namespaced so it never collides
        # with another source's key in core's shared last_read_ts dict.
        return f"mailuid:{settings.username}:{settings.folder}"

    # ── the poll contract ──
    async def poll(
        self, watched_channels: list[str], checkpoints: dict[str, str], user_id: str
    ) -> tuple[list[IncomingMessage], dict[str, str]]:
        settings = reload_settings()
        self._log_posture_once(settings)

        # Not fully configured yet — nothing to poll, cursor untouched.
        if not settings.configured:
            return [], dict(checkpoints)

        # Fail closed: an empty allowlist surfaces NOTHING and never connects. This is the
        # structural guarantee (guardrail 1) — not a per-message filter, an upstream refusal.
        if not settings.allow_senders:
            return [], dict(checkpoints)

        password = self._resolve_password()
        if not password:
            logger.warning("mail-inbox: no IMAP password in the credential store — cannot poll")
            return [], dict(checkpoints)

        return await asyncio.to_thread(self._poll_sync, settings, password, dict(checkpoints))

    def _poll_sync(
        self, settings: MailInboxSettings, password: str, checkpoints: dict[str, str]
    ) -> tuple[list[IncomingMessage], dict[str, str]]:
        key = self._checkpoint_key(settings)
        last_uid = _parse_int(checkpoints.get(key, "0"))
        cursor = last_uid
        seen_ids = self._load_seen_ids()
        new_ids: list[str] = []
        messages: list[IncomingMessage] = []

        client = self._make_client(settings, password)
        try:
            client.connect()
            uids = client.fetch_uids_since(settings.folder, last_uid)
            for uid in sorted(uids):
                raw = client.fetch_message(settings.folder, uid)
                if not raw:
                    # Transient per-UID fetch failure: STOP so the cursor never advances
                    # past an unread message — next poll resumes here (never skips).
                    logger.debug("mail-inbox: empty fetch for uid %s — pausing at cursor", uid)
                    break
                # A message we successfully fetched IS processed (accepted, rejected, or
                # duplicate) — advance the cursor so a restart doesn't reprocess it.
                cursor = uid
                incoming = self._process_message(raw, settings, uid, seen_ids, new_ids)
                if incoming is not None:
                    messages.append(incoming)
        except ImapError as exc:
            logger.warning("mail-inbox: IMAP poll failed: %s — will retry next cycle", exc)
        finally:
            try:
                client.close()
            except Exception:
                logger.debug("mail-inbox: client close error", exc_info=True)

        checkpoints[key] = str(cursor)
        if new_ids:
            self._save_seen_ids(seen_ids, new_ids)
        return messages, checkpoints

    def _process_message(
        self,
        raw: bytes,
        settings: MailInboxSettings,
        uid: int,
        seen_ids: list[str],
        new_ids: list[str],
    ) -> IncomingMessage | None:
        """Parse one RFC822 message, enforce the allowlist + dedup, and map it. None = dropped."""
        try:
            msg = email.message_from_bytes(raw, policy=email.policy.default)
        except Exception:
            logger.debug("mail-inbox: message parse failed for uid %s", uid, exc_info=True)
            return None

        _, from_addr = email.utils.parseaddr(str(msg.get("From", "")))
        if not _sender_allowed(from_addr, settings.allow_senders):
            self._log_rejection(from_addr, uid)
            return None

        message_id = str(msg.get("Message-ID", "")).strip()
        if message_id and (message_id in seen_ids or message_id in new_ids):
            return None  # dedup belt — same message seen before / twice this poll
        if message_id:
            new_ids.append(message_id)

        return self._to_incoming(msg, settings, from_addr, uid, message_id)

    @staticmethod
    def _to_incoming(
        msg: "email.message.EmailMessage",
        settings: MailInboxSettings,
        from_addr: str,
        uid: int,
        message_id: str,
    ) -> IncomingMessage:
        subject = str(msg.get("Subject", "")).strip()
        body = extract_body(msg)
        # Subject + body carried RAW into the item text (and thus the event value);
        # fencing is downstream (EIAT-4), never here.
        text = f"Subject: {subject}\n\n{body}".strip() if subject else body

        display, _ = email.utils.parseaddr(str(msg.get("From", "")))
        # thread_id from the reply chain (In-Reply-To wins; else the first References id).
        thread_id = str(msg.get("In-Reply-To", "")).strip()
        if not thread_id:
            refs = str(msg.get("References", "")).split()
            thread_id = refs[0] if refs else ""

        ts = MailInboxProvider._parse_date(msg)

        return IncomingMessage(
            id=message_id or f"{settings.folder}:{uid}",
            channel_id=settings.receiving_address,  # the address → how a bound address is told apart
            channel_name=settings.receiving_address or SOURCE_NAME,
            thread_id=thread_id or None,
            text=text,
            sender_id=from_addr,  # the allowlist key
            sender_name=display or from_addr,
            timestamp=ts,
            thread_context=[],
            is_dm=False,
        )

    @staticmethod
    def _parse_date(msg: "email.message.EmailMessage") -> float:
        raw = msg.get("Date", "")
        if raw:
            try:
                return email.utils.parsedate_to_datetime(str(raw)).timestamp()
            except (TypeError, ValueError, OverflowError):
                pass
        return time.time()

    # ── outbound / reactions / history: inbound-only for EIAT-2 ──
    async def send_reply(self, channel_id: str, text: str, thread_ts: str | None = None) -> bool:
        # SMTP send_reply (draft-by-default) is EIAT-3. Inbound-only here: no send.
        return False

    async def add_reaction(self, channel_id: str, ts: str, emoji: str) -> bool:
        return False

    async def get_channel_history(
        self, channel_id: str, oldest: str, limit: int = 200
    ) -> list[dict[str, Any]]:
        return []

    async def resolve_user_name(self, user_id: str) -> str:
        return user_id


Provider = MailInboxProvider


def create_provider(config: dict[str, Any] | None = None) -> MailInboxProvider:
    """Extension factory for the mail inbox source (app.json provider.implementation)."""
    return MailInboxProvider(config)
