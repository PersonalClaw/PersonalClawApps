"""DiscordTransport — the ChannelTransportProvider that owns the Discord channel.

Outbound + health/test are token-gated and always available. Inbound is the Gateway
WebSocket loop started by :meth:`start_inbound`, which PersonalClaw's gateway calls
with a :class:`GatewayServices` handle whenever it starts this channel's receiver: at
boot, and when the channel is turned on, updated or its settings are saved (it stops
the previous instance's receiver first). The loop:

1. holds a gateway connection (:class:`DiscordGateway` owns identify/heartbeat/
   resume — see its module docstring for the WS lifecycle);
2. normalizes each ``MESSAGE_CREATE`` to a :class:`ChannelMessage`;
3. hands it to the platform's guarded door (``services.deliver_channel_inbound``) —
   the trust gate, DM pairing, guild-channel tracked-only, non-owner-content
   fencing, redaction, session linking and the turn itself all happen in core, so
   this transport can't forget any of them; it keeps only the outbound half,
   delivering the verdict's canned reply. Core mirrors agent replies back out
   through the :class:`DiscordDelivery` this transport registers as its receiver
   starts (the outbound half of the seam). ``INTERACTION_CREATE`` events (button
   presses) resolve a pending approval in the delivery.

Two Discord-specific inbound facts shape this file:

* **The bot sees its OWN messages.** ``MESSAGE_CREATE`` fires for the bot's own
  sends, so a transport that doesn't filter them feeds its own reply back into the
  session and loops forever. Telegram's ``getUpdates`` never does this, so the trap
  is new here: :meth:`_is_self_authored` drops anything from a bot account or from
  our own user id (captured from READY), and a test proves it.
* **DM-ness is the ABSENCE of ``guild_id``.** Discord marks a guild message by
  attaching ``guild_id``; a DM simply has none. That is the signal used for
  ``is_dm`` — not a channel-``type`` guess, which would need an extra REST lookup
  the event payload makes unnecessary.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys as _sys
from pathlib import Path as _Path
from typing import Any

# The app loader only keeps this app's dir on sys.path while it execs the entry
# module. This is a multi-module package whose modules import each other for the
# life of the process (the gateway loop, delivery, and settings all resolve
# ``discord_runtime.*`` long after boot). Pin the app dir on sys.path so those
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
# ``from discord_runtime.X import`` inside a method would run LATER, off the path,
# and fail. Binding them here, during exec, captures them for the process life.
from discord_runtime.api import DISCORD_MAX_TEXT, DiscordAPI, DiscordAPIError, HTTPDiscordAPI
from discord_runtime.delivery import DiscordDelivery, split_message
from discord_runtime.gateway import DEFAULT_GATEWAY_URL, DiscordGateway
from discord_runtime.inbound_tap import publish as publish_inbound
from discord_runtime.settings import (
    ACTIVATION_OFF,
    PROVIDER,
    LiveConfig,
    adopt_owner_id,
    get_settings,
    load_bot_token,
    reload_settings,
)
from discord_runtime.writes import SendRefused, live_writes_disabled

#: A Discord id: a snowflake, 17 to 20 digits. Channels, threads and DMs are all addressed by one.
_SNOWFLAKE_RE = re.compile(r"\d{17,20}")

logger = logging.getLogger(__name__)

#: Discord refused the token at ``GET /gateway/bot``, before any session was opened.
_TOKEN_REFUSED_AT_DISCOVERY = (
    "Discord rejected the bot token (401 Unauthorized), so no gateway session was opened. Save a "
    "working token in Configure to start one; sending with this token fails too."
)
#: What each close code Discord says not to retry after means for the owner — the same set as
#: ``gateway.FATAL_CLOSE_CODES``, which a test holds equal to this map's keys.
_REFUSALS = {
    4004: (
        "Discord rejected the bot token (close code 4004, authentication failed), so the gateway "
        "session stopped. Save a working token in Configure to start it again; sending with this "
        "token fails too."
    ),
    4014: (
        "Discord refused the privileged intent this bot asks for (close code 4014, disallowed "
        "intents), so the gateway session stopped. Turn on Message Content Intent under Bot → "
        "Privileged Gateway Intents in the Discord Developer Portal, then turn the channel off "
        "and on, or use Configure → Save, to connect again."
    ),
    4013: (
        "Discord rejected the gateway intents this app sends (close code 4013, invalid intents), "
        "so the gateway session stopped. That is a defect in the Discord Channel app, not in your "
        "settings: update the app."
    ),
    4012: (
        "Discord no longer accepts the gateway API version this app uses (close code 4012), so the "
        "gateway session stopped. Update the Discord Channel app."
    ),
    4011: (
        "Discord requires this bot to shard (close code 4011, sharding required): it is in too "
        "many servers for one gateway session, and this app opens one. The gateway session "
        "stopped."
    ),
    4010: (
        "Discord rejected the shard this app sent (close code 4010, invalid shard), so the gateway "
        "session stopped. That is a defect in the Discord Channel app: update the app."
    ),
}


class DiscordTransport(ChannelTransportProvider):
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        # Kept live, so a Configure → Save reaches the next Test/Connect/health/send (see
        # LiveConfig) instead of the next restart.
        self._config = LiveConfig(config or {})
        #: The bot token the receiver was started with (``""`` when there was none) — ``None``
        #: until PersonalClaw started it. The gateway session keeps the token it started with.
        self._inbound_token: str | None = None
        self._services: Any = None
        self._api: DiscordAPI | None = None
        self._delivery: DiscordDelivery | None = None
        self._gateway: DiscordGateway | None = None
        self._gateway_task: asyncio.Task | None = None
        # The bot's own user id, captured from READY — half of the self-message filter.
        self._own_user_id = ""
        #: Why the receiver did not open a gateway session at all (``""`` when it did): Discord
        #: refused the token at ``GET /gateway/bot``.
        self._inbound_stopped = ""

    @property
    def name(self) -> str:
        return PROVIDER

    @property
    def display_name(self) -> str:
        return "Discord"

    def validate_target(self, target: str) -> str:
        """Whether a schedule can send its results to ``target`` on Discord.

        Delivery posts to a channel by its id, a snowflake: a number of 17 to 20 digits. A DM is a
        channel too, which is what the owner's DM route opens.
        """
        if _SNOWFLAKE_RE.fullmatch(str(target or "").strip()):
            return ""
        return (
            "A Discord channel id is a long number, like 1234567890123456789. With Developer Mode "
            "on, right-click the channel and pick Copy Channel ID."
        )

    def capabilities(self) -> ChannelCapabilities:
        # Honest, and every True below has an implementation behind it:
        #   reactions        → DiscordDelivery.add_reaction (PUT .../reactions/{e}/@me)
        #   typing_indicator → DiscordDelivery.show_typing (POST /channels/{id}/typing)
        #   threads          → thread_id carried on ChannelMessage; a thread IS a
        #                      channel id in Discord, so replies target it directly
        #   edits            → DiscordDelivery streaming PATCHes the message
        #   rich_text        → Discord renders standard markdown natively
        # Discord caps a message body at 2000 chars.
        # owner_pairing → DMs cross core's guarded door, where the owner's code from the
        #                  Configure page is redeemed; the delivery reads the owner at each use
        # dm_thread_is_channel → a DM message's thread is its channel id (see
        #                  _to_channel_message), so a handed-off chat continues in the DM
        return ChannelCapabilities(
            inbound=True, threads=True, attachments=True, reactions=True,
            edits=True, rich_text=True, typing_indicator=True,
            max_text_len=DISCORD_MAX_TEXT, owner_pairing=True, dm_thread_is_channel=True,
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

    # ── Inbound: PersonalClaw's gateway starts and stops this with the channel ──
    async def start_inbound(self, services: Any) -> None:
        # Before the token check, so a channel configured later keeps its owner too.
        adopt_owner_id()
        token = self._token()
        self._inbound_token = token
        if not token:
            logger.info("DiscordTransport: no bot token — inbound stays offline")
            return
        self._services = services
        reload_settings()
        self._api = HTTPDiscordAPI(token)
        gateway_url = await self._discover_gateway_url()
        if self._inbound_stopped:
            # Discord refused the token: no session to open, and nothing it could deliver.
            await self._api.close()
            self._api = None
            return

        # Register outbound delivery on the gateway + dashboard. Core delivers every
        # channel result through this ONE provider-agnostic ChannelDelivery handle —
        # it never sees the Discord API client. Filed under PROVIDER, the name core reads this
        # channel's owner by. The delivery reads Discord's OWN owner each time it needs it, so an
        # owner paired from the Configure page while this receiver runs is the one it prompts.
        self._delivery = DiscordDelivery(self._api, lambda: owner_id_for(PROVIDER))
        if hasattr(services, "register_channel_delivery"):
            services.register_channel_delivery(self._delivery, provider=PROVIDER)
        if getattr(services, "dashboard_state", None) is not None:
            services.dashboard_state.channel_delivery = self._delivery

        self._gateway = DiscordGateway(
            token,
            gateway_url=gateway_url,
            on_message=self._on_message_create,
            on_interaction=self._on_interaction_create,
            on_ready=self._on_ready,
        )
        self._gateway_task = asyncio.ensure_future(self._gateway.run())
        logger.info("DiscordTransport: gateway inbound started")

    async def _discover_gateway_url(self) -> str:
        """The bot's own gateway URL from ``GET /gateway/bot``.

        Discord asks clients to fetch this rather than hardcode the host (it can move
        and it carries the session-start budget). Most failures here are not fatal — the
        documented default host still works — so degrade to it and let the gateway
        loop's own backoff report the real problem. A 401 is the exception: Discord has
        refused the token, and opening a session with it only has Discord refuse it again
        (close code 4004), so the refusal is kept for :meth:`health` instead."""
        try:
            info = await self._api.get_gateway_bot()  # type: ignore[union-attr]
            return str(info.get("url", "")) or DEFAULT_GATEWAY_URL
        except DiscordAPIError as exc:
            if exc.status == 401:
                logger.error("discord: GET /gateway/bot refused the bot token (401) — not connecting")
                self._inbound_stopped = _TOKEN_REFUSED_AT_DISCOVERY
                return ""
            logger.warning("discord: GET /gateway/bot failed (%s) — using the default gateway URL", exc)
            return DEFAULT_GATEWAY_URL
        except Exception:
            logger.warning("discord: GET /gateway/bot failed — using the default gateway URL")
            return DEFAULT_GATEWAY_URL

    async def stop_inbound(self) -> None:
        if self._gateway is not None:
            await self._gateway.stop()
        if self._gateway_task is not None:
            self._gateway_task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(self._gateway_task), timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception:
                logger.debug("discord: gateway task stop error", exc_info=True)
        if self._api is not None:
            await self._api.close()

    # ── event handlers (the gateway calls these) ──

    async def _on_ready(self, data: dict[str, Any]) -> None:
        """Capture the bot's own user id — the anchor of the self-message filter."""
        self._own_user_id = str((data.get("user") or {}).get("id", ""))
        logger.info("DiscordTransport: ready as user %s", self._own_user_id or "?")

    async def _on_interaction_create(self, interaction: dict[str, Any]) -> None:
        """A component press — resolve a pending approval (delivery owns it + the ack)."""
        if self._delivery is not None:
            await self._delivery.resolve_interaction(interaction)

    def _is_self_authored(self, message: dict[str, Any]) -> bool:
        """Whether this MESSAGE_CREATE is our own (or another bot's) message.

        MESSAGE_CREATE fires for the bot's OWN sends. Without this filter the reply
        we just posted arrives back as inbound, gets routed to the same session, and
        the bot talks to itself forever. Two independent signals, because either
        alone has a hole: ``author.bot`` also covers other bots and webhooks (which
        should never drive a session), and the id compare still works before READY
        lands or if a payload omits the flag."""
        author = message.get("author") or {}
        if bool(author.get("bot")):
            return True
        return bool(self._own_user_id) and str(author.get("id", "")) == self._own_user_id

    def _to_channel_message(self, message: dict[str, Any]) -> ChannelMessage:
        author = message.get("author") or {}
        return ChannelMessage(
            channel_id=str(message.get("channel_id", "")),
            text=message.get("content", "") or "",
            sender=str(author.get("id", "")),
            # In Discord a thread IS a channel, so the reply target is the channel id
            # whether it's a top-level channel or a thread inside one.
            thread_id=str(message.get("channel_id", "")),
            message_id=str(message.get("id", "")),
            metadata={
                # The presence/absence of guild_id is Discord's DM signal.
                "guild_id": str(message.get("guild_id", "") or ""),
                "sender_name": str(author.get("global_name") or author.get("username") or ""),
                "username": str(author.get("username", "") or ""),
            },
        )

    async def _on_message_create(self, message: dict[str, Any]) -> None:
        if self._is_self_authored(message):
            return
        cm = self._to_channel_message(message)
        if not cm.text.strip():
            return  # nothing to act on (embed-only, sticker, attachment without text)

        guild_id = cm.metadata.get("guild_id", "")
        is_dm = not guild_id
        if self._delivery is not None and guild_id:
            # Remember the channel's guild so build_thread_link can form a real URL.
            self._delivery.note_channel_guild(cm.channel_id, guild_id)

        settings = get_settings()
        if is_dm and settings.dm_activation == ACTIVATION_OFF:
            return

        # The guarded door. Core applies the trust gate, the pairing-code
        # redemption, the fence for non-owner guild content, redaction, session
        # linking and the turn itself — the routing this transport used to carry a
        # copy of. This transport keeps only the channel-specific outbound half:
        # delivering the verdict's canned reply as a Discord message.
        verdict = await self._services.deliver_channel_inbound(PROVIDER, cm, is_dm=is_dm)
        if verdict.canned_reply and self._delivery is not None:
            try:
                await self._delivery.deliver_text(cm.channel_id, verdict.canned_reply)
            except Exception:
                logger.debug("discord: canned reply send failed", exc_info=True)

        # Hand the message to this bundle's own trigger source, if one is attached.
        # AFTER the door and gated on `verdict.allowed`, which is the entire security
        # property: a denied poster gets no session, and must equally get no trigger fire —
        # otherwise anyone sharing a guild with the bot can arm the owner's automations.
        # The tap never raises and knows nothing about events; see inbound_tap's docstring.
        if verdict.allowed:
            publish_inbound(cm, is_dm=is_dm)

    # ── outbound / health ──

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
        # DISABLE_LIVE_WRITES. A Discord message is a live, outward,
        # instantly-human-visible write — the same class core refuses for non-GET egress
        # and model deletion. Typed refusal, never a silent no-op: a test (or an
        # operator) asserting a send must be able to see that the guard, not the
        # network, stopped it. Checked BEFORE the API client is built so a suppressed
        # send opens no connection at all.
        if live_writes_disabled():
            refusal = SendRefused(channel=PROVIDER, target=message.channel_id)
            logger.warning("DiscordTransport.send refused: %s", refusal)
            return refusal
        # The receiver's client carries the token inbound started with; borrow it unless the
        # configured token has moved on since, so a rotated token is what sends.
        borrowed = self._api is not None and self._inbound_token in (None, token)
        api = self._api if borrowed else HTTPDiscordAPI(token)
        try:
            for part in split_message(message.text):
                await api.create_message(message.channel_id, part)  # type: ignore[union-attr]
            return True
        except Exception as exc:
            logger.warning("DiscordTransport.send failed: %s", exc)
            return False
        finally:
            if not borrowed:
                await api.close()  # type: ignore[union-attr]

    async def health(self) -> dict[str, Any]:
        token = self._token()
        if not token:
            return {"state": "offline", "detail": "No bot token configured"}
        if self._inbound_token is None:
            # A transport whose receiver PersonalClaw has not started yet. Its token is live for
            # outbound; "ready" would claim a gateway session that does not exist. PersonalClaw
            # starts one on the instance it has whenever the channel changes (turned on, updated,
            # its settings saved) — and this state is how it tells the channel is configured.
            return {
                "state": "error",
                "detail": (
                    "Outbound ready, inbound NOT STARTED — PersonalClaw starts the Discord "
                    "gateway session when it turns the channel on, and Configure → Save starts "
                    "it now."
                ),
            }
        if self._inbound_token != token:
            # Saved tokens reach outbound at once, the gateway session only when it starts. A
            # token saved in the running PersonalClaw replaces this instance and its session;
            # one changed outside it is what this reports.
            if self._gateway_task is not None and not self._gateway_task.done():
                detail = (
                    "Outbound uses the saved bot token; the Discord gateway session still runs "
                    "on the one it started with. Configure → Save, or turning the channel off "
                    "and on, moves inbound onto it."
                )
            else:
                detail = (
                    "Outbound ready, inbound OFFLINE — the Discord gateway session was started "
                    "before this bot token was saved. Configure → Save, or turning the channel "
                    "off and on, starts it on this one."
                )
            return {"state": "error", "detail": detail}
        # The receiver started on the saved token. Whether it is RECEIVING is its session's to say.
        if self._inbound_stopped:
            return {"state": "error", "detail": f"Inbound STOPPED — {self._inbound_stopped}"}
        gateway = self._gateway
        if gateway is not None and gateway.fatal_close_code is not None:
            refusal = _REFUSALS[gateway.fatal_close_code]
            return {"state": "error", "detail": f"Inbound STOPPED — {refusal}"}
        if gateway is not None and gateway.last_drop:
            return {
                "state": "error",
                "detail": (
                    f"Inbound NOT RECEIVING — {gateway.last_drop}, and the gateway session is "
                    "reconnecting."
                ),
            }
        return {"state": "ready", "detail": "Bot token configured"}

    async def test(self) -> dict[str, Any]:
        """The Channels-page Test action: the live "gateway hello" probe (T4.4).

        ``GET /gateway/bot`` is the cheapest call that proves BOTH halves at once —
        the token authenticates AND a gateway session is available (it returns the
        remaining session-start budget, which is what actually stops a bot from
        connecting once it's exhausted)."""
        token = self._token()
        if not token:
            return {"ok": False, "detail": "No bot token configured"}
        api = HTTPDiscordAPI(token)
        try:
            info = await api.get_gateway_bot()
            limit = info.get("session_start_limit") or {}
            remaining = limit.get("remaining")
            detail = f"Gateway reachable at {info.get('url', '?')}"
            if remaining is not None:
                detail += f" ({remaining} session starts remaining)"
        except Exception as exc:
            return {"ok": False, "detail": f"GET /gateway/bot failed: {exc}"}
        finally:
            await api.close()
        # The channel contract: test() is not ok whenever health() is not ready. Derived from
        # health() rather than re-decided, so the two cannot drift.
        health = await self.health()
        if health["state"] != "ready":
            return {"ok": False, "detail": f"{detail}, but {health['detail']}"}
        return {"ok": True, "detail": detail}


def create_provider(config: dict[str, Any] | None = None) -> "DiscordTransport":
    return DiscordTransport(config)
