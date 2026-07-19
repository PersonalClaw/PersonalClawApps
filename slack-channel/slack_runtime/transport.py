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

logger = logging.getLogger(__name__)


class SlackTransport(ChannelTransportProvider):
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        import os

        # Per-instance config wins; else the shared credential store the gateway
        # propagates into the environment (SLACK_BOT_TOKEN / SLACK_APP_TOKEN).
        self._bot_token = cfg.get("bot_token", "") or os.environ.get("SLACK_BOT_TOKEN", "")
        self._app_token = cfg.get("app_token", "") or os.environ.get("SLACK_APP_TOKEN", "")
        self._runtime: SlackRuntime | None = None

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

    async def connect(self) -> bool:
        return bool(self._bot_token)

    async def disconnect(self) -> None:
        return None

    # ── Inbound: the gateway drives this once at boot ──
    async def start_inbound(self, services: Any) -> None:
        """Build the Slack runtime, wire the socket receiver, connect (retry/degrade)."""
        runtime = SlackRuntime(services)
        if not runtime._slack_enabled:
            logger.info("SlackTransport: no tokens — inbound stays offline")
            return
        runtime.slack = RealSlackClient(runtime._bot_token)
        self._runtime = runtime

        init_interactions(runtime)
        init_socket_mode(runtime, SeenCache())

        if runtime._socket_client is None:
            return  # enterprise validation failed inside init_socket_mode

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

    async def stop_inbound(self) -> None:
        rt = self._runtime
        if rt is not None and rt._socket_client is not None:
            try:
                await asyncio.wait_for(rt._socket_client.close(), timeout=1.0)
            except Exception:
                logger.debug("SlackTransport: socket close timed out", exc_info=True)

    async def send(self, message: OutboundMessage) -> bool:
        if not self._bot_token:
            return False
        try:
            client = self._runtime.slack if self._runtime and self._runtime.slack else RealSlackClient(self._bot_token)
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
        return bool(self._bot_token)

    async def health(self) -> dict[str, Any]:
        if not self._bot_token:
            return {"state": "offline", "detail": "No bot token configured"}
        return {"state": "ready", "detail": "Tokens configured"}

    async def test(self) -> dict[str, Any]:
        if not self._bot_token:
            return {"ok": False, "detail": "No bot token configured"}
        try:
            client = RealSlackClient(self._bot_token)
            res = await client.auth_test()
            team = (res or {}).get("team") or (res or {}).get("team_id") or "workspace"
            return {"ok": True, "detail": f"Authenticated to {team}"}
        except Exception as e:
            return {"ok": False, "detail": f"auth.test failed: {e}"}


def create_provider(config: dict[str, Any] | None = None) -> "SlackTransport":
    return SlackTransport(config)
