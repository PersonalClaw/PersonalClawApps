"""Inbound event tap for Slack."""

import logging
from typing import Any, Callable, Dict, List

logger = logging.getLogger(__name__)

_subscribers: List[Callable[[Dict[str, Any]], None]] = []


def subscribe(callback: Callable[[Dict[str, Any]], None]) -> None:
    """Subscribe to inbound events."""
    _subscribers.append(callback)
    logger.debug(f"Added subscriber, total: {len(_subscribers)}")


def unsubscribe(callback: Callable[[Dict[str, Any]], None]) -> None:
    """Unsubscribe from inbound events."""
    if callback in _subscribers:
        _subscribers.remove(callback)
        logger.debug(f"Removed subscriber, total: {len(_subscribers)}")


def publish(event: Dict[str, Any]) -> None:
    """Publish an event to all subscribers."""
    for callback in _subscribers:
        try:
            callback(event)
        except Exception as e:
            logger.error(f"Error in subscriber callback: {e}")
