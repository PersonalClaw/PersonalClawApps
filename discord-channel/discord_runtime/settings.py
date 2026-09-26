"""DiscordSettings — the discord-channel app's OWN config + credential keys.

Discord behavioral config (the DM activation posture, the application id) lives
HERE in the app bundle, persisted in the app's own store
(``~/.personalclaw/apps/discord-channel/data/config.json`` via
:class:`ProviderSettings`), NOT in core ``config.json``. Core defines no Discord
config.

The bot token is a SECRET. It is the app store's ``bot_token`` setting, declared
``x-meta.sensitive``: setup and the Configure form both write it through
:class:`ProviderSettings`, which keeps the value in the credential store under a key
this app owns and puts only a reference in the settings file, so uninstalling the app
removes it. :func:`load_bot_token` is the one place it is read.

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
import os
from dataclasses import dataclass

from personalclaw.sdk.channel import (
    AppConfig,
    ProviderSettings,
    owner_id_credential,
    owner_id_for,
    save_credential,
)

logger = logging.getLogger(__name__)

_APP = "discord-channel"

#: The channel's provider name: what inbound is delivered to core under, and what its owner
#: is keyed by (``owner_id_credential(PROVIDER)``). Here, not in the transport, so the setup
#: and doctor steps read it without importing the transport.
PROVIDER = "discord"

#: The plain credential-store / environment name of the bot token. Setup no longer writes
#: it — a name no uninstall can attribute to this app outlived the app — but it is still
#: read: an earlier release's setup stored the token there, and a container passes it in
#: the environment.
CRED_DISCORD_BOT_TOKEN = "DISCORD_BOT_TOKEN"

# DM activation modes. "always" answers every paired DM; "mention" only when the
# bot is @-mentioned (rare in a 1:1 DM but honored for parity); "off" disables DMs.
ACTIVATION_ALWAYS = "always"
ACTIVATION_MENTION = "mention"
ACTIVATION_OFF = "off"
_VALID_ACTIVATIONS = frozenset({ACTIVATION_ALWAYS, ACTIVATION_MENTION, ACTIVATION_OFF})


def _validate_activation(value: str) -> str:
    return value if value in _VALID_ACTIVATIONS else ACTIVATION_ALWAYS


def load_bot_token(config: dict | None = None, creds: dict[str, str] | None = None) -> str:
    """The bot token: THE one resolution order, shared by the transport, setup and doctor.

    This app's store (``config``, or the store itself when ``None``) → the shared
    credential store's :data:`CRED_DISCORD_BOT_TOKEN` (``creds``, from whoever holds an
    ``AppConfig`` or a setup context) → the process environment, where the gateway
    exports that store and a container passes the token. Three readers with three
    orders is how a doctor calls a working channel "not configured".
    """
    src = ProviderSettings.load(_APP) if config is None else config
    return (
        str(src.get("bot_token") or "")
        or (creds or {}).get(CRED_DISCORD_BOT_TOKEN, "")
        or os.environ.get(CRED_DISCORD_BOT_TOKEN, "")
    )


def adopt_owner_id() -> None:
    """Store the owner under Discord's own key, once, when only the shared key holds it.

    An earlier release's setup wrote the owner to the one shared ``PERSONALCLAW_OWNER_ID``, and
    core's ``owner_id_for`` still falls back to it for a channel with no key of its own. Saving
    that owner under ``owner_id_credential(PROVIDER)`` keeps it when the fallback goes. The owner
    in effect is the same before and after, so a failed save changes nothing. An install with its
    own key already, or with no owner at all, is left as it is. Logs the key name, never the id.
    """
    key = owner_id_credential(PROVIDER)
    if AppConfig.load().load_credentials().get(key):
        return
    owner = owner_id_for(PROVIDER)
    if not owner:
        return
    try:
        save_credential(key, owner)
    except Exception as exc:  # noqa: BLE001 — the fallback still supplies the same owner
        logger.warning(
            "%s: could not store the owner under %s (%s)", PROVIDER, key, type(exc).__name__
        )
        return
    logger.info("%s: stored the owner under its own key %s, from the shared key", PROVIDER, key)


class LiveConfig:
    """The provider config a transport runs on: the dict it was built with, until this app's store
    is written — then the store.

    The registry builds a transport from ``ProviderSettings.load`` once, when the app is enabled.
    The Apps page's Configure → Save writes the store and re-cycles nothing, so a transport that
    kept its build-time dict answered "No bot token configured" over a token the user had just
    saved. A store that differs from the one this transport last saw was written after it was
    built, so it wins; an unchanged store leaves the build dict in charge, which keeps an
    explicitly constructed transport isolated from whatever store the machine holds.
    """

    def __init__(self, config: dict | None) -> None:
        self._seen = ProviderSettings.load(_APP)
        self._config = dict(self._seen if config is None else config)

    def current(self) -> dict:
        store = ProviderSettings.load(_APP)
        if store != self._seen:
            self._seen, self._config = store, dict(store)
        return self._config


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


# One cached live instance, mirroring the Telegram + Slack apps: re-read whenever the store
# changes.
_settings: DiscordSettings | None = None
#: The raw store ``_settings`` was built from — the cache is valid only while the store says this.
_settings_store: dict | None = None


def get_settings() -> DiscordSettings:
    """The app's live settings: cached, and re-read whenever the app's store changed since.

    The Configure form writes the store directly and nothing tells this app it did, so a cache
    refreshed only by :func:`reload_settings` kept a saved change until the gateway restarted."""
    global _settings, _settings_store
    store = ProviderSettings.load(_APP)
    if _settings is None or store != _settings_store:
        _settings, _settings_store = DiscordSettings.load(), store
    return _settings


def reload_settings() -> DiscordSettings:
    """Force a re-read of the app store and refresh the cached instance."""
    global _settings, _settings_store
    _settings_store = ProviderSettings.load(_APP)
    _settings = DiscordSettings.load()
    return _settings
