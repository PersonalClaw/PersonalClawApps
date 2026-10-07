"""App-level receiver that owns the shared gateway connection.

Both the ``channel`` and ``triggers`` capabilities use this receiver.
The gateway is kept alive as long as either capability is active.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Maps provider name -> transport instance
_transports: dict[str, Any] = {}

# Registered inbound event handler
_inbound_handler: Optional[Callable] = None


def register_transport(provider: str, transport: Any) -> None:
    _transports[provider] = transport


def unregister_transport(provider: str) -> None:
    _transports.pop(provider, None)


def get_transport(provider: str) -> Optional[Any]:
    return _transports.get(provider)


def register_receiver(handler: Callable) -> None:
    global _inbound_handler
    _inbound_handler = handler


def unregister_receiver(handler: Callable) -> None:
    global _inbound_handler
    if _inbound_handler is handler:
        _inbound_handler = None


async def handle_inbound(event: dict) -> None:
    """Route inbound Discord events to registered transports and the tap.

    If a transport is registered for the event's provider, forward the event
    there so the agent can respond.  Also push the event to the app-level
    inbound tap so the trigger source can fire automations.
    """
    if _inbound_handler is not None:
        await _inbound_handler(event)

    provider = event.get("provider", "")
    transport = get_transport(provider)
    if transport is not None:
        # Forward to transport for agent reply
        await transport._handle_inbound(event)  # type: ignore[attr-defined]
