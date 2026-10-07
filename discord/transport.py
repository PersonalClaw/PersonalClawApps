"""Discord transport — runs the gateway and exposes messaging capabilities.

The transport registers itself with the shared receiver so that the agent
can answer when the ``channel`` capability is on.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

from discord_runtime.transport import (
    DiscordTransport as _BaseTransport,
    create_provider as _create_provider,
)
from discord_runtime.gateway import GatewayConnection

from . import settings as app_settings
from . import inbound_tap
from . import receiver as app_receiver

logger = logging.getLogger(__name__)

PROVIDER = app_settings.PROVIDER


class DiscordTransport:
    """Wraps the base Discord transport and manages the shared connection."""

    def __init__(self, account_api, capability_api) -> None:
        self._account_api = account_api
        self._capability_api = capability_api
        self._transport: _BaseTransport | None = None
        self._gateway: GatewayConnection | None = None
        self._ref_count: int = 0
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start or reference-count the shared gateway connection."""
        async with self._lock:
            self._ref_count += 1
            if self._ref_count == 1:
                await self._ensure_running()

    async def stop(self) -> None:
        """Stop the connection when the last capability releases it."""
        async with self._lock:
            self._ref_count = max(0, self._ref_count - 1)
            if self._ref_count == 0:
                await self._shutdown()

    async def _ensure_running(self) -> None:
        """Create the transport and open the gateway connection once."""
        cfg = app_settings.resolve_settings(self._account_api, self._capability_api)
        token = cfg["bot_token"]
        app_id = cfg["application_id"]
        dm_activation = cfg["dm_activation"]

        if not token or not app_id:
            logger.warning("Discord transport: missing bot_token or application_id")
            return

        self._transport = _create_provider(
            bot_token=token,
            application_id=app_id,
            dm_activation=dm_activation,
        )
        self._gateway = GatewayConnection(self._transport)

        # Subscribe the app receiver so inbound events are routed here
        inbound_tap.register_receiver(app_receiver.handle_inbound)

        # Register the transport with the app receiver so the agent can reply
        app_receiver.register_transport(PROVIDER, self._transport)

        self._task = asyncio.create_task(self._gateway.run())
        logger.info("Discord transport started (ref_count=%d)", self._ref_count)

    async def _shutdown(self) -> None:
        """Close the gateway and clean up."""
        if self._gateway:
            await self._gateway.close()
            self._gateway = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        inbound_tap.unregister_receiver(app_receiver.handle_inbound)
        app_receiver.unregister_transport(PROVIDER)
        self._transport = None
        logger.info("Discord transport stopped (ref_count=0)")

    async def send(self, channel_id: str, message: str, **kwargs: Any) -> dict:
        """Send a message through the transport."""
        if self._transport is None:
            raise RuntimeError("Discord transport not started")
        return await self._transport.send(channel_id, message, **kwargs)

    async def react(self, message_id: str, emoji: str, **kwargs: Any) -> None:
        """React to a message."""
        if self._transport is None:
            raise RuntimeError("Discord transport not started")
        return await self._transport.react(message_id, emoji, **kwargs)

    async def create_thread(self, channel_id: str, name: str, **kwargs: Any) -> dict:
        """Create a thread in a channel."""
        if self._transport is None:
            raise RuntimeError("Discord transport not started")
        return await self._transport.create_thread(channel_id, name, **kwargs)

    async def list_channels(self, guild_id: str) -> list[dict]:
        """List channels in a guild."""
        if self._transport is None:
            raise RuntimeError("Discord transport not started")
        return await self._transport.list_channels(guild_id)


async def create_provider(account_api, capability_api) -> DiscordTransport:
    """Factory that returns a configured transport instance."""
    return DiscordTransport(account_api, capability_api)
