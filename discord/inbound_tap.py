"""App-level inbound tap for Discord events.

The transport publishes inbound events here after core's guarded door
has returned allowed.  The trigger source subscribes to this tap to
fire automations independently of the channel capability.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_subscribers: list[Callable] = []


def subscribe(handler: Callable) -> None:
    _subscribers.append(handler)


def unsubscribe(handler: Callable) -> None:
    if handler in _subscribers:
        _subscribers.remove(handler)


async def publish(event: dict) -> None:
    """Publish an inbound event to all subscribers."""
    for handler in _subscribers:
        try:
            await handler(event)
        except Exception:
            logger.exception("Error in inbound tap subscriber")


def has_subscribers() -> bool:
    return len(_subscribers) > 0
