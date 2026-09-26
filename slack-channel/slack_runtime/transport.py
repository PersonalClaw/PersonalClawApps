"""SlackTransport — the ChannelTransportProvider that owns the Slack channel.

Outbound + health/test are always available (token-gated). Inbound is driven by
:meth:`start_inbound`, which the gateway calls once at boot with a
:class:`~personalclaw.gateway_services.GatewayServices` handle: the transport
builds a :class:`SlackRuntime`, wires the Socket-Mode receiver + interactive
handlers (which live in this bundle), and connects — with the same
retry/degrade-gracefully behavior the gateway used to inline.
"""

from __future__ import annotations

import asyncio
import logging
import sys as _sys
from pathlib import Path as _Path
from typing import Any

# The app loader only keeps this app's dir on sys.path while it execs the entry
# module. The Slack integration is a multi-module package whose modules import
# each other (some lazily, to break cycles) throughout the process lifetime — the
# socket receiver, delivery, and interaction handlers all resolve ``slack_runtime.*``
# long after boot. Pin the app dir on sys.path for the life of the process so those
# imports keep resolving (a real installed package would be permanently importable).
_APP_DIR = str(_Path(__file__).resolve().parents[1])
if _APP_DIR not in _sys.path:
    _sys.path.insert(0, _APP_DIR)

from personalclaw.sdk.channel import (
    ChannelCapabilities,
    ChannelTransportProvider,
    OutboundMessage,
    fence_channel_content,
)

# Import ALL runtime deps at MODULE level (not lazily in start_inbound): the app
# loader only keeps this app's dir on sys.path while it execs this module, so a
# ``from slack_runtime.X import`` inside a method runs LATER — when the dir is off
# the path — and fails with "No module named 'slack_runtime'". Binding them here,
# during exec, captures them for the life of the transport instance.
from slack_runtime.client import RealSlackClient
from slack_runtime.delivery import SlackDelivery
from slack_runtime.events import SeenCache, init_socket_mode
from slack_runtime.interactions import init as init_interactions
from slack_runtime.runtime import SlackRuntime
from slack_runtime.settings import LiveConfig, load_tokens
from slack_runtime.writes import SendRefused, live_writes_disabled

# NOT ``__name__``: the app loader execs this ENTRY module under a synthetic name
# (``_pclaw_app_slack_channel__slack_runtime_transport``), so ``__name__`` produced a
# logger outside the ``slack_runtime`` root this app declares in ``app.json``
# ``loggerRoots`` — the level the operator sets never reached the transport, so its
# diagnostics were unreachable at ANY verbosity. #952 called the offline notice "an INFO
# line invisible at the default WARNING log level"; measured, it was invisible at DEBUG
# too. Every other module in this bundle is imported as ``slack_runtime.X`` and is fine.
logger = logging.getLogger("slack_runtime.transport")


def fence_untrusted_inbound(text: str, sender_id: str, *, trusted: bool) -> str:
    """Fence untrusted NON-OWNER inbound content as DATA before it reaches the agent.

    CHANNEL-EXPANSION T1.4 / CE-6 — the channel conformance kit's ``[fencing]`` clause.
    A non-owner's chat text is untrusted input, never instructions: on Slack's direct
    inbound path (``handler.handle_message``) it MUST reach the model wrapped in the
    platform's untrusted-content fence — the SAME fence core's guarded door hands the
    sibling channels as ``verdict.fenced_text``
    (``fence_channel_content(text, provider, sender)``). It is applied HERE, at the
    transport that owns Slack's inbound path, so the handler cannot forget it — and so
    the fence a channel produces has a consumer this bundle can point to.

    A *trusted* sender passes through unfenced — the owner, an explicitly-allowlisted
    user, or a trusted bot — exactly as core exempts ``is_allowed_sender``: fencing the
    owner's own request would make the agent read it as inert data it must not act on.
    Mirrors core's ``verdict.fenced_text or msg.text``: the fenced form for an untrusted
    sender, the raw text otherwise (an empty message is returned unchanged — nothing to
    fence). The caller decides trust; keeping that decision out of here leaves this a
    pure function of ``(text, sender_id, trusted)``.
    """
    if trusted or not text:
        return text
    fenced_text = fence_channel_content(text, "slack", sender_id)
    return fenced_text


class SlackTransport(ChannelTransportProvider):
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        # The config, kept live: every token read goes through it, so a Configure → Save is seen
        # by the next Test/Connect/health/send rather than at the next restart. ``start_inbound``
        # hands the same config to the runtime, so the INBOUND half resolves its tokens from the
        # same place the outbound half does (#952).
        self._config = LiveConfig(config if config is not None else {})
        self._runtime: SlackRuntime | None = None
        #: The ``(bot, app)`` tokens the gateway drove the receiver with at boot — ``None`` until
        #: it did. Socket Mode keeps the tokens it started with, so this is what tells ``health``
        #: that tokens saved since have not reached inbound.
        self._inbound_tokens: tuple[str, str] | None = None
        #: True once the Socket-Mode receiver is actually connected. ``health()`` needs the
        #: three-way distinction — connected / tried-and-failed / never driven — because a
        #: config save re-cycles this provider and builds a FRESH transport that the
        #: gateway does not re-drive, so "never driven" is a real, reportable state and not
        #: just a boot-time blink.
        self._inbound_started: bool = False
        #: Why inbound is not running, when it was driven and failed (``""`` otherwise).
        #: ``health()``/``test()`` report it, so the provider row can no longer show a
        #: green "Tokens configured" over a receiver that never started (#952).
        self._inbound_offline_reason: str = ""

    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            inbound=True, threads=True, attachments=True, reactions=True,
            edits=True, rich_text=True, typing_indicator=True, max_text_len=40000,
        )

    @property
    def name(self) -> str:
        return "slack"

    @property
    def display_name(self) -> str:
        return "Slack"

    def _tokens(self) -> tuple[str, str]:
        """``(bot_token, app_token)`` as configured now."""
        return load_tokens(self._config.current())

    async def connect(self) -> bool:
        return bool(self._tokens()[0])

    async def disconnect(self) -> None:
        return None

    # ── Inbound: the gateway drives this once at boot ──
    async def start_inbound(self, services: Any) -> None:
        """Build the Slack runtime, wire the socket receiver, connect (retry/degrade)."""
        # Pass this transport's own config through: without it the runtime re-derived the
        # tokens from core's credential store alone and a dashboard-configured install
        # never started inbound (#952).
        runtime = SlackRuntime(services, config=self._config.current())
        self._inbound_tokens = (runtime._bot_token, runtime._app_token)
        if not runtime._slack_enabled:
            missing = " and ".join(
                n for n, tok in (("Bot Token", runtime._bot_token), ("App Token", runtime._app_token))
                if not tok
            )
            self._inbound_offline_reason = (
                f"no {missing} — Socket Mode needs both. Set them in "
                "Settings → Providers → Slack Channel."
            )
            # WARNING, not INFO: this is the whole reason a correctly-credentialled-looking
            # Slack install answers nothing, and it was previously below the default level.
            logger.warning("SlackTransport: inbound stays offline — %s", self._inbound_offline_reason)
            return
        runtime.slack = RealSlackClient(runtime._bot_token)
        self._runtime = runtime

        init_interactions(runtime)
        init_socket_mode(runtime, SeenCache())

        if runtime._socket_client is None:
            # enterprise validation failed inside init_socket_mode (which logs the why)
            # Not "…or add the org to Allowed Enterprise IDs": that list no longer gates
            # acceptance (see validate_enterprise), so auth.test is the only thing that can
            # have failed here. Pointing at an inert setting is worse than saying nothing.
            self._inbound_offline_reason = (
                "Slack workspace validation failed — auth.test rejected the Bot Token. "
                "Re-check it in Settings → Providers → Slack Channel."
            )
            return

        # Register outbound delivery on the gateway + the dashboard. Core delivers
        # through this ONE provider-agnostic ChannelDelivery handle (text, attachments,
        # streaming, identity lookups, approvals) — it never sees the Slack client.
        delivery = SlackDelivery(runtime.slack, runtime._owner_id)
        if hasattr(services, "register_channel_delivery"):
            services.register_channel_delivery(delivery)
        if getattr(services, "dashboard_state", None) is not None:
            services.dashboard_state.channel_delivery = delivery

        # Register this channel's observe-mode channels on the core history buffer
        # (per-channel activation is APP config; channel_history stays generic).
        try:
            from slack_runtime.settings import ACTIVATION_OBSERVE, get_settings

            _ch_hist = getattr(services, "channel_history", None)
            if _ch_hist is not None:
                for _cid, _ccfg in get_settings().channels.items():
                    if _ccfg.activation == ACTIVATION_OBSERVE:
                        _ch_hist.set_observe(_cid)
        except Exception:
            logger.debug("observe-channel registration failed", exc_info=True)

        for attempt in range(1, 4):
            try:
                await runtime._socket_client.connect()
                logger.info("SlackTransport: Socket-Mode connected")
                self._inbound_offline_reason = ""
                self._inbound_started = True
                return
            except Exception as e:  # noqa: BLE001 — resilience: never crash the gateway
                if attempt < 3:
                    logger.warning("Slack Socket-Mode connect failed (%s/3): %s — retrying", attempt, e)
                    await asyncio.sleep(2 * attempt)
                else:
                    logger.error(
                        "Slack Socket-Mode connect failed after 3 attempts (%s) — "
                        "Slack offline; the rest of the gateway is unaffected.", e,
                    )
                    runtime._slack_enabled = False
                    self._inbound_offline_reason = f"Socket-Mode connect failed after 3 attempts: {e}"

    async def stop_inbound(self) -> None:
        rt = self._runtime
        if rt is not None and rt._socket_client is not None:
            try:
                await asyncio.wait_for(rt._socket_client.close(), timeout=1.0)
            except Exception:
                logger.debug("SlackTransport: socket close timed out", exc_info=True)

    async def send(self, message: OutboundMessage) -> bool | SendRefused:
        """Transmit one outbound message. ``True`` delivered, ``False`` failed, or a
        :class:`SendRefused` when the platform's live-writes kill switch is on.

        The refusal is checked AFTER the token gate on purpose: an unconfigured
        transport could not have written anything, so reporting "refused" there would
        claim the guard suppressed a write that was never possible. Only a transport
        that WOULD have transmitted reports a refusal.
        """
        bot_token, _ = self._tokens()
        if not bot_token:
            return False
        # DISABLE_LIVE_WRITES (§1.4). A chat.postMessage is a live, outward,
        # instantly-human-visible write — the same class core refuses for non-GET egress
        # and model deletion. Typed refusal, never a silent no-op: a test (or an
        # operator) asserting a send must be able to see that the guard, not the
        # network, stopped it. Checked BEFORE the client is resolved so a suppressed
        # send opens no connection at all.
        if live_writes_disabled():
            # ``self.name`` rather than a new module constant: this bundle already has
            # exactly one source of truth for the channel's name, and a second spelling
            # of "slack" is a drift seam for no gain.
            refusal = SendRefused(channel=self.name, target=message.channel_id)
            logger.warning("SlackTransport.send refused: %s", refusal)
            return refusal
        try:
            # The receiver's client carries the bot token inbound started with; reuse it unless
            # the configured token has moved on since, so a rotated token is what sends.
            reuse = (
                self._runtime is not None
                and self._runtime.slack is not None
                and (self._inbound_tokens is None or self._inbound_tokens[0] == bot_token)
            )
            client = self._runtime.slack if reuse else RealSlackClient(bot_token)  # type: ignore[union-attr]
            await client.post_message(
                channel=message.channel_id,
                text=message.text,
                thread_ts=message.thread_id or None,
            )
            return True
        except Exception as e:
            logger.warning("SlackTransport.send failed: %s", e)
            return False

    @property
    def connected(self) -> bool:
        return bool(self._tokens()[0])

    async def health(self) -> dict[str, Any]:
        """Readiness for the Channels page / provider row.

        Reports the INBOUND half too (#952). It used to answer "ready — Tokens configured"
        off the bot token alone, which is exactly true and exactly useless: the two signals
        an operator trusts (a green provider row and "connected to Slack") both described
        outbound while the receiver was dead. ``error`` rather than ``offline`` because
        outbound genuinely works — the channel is half-up, not down.

        Tokens are read as configured NOW, and the receiver is compared against the tokens it
        was started with: saved tokens reach outbound at once but inbound only at the next
        start, and a boot-time reason ("no Bot Token") must not outlive the token it was about.
        """
        tokens = self._tokens()
        if not tokens[0]:
            return {"state": "offline", "detail": "No bot token configured"}
        if self._inbound_tokens is not None and self._inbound_tokens != tokens:
            if self._inbound_started:
                detail = (
                    "Outbound uses the saved tokens; Socket Mode is still connected with the "
                    "ones it started with. Restart the gateway to move inbound onto the saved "
                    "tokens."
                )
            else:
                detail = (
                    "Outbound ready, inbound OFFLINE — the Socket-Mode receiver starts with the "
                    "gateway, so the tokens saved since then take effect on the next restart."
                )
            return {"state": "error", "detail": detail}
        if self._inbound_started:
            return {"state": "ready", "detail": "Tokens configured, Socket-Mode connected"}
        if self._inbound_offline_reason:
            return {
                "state": "error",
                "detail": f"Outbound ready, inbound OFFLINE — {self._inbound_offline_reason}",
            }
        return {
            "state": "error",
            "detail": (
                "Outbound ready, inbound NOT STARTED — the gateway drives the Socket-Mode "
                "receiver once at boot, so saved tokens take effect on the next restart."
            ),
        }

    async def test(self) -> dict[str, Any]:
        bot_token, _ = self._tokens()
        if not bot_token:
            return {"ok": False, "detail": "No bot token configured"}
        try:
            client = RealSlackClient(bot_token)
            res = await client.auth_test()
            team = (res or {}).get("team") or (res or {}).get("team_id") or "workspace"
        except Exception as e:
            return {"ok": False, "detail": f"auth.test failed: {e}"}
        # Agree with health(): the channel contract requires test() to be not-ok whenever
        # health() is not "ready", or the owner gets a green Test on a channel that cannot
        # hear them. Derived from health() rather than re-deciding, so the two cannot drift.
        h = await self.health()
        if h["state"] != "ready":
            return {"ok": False, "detail": f"Authenticated to {team}, but {h['detail']}"}
        return {"ok": True, "detail": f"Authenticated to {team}"}


def create_provider(config: dict[str, Any] | None = None) -> "SlackTransport":
    return SlackTransport(config)
