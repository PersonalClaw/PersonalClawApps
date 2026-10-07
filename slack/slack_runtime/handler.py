"""Handler for Slack channel interactions."""

import logging
from typing import Any, Dict, Optional

from personalclaw.sdk.provider import ProviderSettings
from personalclaw.sdk.account import AccountCredentials
from slack_runtime.settings import APP

logger = logging.getLogger(__name__)

APP_NAME = "slack"


def pre_tool_hook(context: Dict[str, Any]) -> Dict[str, Any]:
    """Pre-tool hook for Slack interactions."""
    # Load settings from account and capability
    tokens = AccountCredentials.get_credentials("slack", ["bot_token", "app_token"])
    settings = ProviderSettings.load(APP)
    
    # Merge into context
    context["slack_tokens"] = tokens
    context["slack_settings"] = settings
    
    return context


def _persist_channel_config(config: Dict[str, Any]) -> None:
    """Persist channel configuration to provider settings."""
    current = ProviderSettings.load(APP)
    current.update(config)
    ProviderSettings.update(APP, current)


def handle_message(message: Dict[str, Any], context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Handle incoming Slack message."""
    logger.info(f"Handling message from {message.get('user', 'unknown')}")
    
    # Validate against settings
    allowed_users = context.get("slack_settings", {}).get("allowed_users", [])
    if allowed_users and message.get("user") not in allowed_users:
        logger.warning(f"User {message.get('user')} not in allowed list")
        return None
    
    # Process message
    return {
        "response": "Message processed",
        "user": message.get("user"),
        "channel": message.get("channel")
    }
