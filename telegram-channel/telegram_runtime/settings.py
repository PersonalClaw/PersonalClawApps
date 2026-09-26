"""TelegramSettings — the telegram-channel app's OWN config + credential keys.

Telegram behavioral config (the DM activation posture) lives HERE in the app
bundle, persisted in the app's own store (``~/.personalclaw/apps/telegram-channel/
data/config.json`` via :class:`ProviderSettings`), NOT in core ``config.json``.
Core defines no Telegram config.

The bot token is a SECRET. It is the app store's ``bot_token`` setting, declared
``x-meta.sensitive``: setup and the Configure form both write it through
:class:`ProviderSettings`, which keeps the value in the credential store under a key
this app owns and puts only a reference in the settings file, so uninstalling the
app removes it. :func:`load_bot_token` is the one place it is read. Who is allowed
to talk (allowlist, pairing) and which groups are tracked are owned by the core
sender-trust seam (``channel_trust``, provider=``"telegram"``), so this app keeps no
allowlist of its own — the whole point of CE-1.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from personalclaw.sdk.channel import ProviderSettings

logger = logging.getLogger(__name__)

_APP = "telegram-channel"

#: The channel's provider name: what inbound is delivered to core under, and what its owner
#: is keyed by (``owner_id_credential(PROVIDER)``). Here, not in the transport, so the setup
#: and doctor steps read it without importing the transport.
PROVIDER = "telegram"

#: The plain credential-store / environment name of the bot token. Setup no longer writes
#: it — a name no uninstall can attribute to this app outlived the app — but it is still
#: read: an earlier release's setup stored the token there, and a container passes it in
#: the environment.
CRED_TELEGRAM_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"

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
    credential store's :data:`CRED_TELEGRAM_BOT_TOKEN` (``creds``, from whoever holds an
    ``AppConfig`` or a setup context) → the process environment, where the gateway
    exports that store and a container passes the token. Three readers with three
    orders is how a doctor calls a working channel "not configured".
    """
    src = ProviderSettings.load(_APP) if config is None else config
    return (
        str(src.get("bot_token") or "")
        or (creds or {}).get(CRED_TELEGRAM_BOT_TOKEN, "")
        or os.environ.get(CRED_TELEGRAM_BOT_TOKEN, "")
    )


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
class TelegramSettings:
    """The telegram-channel app's behavioral config (its own store)."""

    dm_activation: str = ACTIVATION_ALWAYS

    @classmethod
    def load(cls) -> "TelegramSettings":
        """Read + coerce the app store."""
        d = ProviderSettings.load(_APP)
        return cls(dm_activation=_validate_activation(d.get("dm_activation", ACTIVATION_ALWAYS)))


# One cached live instance, mirroring the Slack app: re-read whenever the store changes.
_settings: TelegramSettings | None = None
#: The raw store ``_settings`` was built from — the cache is valid only while the store says this.
_settings_store: dict | None = None


def get_settings() -> TelegramSettings:
    """The app's live settings: cached, and re-read whenever the app's store changed since.

    The Configure form writes the store directly and nothing tells this app it did, so a cache
    refreshed only by :func:`reload_settings` kept a saved change until the gateway restarted."""
    global _settings, _settings_store
    store = ProviderSettings.load(_APP)
    if _settings is None or store != _settings_store:
        _settings, _settings_store = TelegramSettings.load(), store
    return _settings


def reload_settings() -> TelegramSettings:
    """Force a re-read of the app store and refresh the cached instance."""
    global _settings, _settings_store
    _settings_store = ProviderSettings.load(_APP)
    _settings = TelegramSettings.load()
    return _settings
