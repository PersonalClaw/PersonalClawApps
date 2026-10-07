"""Discord trigger source — fires automation events for Discord messages.

Uses the shared receiver pattern: subscribes to inbound_tap and shares
the gateway connection with the channel capability via the app receiver.
"""

from __future__ import annotations

import logging

from . import settings as app_settings
from . import inbound_tap
from . import receiver as app_receiver

logger = logging.getLogger(__name__)

APP_NAME = "discord"

TRIGGER_EVENTS = [
    f"app:{APP_NAME}:direct_message",
    f"app:{APP_NAME}:guild_message",
]


class DiscordTriggerSource:
    """Trigger source that fires automations on Discord messages."""

    def __init__(self, account_api, capability_api) -> None:
        self._account_api = account_api
        self._capability_api = capability_api
        self._started = False

    async def start(self) -> None:
        """Start the trigger source by subscribing to the app receiver."""
        if self._started:
            return
        self._started = True
        inbound_tap.subscribe(self._on_inbound)
        logger.info("Discord trigger source started")

    async def stop(self) -> None:
        """Stop the trigger source."""
        if not self._started:
            return
        self._started = False
        inbound_tap.unsubscribe(self._on_inbound)
        logger.info("Discord trigger source stopped")

    async def _on_inbound(self, event: dict) -> None:
        """Forward inbound events to the tap for automation firing."""
        await inbound_tap.publish(event)

    def get_events(self) -> list[str]:
        return TRIGGER_EVENTS.copy()

    def is_started(self) -> bool:
        return self._started


async def create_provider(account_api, capability_api) -> DiscordTriggerSource:
    """Factory for the trigger source."""
    return DiscordTriggerSource(account_api, capability_api)
