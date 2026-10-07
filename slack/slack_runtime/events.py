"""Slack event handling."""

import logging
from typing import Any, Dict, Optional

from slack_runtime.inbound_tap import publish
from slack_runtime.settings import APP, get_settings

logger = logging.getLogger(__name__)

EVENT_TYPES = {
    "direct_message": "app:slack:direct_message",
    "channel_message": "app:slack:channel_message"
}


def handle_slack_event(event: Dict[str, Any]) -> bool:
    """Handle incoming Slack event."""
    event_type = event.get("type")
    
    if event_type not in EVENT_TYPES:
        logger.debug(f"Unknown event type: {event_type}")
        return False
    
    # Get settings for admission control
    settings = get_settings()
    
    # Check allowlist
    allowed_users = settings.get("allowed_users", [])
    user = event.get("user", "")
    if allowed_users and user not in allowed_users:
        logger.debug(f"User {user} not in allowed list")
        return False
    
    # Check tracked channels
    tracking_channels = settings.get("tracking_channels", [])
    channel = event.get("channel", "")
    if tracking_channels and channel not in tracking_channels:
        logger.debug(f"Channel {channel} not in tracked channels")
        return False
    
    # Check DM activation
    if event_type == "direct_message":
        dm_activation = settings.get("dm_activation", True)
        if not dm_activation:
            logger.debug("DM activation is disabled")
            return False
    
    # Publish event
    app_event = EVENT_TYPES[event_type]
    logger.debug(f"Publishing event: {app_event}")
    publish(event)
    
    return True
