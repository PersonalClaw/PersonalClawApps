"""DiscordSettings — the discord-channel app's OWN config + credential keys.

Discord behavioral config (the DM activation posture, the application id) lives
HERE in the app bundle, persisted in the app's own store
(``~/.personalclaw/apps/discord-channel/data/config.json`` via
:class:`ProviderSettings`), NOT in core ``config.json``. Core defines no Discord
config.

The bot token is a SECRET, so it lives in the shared credential store (``.env``)
under this app's own key — Discord is not one of core's in-core credential
exceptions (only ``SLACK_*``/``PERSONALCLAW_OWNER_ID`` are, per
``docs/architecture/provider-boundary.md``), and the credential store reads back
every key by name, so the app owns ``DISCORD_BOT_TOKEN`` literally.

The **application id** is deliberately NOT a credential: Discord prints it on the
public "General Information" page, it appears in every invite URL a user clicks,
and leaking it grants nothing (the token is what authenticates). Putting it in the
credential store would claim a secrecy it does not have and hide it from the
Configure form; it belongs in the app store next to ``dm_activation`` where the
user can see and edit it. The runtime needs it for the OAuth2 invite URL the setup
step prints; interaction responses are addressed by interaction id + token, which
arrive on the ``INTERACTION_CREATE`` payload itself, so nothing on the hot path
depends on it being set.

Who is allowed to talk (allowlist, pairing) and which guild channels are tracked
are owned by the core sender-trust seam (``channel_trust``, provider=``"discord"``),
so this app keeps no allowlist of its own — the whole point of CE-1.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from personalclaw.sdk.channel import ProviderSettings

logger = logging.getLogger(__name__)

_APP = "discord-channel"

#: The credential-store key the bot token is stored under. App-owned (see module
#: docstring): the setup step writes it and the runtime reads it back by name.
CRED_DISCORD_BOT_TOKEN = "DISCORD_BOT_TOKEN"

# DM activation modes. "always" answers every paired DM; "mention" only when the
# bot is @-mentioned (rare in a 1:1 DM but honored for parity); "off" disables DMs.
ACTIVATION_ALWAYS = "always"
ACTIVATION_MENTION = "mention"
ACTIVATION_OFF = "off"
_VALID_ACTIVATIONS = frozenset({ACTIVATION_ALWAYS, ACTIVATION_MENTION, ACTIVATION_OFF})


def _validate_activation(value: str) -> str:
    return value if value in _VALID_ACTIVATIONS else ACTIVATION_ALWAYS


@dataclass
class DiscordSettings:
    """The discord-channel app's behavioral config (its own store)."""

    dm_activation: str = ACTIVATION_ALWAYS
    application_id: str = ""

    @classmethod
    def load(cls) -> "DiscordSettings":
        """Read + coerce the app store."""
        d = ProviderSettings.load(_APP)
        return cls(
            dm_activation=_validate_activation(d.get("dm_activation", ACTIVATION_ALWAYS)),
            application_id=str(d.get("application_id", "") or ""),
        )


# One cached live instance, mirroring the Telegram + Slack apps: build once, refresh
# on write.
_settings: DiscordSettings | None = None


def get_settings() -> DiscordSettings:
    """The app's live settings (cached; refreshed by :func:`reload_settings`)."""
    global _settings
    if _settings is None:
        _settings = DiscordSettings.load()
    return _settings


def reload_settings() -> DiscordSettings:
    """Force a re-read of the app store and refresh the cached instance."""
    global _settings
    _settings = DiscordSettings.load()
    return _settings
