"""TelegramSettings — the telegram-channel app's OWN config + credential keys.

Telegram behavioral config (the DM activation posture) lives HERE in the app
bundle, persisted in the app's own store (``~/.personalclaw/apps/telegram-channel/
data/config.json`` via :class:`ProviderSettings`), NOT in core ``config.json``.
Core defines no Telegram config.

The bot token is a SECRET, so it lives in the shared credential store (``.env``)
under this app's own key — Telegram is not one of core's in-core credential
exceptions (only ``SLACK_*``/``PERSONALCLAW_OWNER_ID`` are, per
``docs/architecture/provider-boundary.md``), and the credential store reads back
every key by name, so the app owns ``TELEGRAM_BOT_TOKEN`` literally. Who is
allowed to talk (allowlist, pairing) and which groups are tracked are owned by the
core sender-trust seam (``channel_trust``, provider=``"telegram"``), so this app
keeps no allowlist of its own — the whole point of CE-1.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from personalclaw.sdk.channel import ProviderSettings

logger = logging.getLogger(__name__)

_APP = "telegram-channel"

#: The credential-store key the bot token is stored under. App-owned (see module
#: docstring): the setup step writes it and the runtime reads it back by name.
CRED_TELEGRAM_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"

# DM activation modes. "always" answers every paired DM; "mention" only when the
# bot is @-mentioned (rare in a 1:1 DM but honored for parity); "off" disables DMs.
ACTIVATION_ALWAYS = "always"
ACTIVATION_MENTION = "mention"
ACTIVATION_OFF = "off"
_VALID_ACTIVATIONS = frozenset({ACTIVATION_ALWAYS, ACTIVATION_MENTION, ACTIVATION_OFF})


def _validate_activation(value: str) -> str:
    return value if value in _VALID_ACTIVATIONS else ACTIVATION_ALWAYS


@dataclass
class TelegramSettings:
    """The telegram-channel app's behavioral config (its own store)."""

    dm_activation: str = ACTIVATION_ALWAYS

    @classmethod
    def load(cls) -> "TelegramSettings":
        """Read + coerce the app store."""
        d = ProviderSettings.load(_APP)
        return cls(dm_activation=_validate_activation(d.get("dm_activation", ACTIVATION_ALWAYS)))


# One cached live instance, mirroring the Slack app: build once, refresh on write.
_settings: TelegramSettings | None = None


def get_settings() -> TelegramSettings:
    """The app's live settings (cached; refreshed by :func:`reload_settings`)."""
    global _settings
    if _settings is None:
        _settings = TelegramSettings.load()
    return _settings


def reload_settings() -> TelegramSettings:
    """Force a re-read of the app store and refresh the cached instance."""
    global _settings
    _settings = TelegramSettings.load()
    return _settings
