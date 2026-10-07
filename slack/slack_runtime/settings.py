"""Settings management for the Slack app."""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from personalclaw.sdk.provider import ProviderSettings
from personalclaw.sdk.account import AccountCredentials

APP = "slack"
_APP = "slack"

DATA_DIR = Path(__file__).parent.parent / "data"
CONFIG_FILE = DATA_DIR / "config.json"
MIGRATION_MARKER = DATA_DIR / ".core_migration_done"


class SlackSettings:
    """Settings container for Slack app."""
    
    def __init__(self):
        self._settings = None
    
    def load(self) -> Dict[str, Any]:
        """Load settings from provider settings."""
        if self._settings is None:
            self._settings = ProviderSettings.load(APP)
        return self._settings


def get_settings() -> Dict[str, Any]:
    """Get current Slack settings."""
    return ProviderSettings.load(APP)


def persist_setting(key: str, value: Any) -> None:
    """Persist a single setting."""
    settings = get_settings()
    settings[key] = value
    ProviderSettings.update(APP, settings)


def load_tokens() -> Dict[str, str]:
    """Load Slack tokens from account credentials."""
    return AccountCredentials.get_credentials("slack", ["bot_token", "app_token"])


class LiveConfig:
    """Live configuration manager."""
    
    def __init__(self):
        self._config = None
    
    def get(self, key: str, default: Any = None) -> Any:
        """Get a config value."""
        if self._config is None:
            self._config = ProviderSettings.load(APP)
        return self._config.get(key, default)
    
    def set(self, key: str, value: Any) -> None:
        """Set a config value."""
        settings = self._config or ProviderSettings.load(APP)
        settings[key] = value
        ProviderSettings.update(APP, settings)
        self._config = settings


def migrate_from_core() -> bool:
    """Migrate settings from legacy core config.
    
    Returns True if migration was performed, False otherwise.
    """
    if MIGRATION_MARKER.exists():
        return False
    
    core_config_path = Path.home() / ".personalclaw" / "config.json"
    if not core_config_path.exists():
        return False
    
    try:
        with open(core_config_path) as f:
            core_config = json.load(f)
        
        slack_block = core_config.get("slack")
        if not slack_block:
            return False
        
        # Migrate settings to new format
        settings = {}
        for key in ["bot_token", "app_token", "allowed_users", "tracking_channels",
                    "command", "trusted_bot_ids", "reactions", "reactions_enabled",
                    "channels", "dm_activation"]:
            if key in slack_block:
                settings[key] = slack_block[key]
        
        if settings:
            ProviderSettings.update(APP, settings)
            MIGRATION_MARKER.touch()
            return True
    except Exception:
        pass
    
    return False


def persist_list_entry(key: str, entry: str, append: bool = True) -> None:
    """Persist an entry to a list setting."""
    settings = get_settings()
    current = settings.get(key, [])
    if isinstance(current, str):
        current = [current]
    
    if append and entry not in current:
        current.append(entry)
    elif not append:
        current = [entry]
    
    settings[key] = current
    ProviderSettings.update(APP, settings)


def remove_list_entry(key: str, entry: str) -> None:
    """Remove an entry from a list setting."""
    settings = get_settings()
    current = settings.get(key, [])
    if entry in current:
        current.remove(entry)
        settings[key] = current
        ProviderSettings.update(APP, settings)
