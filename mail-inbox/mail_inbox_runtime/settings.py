"""MailInboxSettings — the mail-inbox app's OWN non-secret config + credential keys.

Where each value lives, and why (the app/core boundary, provider-boundary.md §2.5/§2.6):

- IMAP host/port/ssl/username/address/folder and the sender **allowlist** are
  NON-secret behavioral config, so they live in this app's own ``ProviderSettings``
  store (``~/.personalclaw/apps/mail-inbox/data/config.json``), NOT in core
  ``config.json``. Core defines no mail config.
- The IMAP **password** is a SECRET, so it lives ONLY in the shared credential store
  under this app's own key ``MAIL_INBOX_PASSWORD`` (EIAT guardrail: "credentials come
  only from the SDK credential store, never app.json/ProviderSettings"). The setup step
  writes it; the provider reads it back by name. It is never persisted in the settings
  store, never echoed into a log.

The allowlist is the inbound security surface: it is stored here but ENFORCED in the
provider, fail-closed — an empty/absent allowlist surfaces ZERO messages (§2.7).

The **prompt-bound address table** (EIAT-4, contract C4) is non-secret behavioral config
too, so it lives in the same store under ``bound_addresses`` — see ``addresses.py`` for
the row shape and the fail-closed per-address rule. It is declared in ``app.json``'s
schema, which is what makes it editable from the platform's generated app-settings page
(core's config PUT rejects any key the schema does not declare) and what puts the write
path and this read path on the SAME file (``data/config.json``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from personalclaw.sdk.settings import ProviderSettings

from mail_inbox_runtime.addresses import (
    SETTINGS_KEY as _ADDRESSES_KEY,
    BoundAddress,
    load_bound_addresses,
    # ONE definition of allowlist normalization, shared with the per-address lists — so the
    # app-wide list and a bound row's can never disagree about what a pattern means.
    normalize_senders as _coerce_senders,
)

logger = logging.getLogger(__name__)

_APP = "mail-inbox"

#: The credential-store key the IMAP password is stored under. App-owned: the setup
#: step writes it and the runtime reads it back by name (never in ProviderSettings).
CRED_MAIL_PASSWORD = "MAIL_INBOX_PASSWORD"

_DEFAULT_PORT = 993
_DEFAULT_FOLDER = "INBOX"


def _coerce_port(value: object) -> int:
    try:
        port = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return _DEFAULT_PORT
    return port if 1 <= port <= 65535 else _DEFAULT_PORT


@dataclass
class MailInboxSettings:
    """The mail-inbox app's behavioral config (its own store). No secrets here."""

    host: str = ""
    port: int = _DEFAULT_PORT
    use_ssl: bool = True
    username: str = ""
    address: str = ""
    folder: str = _DEFAULT_FOLDER
    allow_senders: list[str] = field(default_factory=list)
    #: Prompt-bound receiving addresses (C4). Coerced at load; see ``addresses.py``.
    bound_addresses: list[BoundAddress] = field(default_factory=list)

    @property
    def receiving_address(self) -> str:
        """The address mail arrives at (the inbox channel id). Falls back to the login."""
        return self.address or self.username

    @property
    def configured(self) -> bool:
        """True once the mailbox connection is fully specified. The allowlist being
        empty does NOT count as unconfigured — it is a deliberate fail-closed posture."""
        return bool(self.host and self.username)

    @classmethod
    def load(cls) -> "MailInboxSettings":
        """Read + coerce the app store (never the credential store)."""
        d = ProviderSettings.load(_APP)
        return cls(
            host=str(d.get("host", "")).strip(),
            port=_coerce_port(d.get("port", _DEFAULT_PORT)),
            use_ssl=bool(d.get("use_ssl", True)),
            username=str(d.get("username", "")).strip(),
            address=str(d.get("address", "")).strip(),
            folder=str(d.get("folder", _DEFAULT_FOLDER)).strip() or _DEFAULT_FOLDER,
            allow_senders=_coerce_senders(d.get("allow_senders", [])),
            bound_addresses=load_bound_addresses(d.get(_ADDRESSES_KEY, [])),
        )


# One cached live instance, mirroring the telegram/slack apps: build once, refresh on
# write via reload_settings().
_settings: MailInboxSettings | None = None


def get_settings() -> MailInboxSettings:
    """The app's live settings (cached; refreshed by :func:`reload_settings`)."""
    global _settings
    if _settings is None:
        _settings = MailInboxSettings.load()
    return _settings


def reload_settings() -> MailInboxSettings:
    """Force a re-read of the app store and refresh the cached instance."""
    global _settings
    _settings = MailInboxSettings.load()
    return _settings
