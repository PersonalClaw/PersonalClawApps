"""Slack interaction handlers."""

from typing import Any, Dict, List, Optional

from personalclaw.sdk.provider import ProviderSettings
from slack_runtime.settings import APP

APP_NAME = "slack"


def get_allowed_users() -> List[str]:
    """Get list of allowed users."""
    settings = ProviderSettings.load(APP)
    return settings.get("allowed_users", [])


def add_allowed_user(user_id: str) -> None:
    """Add user to allowed list."""
    settings = ProviderSettings.load(APP)
    users = settings.get("allowed_users", [])
    if user_id not in users:
        users.append(user_id)
        settings["allowed_users"] = users
        ProviderSettings.update(APP, settings)


def remove_allowed_user(user_id: str) -> None:
    """Remove user from allowed list."""
    settings = ProviderSettings.load(APP)
    users = settings.get("allowed_users", [])
    if user_id in users:
        users.remove(user_id)
        settings["allowed_users"] = users
        ProviderSettings.update(APP, settings)


def get_tracking_channels() -> List[str]:
    """Get list of tracked channels."""
    settings = ProviderSettings.load(APP)
    return settings.get("tracking_channels", [])


def add_tracking_channel(channel_id: str) -> None:
    """Add channel to tracking list."""
    settings = ProviderSettings.load(APP)
    channels = settings.get("tracking_channels", [])
    if channel_id not in channels:
        channels.append(channel_id)
        settings["tracking_channels"] = channels
        ProviderSettings.update(APP, settings)


def remove_tracking_channel(channel_id: str) -> None:
    """Remove channel from tracking list."""
    settings = ProviderSettings.load(APP)
    channels = settings.get("tracking_channels", [])
    if channel_id in channels:
        channels.remove(channel_id)
        settings["tracking_channels"] = channels
        ProviderSettings.update(APP, settings)
