"""Slack channel transport."""

import logging
from typing import Any, Dict, Optional

from personalclaw.sdk.account import AccountCredentials
from personalclaw.sdk.provider import ProviderSettings
from slack_runtime.settings import APP

logger = logging.getLogger(__name__)


class SlackTransport:
    """Transport for Slack channel."""
    
    def __init__(self):
        self._client = None
        self._connected = False
    
    def connect(self) -> bool:
        """Connect to Slack using Socket Mode."""
        tokens = AccountCredentials.get_credentials("slack", ["bot_token", "app_token"])
        
        if not tokens.get("bot_token"):
            logger.error("No bot token available")
            return False
        
        if not tokens.get("app_token"):
            logger.error("No app token available")
            return False
        
        try:
            # Initialize Slack client
            from slack_sdk.web import WebClient
            from slack_sdk.socket_mode import SocketModeClient
            
            self._web_client = WebClient(token=tokens["bot_token"])
            self._socket_client = SocketModeClient(
                app_token=tokens["app_token"],
                websocket_endpoint="wss://socket-mode.slack.com/"
            )
            
            # Set up handlers
            self._socket_client.socket_mode_request_listeners.append(
                self._handle_socket_mode_request
            )
            
            # Start connection
            self._socket_client.start()
            self._connected = True
            
            logger.info("Connected to Slack via Socket Mode")
            return True
            
        except Exception as e:
            logger.error(f"Failed to connect: {e}")
            return False
    
    def disconnect(self) -> None:
        """Disconnect from Slack."""
        if self._socket_client:
            self._socket_client.close()
            self._connected = False
    
    def _handle_socket_mode_request(self, ws, req):
        """Handle Socket Mode request."""
        pass
    
    def send_message(self, channel: str, text: str) -> bool:
        """Send a message to a Slack channel."""
        if not self._connected:
            logger.error("Not connected to Slack")
            return False
        
        try:
            self._web_client.chat_postMessage(channel=channel, text=text)
            return True
        except Exception as e:
            logger.error(f"Failed to send message: {e}")
            return False
    
    def is_connected(self) -> bool:
        """Check if connected."""
        return self._connected
