"""TelegramTransport — the ChannelTransportProvider that owns the Telegram channel.

Outbound + health/test are token-gated and always available. Inbound is a
``getUpdates`` long-poll loop started by :meth:`start_inbound`, which the gateway
calls with a :class:`GatewayServices` handle whenever it starts this channel's
receiver: at boot, and when the channel is turned on, updated or its settings are
saved (it stops the previous instance's receiver first). The loop:

1. long-polls ``getUpdates`` (offset persisted in the app's ``data/`` dir so a
   restart resumes where it left off, never reprocessing an update);
2. normalizes each ``message`` to a :class:`ChannelMessage`;
3. hands it to the platform's guarded door (``services.deliver_channel_inbound``) —
   the trust gate, DM pairing, group tracked-only, non-owner-content fencing,
   redaction, session linking and the turn itself all happen in core, so this
   transport can't forget any of them; it keeps only the outbound half, delivering
   the verdict's canned reply. Core mirrors agent replies back out through the
   :class:`TelegramDelivery` this transport registers as its receiver starts (the
   outbound half of the seam). ``callback_query`` updates (inline-keyboard button
   presses) resolve a pending approval in the delivery.

Webhook mode is deferred to EXTERNAL-ACCESS by the plan; long-poll is the whole
inbound story here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
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
    owner_id_for,
)

# Import ALL runtime deps at MODULE level (not lazily inside methods): the loader
# only keeps this app's dir on sys.path while it execs this module, so a
# ``from telegram_runtime.X import`` inside a method would run LATER, off the path,
# and fail. Binding them here, during exec, captures them for the process life.
from telegram_runtime.api import DEFAULT_POLL_TIMEOUT, HTTPTelegramAPI, TelegramAPI, TelegramAPIError
from telegram_runtime.delivery import TelegramDelivery, send_parts
from telegram_runtime.inbound_tap import publish as publish_inbound
from telegram_runtime.settings import (
    ACTIVATION_OFF,
    PROVIDER,
    LiveConfig,
    adopt_owner_id,
    get_settings,
    load_bot_token,
    reload_settings,
)
from telegram_runtime.writes import SendRefused, live_writes_disabled

logger = logging.getLogger(__name__)

# The update types we ask Telegram for — everything else (edited messages, polls,
# channel posts) is noise for a DM/group bot and just inflates the poll payload.
ALLOWED_UPDATES = ["message", "callback_query"]
_OFFSET_FILE = "poll_offset.json"
#: The longest the receiver waits between two failed long-polls.
_MAX_BACKOFF = 30.0


def _why_the_poll_failed(exc: BaseException) -> str:
    """A failed long-poll in words safe to show: Telegram's own answer, never the request.

    An answer from Telegram carries its code and description. A failure with no code never got
    one — the client could not reach Telegram, or got back something that is not the Bot API —
    and its text is the HTTP client's, which can carry the request URL, and the URL carries the
    token. So that case, and anything else, is named rather than quoted."""
    if isinstance(exc, TelegramAPIError):
        if not exc.error_code:
            return "Telegram could not be reached, or did not answer like the Bot API"
        code = str(exc.error_code)
        return exc.description if code in exc.description else f"{exc.description} (code {code})"
    return f"an unexpected {type(exc).__name__}"

#: What ``sendMessage`` takes as ``chat_id``: an integer id (negative for groups and channels), or a
#: public channel's ``@username`` (5 to 32 letters, digits or underscores, starting with a letter).
_TARGET_RE = re.compile(r"-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{4,31}")


class TelegramTransport(ChannelTransportProvider):
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        # Kept live, so a Configure → Save reaches the next Test/Connect/health/send (see
        # LiveConfig) instead of the next restart.
        self._config = LiveConfig(config or {})
        #: The bot token the long-poll receiver was started with (``""`` when there was none) —
        #: ``None`` until the gateway started it. The receiver keeps the token it started with.
        self._inbound_token: str | None = None
        self._services: Any = None
        self._api: TelegramAPI | None = None
        self._delivery: TelegramDelivery | None = None
        self._poll_task: asyncio.Task | None = None
        self._stopping = False
        self._offset = 0
        #: Why the long-poll receiver gave up for good (``""`` while it runs). The loop used to end
        #: on a 401 with only a log line, and ``health`` kept answering "ready".
        self._inbound_stopped = ""
        #: Why the last long-poll failed while the receiver retries it (``""`` once one succeeds).
        self._poll_failure = ""

    @property
    def name(self) -> str:
        return PROVIDER

    @property
    def display_name(self) -> str:
        return "Telegram"

    def validate_target(self, target: str) -> str:
        """Whether a schedule can send its results to ``target`` on Telegram.

        ``sendMessage`` takes a chat id, a number that is negative for a group, supergroup or
        channel (``-1001234567890``), or a public channel's ``@username``. Anything else would be
        refused by Telegram at send time, so it is refused here, in words, instead.
        """
        if _TARGET_RE.fullmatch(str(target or "").strip()):
            return ""
        return (
            "A Telegram chat id is a number, like 4242, or -1001234567890 for a group, or a "
            "public channel's @username."
        )

    def capabilities(self) -> ChannelCapabilities:
        # Honest: streaming is edit-based (not native chunk append), rich text is
        # MarkdownV2 (a limited subset), threads are reply-chains. Telegram caps a
        # message at 4096 chars.
        # owner_pairing: DMs cross core's guarded door, where the owner's code from the Configure
        # page is redeemed, and the delivery reads the owner each time it needs it.
        # dm_thread_is_channel: every DM message carries its chat id as its thread (see
        # _to_channel_message), so a chat handed off to Telegram continues in the DM.
        return ChannelCapabilities(
            inbound=True, threads=True, attachments=True, reactions=False,
            edits=True, rich_text=True, typing_indicator=False, max_text_len=4096,
            owner_pairing=True, dm_thread_is_channel=True,
        )

    def _token(self) -> str:
        """The bot token as configured now: per-instance config wins; else the plain-named token
        the gateway exports into the environment (see :func:`load_bot_token`)."""
        return load_bot_token(self._config.current())

    async def connect(self) -> bool:
        return bool(self._token())

    async def disconnect(self) -> None:
        if self._api is not None:
            await self._api.close()

    @property
    def connected(self) -> bool:
        return bool(self._token())

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

    # ── Inbound: the gateway starts and stops this with the channel ──
    async def start_inbound(self, services: Any) -> None:
        # Before the token check, so a channel configured later keeps its owner too.
        adopt_owner_id()
        token = self._token()
        self._inbound_token = token
        if not token:
            logger.info("TelegramTransport: no bot token — inbound stays offline")
            return
        self._services = services
        reload_settings()
        self._api = HTTPTelegramAPI(token)

        # Register outbound delivery on the gateway + dashboard. Core delivers every
        # channel result through this ONE provider-agnostic ChannelDelivery handle —
        # it never sees the Telegram API client. Filed under PROVIDER, the name core reads this
        # channel's owner by. The delivery reads Telegram's OWN owner each time it needs it, so an
        # owner paired from the Configure page while this receiver runs is the one it prompts.
        self._delivery = TelegramDelivery(self._api, lambda: owner_id_for(PROVIDER))
        if hasattr(services, "register_channel_delivery"):
            services.register_channel_delivery(self._delivery, provider=PROVIDER)
        if getattr(services, "dashboard_state", None) is not None:
            services.dashboard_state.channel_delivery = self._delivery

        self._offset = self._load_offset()
        self._stopping = False
        self._poll_task = asyncio.ensure_future(self._poll_loop())
        logger.info("TelegramTransport: long-poll inbound started (offset=%d)", self._offset)

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
        """Long-poll getUpdates, dispatching each update. Degrades, never crashes.

        Every way it stops receiving is recorded for :meth:`health`: a token Telegram rejects ends
        the loop (``_inbound_stopped``), and a poll that fails and is retried leaves its reason
        (``_poll_failure``) until the next one succeeds."""
        backoff = 1.0
        while not self._stopping:
            try:
                updates = await self._api.get_updates(  # type: ignore[union-attr]
                    offset=self._offset, timeout=DEFAULT_POLL_TIMEOUT,
                    allowed_updates=ALLOWED_UPDATES,
                )
                backoff = 1.0
                self._poll_failure = ""
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
                    logger.error("telegram: invalid bot token (401) — the long-poll receiver stopped")
                    self._inbound_stopped = (
                        "Telegram rejected the bot token (401 Unauthorized), so the long-poll "
                        "receiver stopped. Save a working token from @BotFather in Configure to "
                        "start it again; sending with this token fails too."
                    )
                    return
                self._poll_failure = _why_the_poll_failed(exc)
                logger.warning("telegram: getUpdates error: %s — backing off %ss", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)
            except Exception as exc:
                self._poll_failure = _why_the_poll_failed(exc)
                logger.warning("telegram: poll loop error — backing off %ss", backoff, exc_info=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)

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
                # A group's title: core lists an untracked group that messaged the bot by it, on
                # the Sender trust page, so the owner can tell which group to track.
                "channel_name": chat.get("title", "") or "",
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

        # The guarded door (EA-7). Core applies the trust gate, pairing-code
        # redemption, the non-owner-content fence, redaction, session linking and
        # the turn itself — the routing this transport used to carry a copy of.
        # This transport keeps only the outbound half: the canned reply.
        verdict = await self._services.deliver_channel_inbound(PROVIDER, cm, is_dm=is_dm)
        if verdict.canned_reply and self._delivery is not None:
            try:
                await self._delivery.deliver_text(cm.channel_id, verdict.canned_reply)
            except Exception:
                logger.debug("telegram: canned reply send failed", exc_info=True)

        # CE-10: hand the message to this bundle's own trigger source, if one is attached.
        # AFTER the door and gated on `verdict.allowed`, which is the entire security
        # property: a denied stranger gets no session, and must equally get no trigger
        # fire — otherwise anyone who can reach the bot can arm the owner's automations.
        # The tap never raises and knows nothing about events; see inbound_tap's docstring.
        if verdict.allowed:
            publish_inbound(cm, is_dm=is_dm)

    async def send(self, message: OutboundMessage) -> bool | SendRefused:
        """Transmit one outbound message. ``True`` delivered, ``False`` failed, or a
        :class:`SendRefused` when the platform's live-writes kill switch is on.

        The refusal is checked AFTER the token gate on purpose: an unconfigured
        transport could not have written anything, so reporting "refused" there would
        claim the guard suppressed a write that was never possible. Only a transport
        that WOULD have transmitted reports a refusal.
        """
        token = self._token()
        if not token:
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
        # The receiver's client carries the token inbound started with; borrow it unless the
        # configured token has moved on since, so a rotated token is what sends.
        borrowed = self._api is not None and self._inbound_token in (None, token)
        api = self._api if borrowed else HTTPTelegramAPI(token)
        try:
            # A text that renders to no message at all ("", ANSI codes, blank lines) sends
            # nothing and says so, instead of reporting a delivery that did not happen.
            sent = await send_parts(
                api,  # type: ignore[arg-type]
                message.channel_id, message.text,
                reply_to_message_id=int(message.thread_id) if message.thread_id.isdigit() else None,
            )
            return bool(sent)
        except Exception as exc:
            logger.warning("TelegramTransport.send failed: %s", exc)
            return False
        finally:
            if not borrowed:
                await api.close()  # type: ignore[union-attr]

    async def health(self) -> dict[str, Any]:
        token = self._token()
        if not token:
            return {"state": "offline", "detail": "No bot token configured"}
        if self._inbound_token is None:
            # A transport whose receiver the gateway has not started yet. Its token is live for
            # outbound; "ready" would claim a receiver that does not exist. The gateway starts
            # one on the instance it has whenever the channel changes (turned on, updated, its
            # settings saved) — and this state is how it tells the channel is configured.
            return {
                "state": "error",
                "detail": (
                    "Outbound ready, inbound NOT STARTED — the gateway starts the long-poll "
                    "receiver when it turns the channel on, and Configure → Save starts it now."
                ),
            }
        if self._inbound_token != token:
            # Saved tokens reach outbound at once, the long-poll receiver only when it starts. A
            # token saved in the running gateway replaces this instance and its receiver; one
            # changed outside it is what this reports.
            if self._poll_task is not None and not self._poll_task.done():
                detail = (
                    "Outbound uses the saved bot token; the long-poll receiver still runs on "
                    "the one it started with. Configure → Save, or turning the channel off and "
                    "on, moves inbound onto it."
                )
            else:
                detail = (
                    "Outbound ready, inbound OFFLINE — the long-poll receiver was started before "
                    "this bot token was saved. Configure → Save, or turning the channel off and "
                    "on, starts it on this one."
                )
            return {"state": "error", "detail": detail}
        # The receiver runs on the saved token. Whether it is RECEIVING is the poll loop's to say.
        if self._inbound_stopped:
            return {"state": "error", "detail": f"Inbound STOPPED — {self._inbound_stopped}"}
        if self._poll_failure:
            return {
                "state": "error",
                "detail": (
                    f"Inbound NOT RECEIVING — the last long-poll failed: {self._poll_failure}. "
                    f"The receiver keeps retrying, waiting up to {_MAX_BACKOFF:g} seconds between "
                    "tries."
                ),
            }
        return {"state": "ready", "detail": "Bot token configured"}

    async def test(self) -> dict[str, Any]:
        token = self._token()
        if not token:
            return {"ok": False, "detail": "No bot token configured"}
        api = HTTPTelegramAPI(token)
        try:
            me = await api.get_me()
            uname = me.get("username") or me.get("first_name") or "bot"
        except Exception as exc:
            return {"ok": False, "detail": f"getMe failed: {exc}"}
        finally:
            await api.close()
        # The channel contract: test() is not ok whenever health() is not ready. Derived from
        # health() rather than re-decided, so the two cannot drift.
        health = await self.health()
        if health["state"] != "ready":
            return {"ok": False, "detail": f"Authenticated as @{uname}, but {health['detail']}"}
        return {"ok": True, "detail": f"Authenticated as @{uname}"}


def create_provider(config: dict[str, Any] | None = None) -> "TelegramTransport":
    return TelegramTransport(config)
