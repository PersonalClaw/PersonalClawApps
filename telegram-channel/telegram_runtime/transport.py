"""TelegramTransport — the ChannelTransportProvider that owns the Telegram channel.

Outbound + health/test are token-gated and always available. Inbound is a
``getUpdates`` long-poll loop started by :meth:`start_inbound`, which the gateway
calls once at boot with a :class:`GatewayServices` handle. The loop:

1. long-polls ``getUpdates`` (offset persisted in the app's ``data/`` dir so a
   restart resumes where it left off, never reprocessing an update);
2. normalizes each ``message`` to a :class:`ChannelMessage`;
3. runs it through the core sender-trust seam (:func:`guard_inbound`) — DM pairing,
   group tracked-only, and non-owner-content fencing all happen there, so this
   transport can't forget them;
4. routes an allowed message to a channel-linked dashboard session and drives one
   turn via core ``run_chat`` — core then mirrors the reply back out through the
   :class:`TelegramDelivery` this transport registers at boot (the outbound half of
   the seam). ``callback_query`` updates (inline-keyboard button presses) resolve a
   pending approval in the delivery.

Webhook mode is deferred to EXTERNAL-ACCESS by the plan; long-poll is the whole
inbound story here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys as _sys
from pathlib import Path as _Path
from typing import Any

# The app loader only keeps this app's dir on sys.path while it execs the entry
# module. This is a multi-module package whose modules import each other for the
# life of the process (the poll loop, delivery, and settings all resolve
# ``telegram_runtime.*`` long after boot). Pin the app dir on sys.path so those
# imports keep resolving — a real installed package would be permanently importable.
_APP_DIR = str(_Path(__file__).resolve().parents[1])
if _APP_DIR not in _sys.path:
    _sys.path.insert(0, _APP_DIR)

from personalclaw.sdk.channel import (
    ChannelCapabilities,
    ChannelMessage,
    ChannelTransportProvider,
    OutboundMessage,
    guard_inbound,
    redact_credentials,
    redact_exfiltration_urls,
)

# Import ALL runtime deps at MODULE level (not lazily inside methods): the loader
# only keeps this app's dir on sys.path while it execs this module, so a
# ``from telegram_runtime.X import`` inside a method would run LATER, off the path,
# and fail. Binding them here, during exec, captures them for the process life.
from telegram_runtime.api import DEFAULT_POLL_TIMEOUT, HTTPTelegramAPI, TelegramAPI, TelegramAPIError
from telegram_runtime.delivery import TelegramDelivery
from telegram_runtime.settings import (
    ACTIVATION_OFF,
    CRED_TELEGRAM_BOT_TOKEN,
    get_settings,
    reload_settings,
)
from telegram_runtime.writes import SendRefused, live_writes_disabled

logger = logging.getLogger(__name__)

PROVIDER = "telegram"
# The update types we ask Telegram for — everything else (edited messages, polls,
# channel posts) is noise for a DM/group bot and just inflates the poll payload.
ALLOWED_UPDATES = ["message", "callback_query"]
_OFFSET_FILE = "poll_offset.json"


class TelegramTransport(ChannelTransportProvider):
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        import os

        cfg = config or {}
        # Per-instance config wins; else the shared credential store the gateway
        # propagates into the environment under this app's own key.
        self._token = cfg.get("bot_token", "") or os.environ.get(CRED_TELEGRAM_BOT_TOKEN, "")
        self._services: Any = None
        self._api: TelegramAPI | None = None
        self._delivery: TelegramDelivery | None = None
        self._poll_task: asyncio.Task | None = None
        self._stopping = False
        self._offset = 0

    @property
    def name(self) -> str:
        return PROVIDER

    @property
    def display_name(self) -> str:
        return "Telegram"

    def capabilities(self) -> ChannelCapabilities:
        # Honest: streaming is edit-based (not native chunk append), rich text is
        # MarkdownV2 (a limited subset), threads are reply-chains. Telegram caps a
        # message at 4096 chars.
        return ChannelCapabilities(
            inbound=True, threads=True, attachments=True, reactions=False,
            edits=True, rich_text=True, typing_indicator=False, max_text_len=4096,
        )

    async def connect(self) -> bool:
        return bool(self._token)

    async def disconnect(self) -> None:
        if self._api is not None:
            await self._api.close()

    @property
    def connected(self) -> bool:
        return bool(self._token)

    # ── offset persistence (resume the long-poll across restarts) ──
    def _offset_path(self) -> _Path:
        from personalclaw.sdk.channel import ProviderSettings

        return ProviderSettings.config_path("telegram-channel").parent / _OFFSET_FILE

    def _load_offset(self) -> int:
        try:
            data = json.loads(self._offset_path().read_text(encoding="utf-8"))
            return int(data.get("offset", 0))
        except (OSError, ValueError, json.JSONDecodeError):
            return 0

    def _save_offset(self, offset: int) -> None:
        from personalclaw.sdk.channel import atomic_write

        path = self._offset_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, json.dumps({"offset": offset}) + "\n")
        except OSError:
            logger.debug("telegram: failed to persist poll offset", exc_info=True)

    # ── Inbound: the gateway drives this once at boot ──
    async def start_inbound(self, services: Any) -> None:
        if not self._token:
            logger.info("TelegramTransport: no bot token — inbound stays offline")
            return
        self._services = services
        reload_settings()
        self._api = HTTPTelegramAPI(self._token)

        # Register outbound delivery on the gateway + dashboard. Core delivers every
        # channel result through this ONE provider-agnostic ChannelDelivery handle —
        # it never sees the Telegram API client.
        owner_id = self._resolve_owner_id(services)
        self._delivery = TelegramDelivery(self._api, owner_id)
        if hasattr(services, "register_channel_delivery"):
            services.register_channel_delivery(self._delivery)
        if getattr(services, "dashboard_state", None) is not None:
            services.dashboard_state.channel_delivery = self._delivery

        self._offset = self._load_offset()
        self._stopping = False
        self._poll_task = asyncio.ensure_future(self._poll_loop())
        logger.info("TelegramTransport: long-poll inbound started (offset=%d)", self._offset)

    @staticmethod
    def _resolve_owner_id(services: Any) -> str:
        from personalclaw.sdk.channel import CRED_OWNER_ID

        try:
            creds = services.config.load_credentials()
            return creds.get(CRED_OWNER_ID, "") or getattr(services, "owner_id", "")
        except Exception:
            return getattr(services, "owner_id", "")

    async def stop_inbound(self) -> None:
        self._stopping = True
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(self._poll_task), timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception:
                logger.debug("telegram: poll task stop error", exc_info=True)
        if self._api is not None:
            await self._api.close()

    async def _poll_loop(self) -> None:
        """Long-poll getUpdates, dispatching each update. Degrades, never crashes."""
        backoff = 1.0
        while not self._stopping:
            try:
                updates = await self._api.get_updates(  # type: ignore[union-attr]
                    offset=self._offset, timeout=DEFAULT_POLL_TIMEOUT,
                    allowed_updates=ALLOWED_UPDATES,
                )
                backoff = 1.0
                for update in updates:
                    # Advance past this update_id BEFORE dispatch so a handler that
                    # raises can't wedge the loop on the same update forever.
                    uid = int(update.get("update_id", 0))
                    if uid >= self._offset:
                        self._offset = uid + 1
                    try:
                        await self._dispatch(update)
                    except Exception:
                        logger.warning("telegram: update dispatch failed", exc_info=True)
                if updates:
                    self._save_offset(self._offset)
            except asyncio.CancelledError:
                raise
            except TelegramAPIError as exc:
                if exc.error_code == 401:
                    logger.error("telegram: invalid bot token (401) — inbound offline")
                    return
                logger.warning("telegram: getUpdates error: %s — backing off %ss", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            except Exception:
                logger.warning("telegram: poll loop error — backing off %ss", backoff, exc_info=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _dispatch(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            await self._on_callback_query(update["callback_query"])
            return
        message = update.get("message")
        if message:
            await self._on_message(message)

    async def _on_callback_query(self, cq: dict[str, Any]) -> None:
        """An inline-keyboard button press — resolve a pending approval (delivery owns it)."""
        if self._delivery is not None:
            await self._delivery.resolve_callback(cq)

    def _to_channel_message(self, message: dict[str, Any]) -> ChannelMessage:
        chat = message.get("chat", {}) or {}
        frm = message.get("from", {}) or {}
        sender_name = " ".join(
            p for p in (frm.get("first_name", ""), frm.get("last_name", "")) if p
        ) or frm.get("username", "")
        return ChannelMessage(
            channel_id=str(chat.get("id", "")),
            text=message.get("text", "") or message.get("caption", "") or "",
            sender=str(frm.get("id", "")),
            thread_id=str(chat.get("id", "")),
            message_id=str(message.get("message_id", "")),
            ts=float(message.get("date", 0)),
            metadata={
                "chat_type": chat.get("type", ""),
                "sender_name": sender_name,
                "username": frm.get("username", ""),
            },
        )

    async def _on_message(self, message: dict[str, Any]) -> None:
        cm = self._to_channel_message(message)
        if not cm.text.strip():
            return  # nothing to act on (sticker, media without caption, etc.)

        chat_type = cm.metadata.get("chat_type", "")
        is_dm = chat_type == "private"
        settings = get_settings()
        if is_dm and settings.dm_activation == ACTIVATION_OFF:
            return

        state = getattr(self._services, "dashboard_state", None)
        verdict = guard_inbound(
            state, PROVIDER, cm.sender,
            sender_name=cm.metadata.get("sender_name", ""),
            channel_id=cm.channel_id, is_dm=is_dm, text=cm.text,
        )
        if not verdict.allowed:
            if verdict.canned_reply and self._delivery is not None:
                try:
                    await self._delivery.deliver_text(cm.channel_id, verdict.canned_reply)
                except Exception:
                    logger.debug("telegram: canned reply send failed", exc_info=True)
            return

        # Non-owner group content is fenced by the seam — feed the fenced form to the
        # session so the model reads it as data, not instructions.
        text_for_session = verdict.fenced_text or cm.text
        await self._route_to_session(cm, text_for_session)

    async def _route_to_session(self, cm: ChannelMessage, text: str) -> None:
        """Link a dashboard session to this chat and drive one turn via core.

        Core's ``run_chat`` mirrors the reply back out through our registered
        TelegramDelivery for a channel-linked session, so this transport never
        renders outbound itself — the seam does."""
        state = getattr(self._services, "dashboard_state", None)
        if state is None:
            logger.warning("telegram: no dashboard state — cannot route message")
            return
        from personalclaw.sdk.channel import run_chat

        thread_key = cm.channel_id  # one session per Telegram chat
        session = state.get_linked_session(thread_key)
        if session is None:
            session = state.get_or_create_session(app="telegram")
            state.link_channel(session.key, thread_key, cm.channel_id)

        safe, _ = redact_exfiltration_urls(text)
        safe, _ = redact_credentials(safe)
        session.append("user", safe, "msg msg-u")
        if getattr(session, "running", False):
            session.queue_append(text)
            return
        task = asyncio.ensure_future(run_chat(state, session, text))
        session.task = task
        tasks = getattr(state, "_background_tasks", None)
        if tasks is not None:
            tasks.add(task)
            task.add_done_callback(tasks.discard)

    async def send(self, message: OutboundMessage) -> bool | SendRefused:
        """Transmit one outbound message. ``True`` delivered, ``False`` failed, or a
        :class:`SendRefused` when the platform's live-writes kill switch is on.

        The refusal is checked AFTER the token gate on purpose: an unconfigured
        transport could not have written anything, so reporting "refused" there would
        claim the guard suppressed a write that was never possible. Only a transport
        that WOULD have transmitted reports a refusal.
        """
        if not self._token:
            return False
        # DISABLE_LIVE_WRITES (§1.4). A Telegram message is a live, outward,
        # instantly-human-visible write with no undo — the same class core refuses for
        # non-GET egress and model deletion. Typed refusal, never a silent no-op: a
        # test (or an operator) asserting a send must be able to see that the guard,
        # not the network, stopped it.
        if live_writes_disabled():
            refusal = SendRefused(channel=PROVIDER, target=message.channel_id)
            logger.warning("TelegramTransport.send refused: %s", refusal)
            return refusal
        try:
            api = self._api or HTTPTelegramAPI(self._token)
            from telegram_runtime.format import to_markdown_v2

            await api.send_message(
                message.channel_id, to_markdown_v2(message.text),
                parse_mode="MarkdownV2",
                reply_to_message_id=int(message.thread_id) if message.thread_id.isdigit() else None,
            )
            return True
        except Exception as exc:
            logger.warning("TelegramTransport.send failed: %s", exc)
            return False

    async def health(self) -> dict[str, Any]:
        if not self._token:
            return {"state": "offline", "detail": "No bot token configured"}
        return {"state": "ready", "detail": "Bot token configured"}

    async def test(self) -> dict[str, Any]:
        if not self._token:
            return {"ok": False, "detail": "No bot token configured"}
        api = HTTPTelegramAPI(self._token)
        try:
            me = await api.get_me()
            uname = me.get("username") or me.get("first_name") or "bot"
            return {"ok": True, "detail": f"Authenticated as @{uname}"}
        except Exception as exc:
            return {"ok": False, "detail": f"getMe failed: {exc}"}
        finally:
            await api.close()


def create_provider(config: dict[str, Any] | None = None) -> "TelegramTransport":
    return TelegramTransport(config)
