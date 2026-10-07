"""Trigger source for Slack events."""

import logging
from typing import Any, Dict, List, Optional

from personalclaw.sdk.provider import ProviderSettings
from slack_runtime.settings import APP

logger = logging.getLogger(__name__)

APP_NAME = "slack"

# Event type mappings
EVENT_TYPES = {
    "direct_message": "app:slack:direct_message",
    "channel_message": "app:slack:channel_message"
}


class SlackTriggerSource:
    """Trigger source for Slack messages."""
    
    def __init__(self):
        self._enabled = False
        self._events: List[Dict[str, Any]] = []
    
    def start(self) -> None:
        """Start the trigger source."""
        logger.info("Starting Slack trigger source")
        self._enabled = True
        
        # Subscribe to inbound events
        from slack_runtime.inbound_tap import subscribe
        subscribe(self._handle_event)
    
    def stop(self) -> None:
        """Stop the trigger source."""
        logger.info("Stopping Slack trigger source")
        self._enabled = False
    
    def _handle_event(self, event: Dict[str, Any]) -> None:
        """Handle incoming event."""
        if not self._enabled:
            return
        
        event_type = event.get("type")
        app_event = EVENT_TYPES.get(event_type)
        
        if app_event:
            logger.debug(f"Trigger event: {app_event}")
            self._events.append({
                "type": app_event,
                "data": event
            })
    
    def get_events(self) -> List[Dict[str, Any]]:
        """Get pending events."""
        events = self._events
        self._events = []
        return events
    
    def is_enabled(self) -> bool:
        """Check if trigger source is enabled."""
        return self._enabled
