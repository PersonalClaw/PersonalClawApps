"""EmailSettings — the email-channel app's OWN non-secret config + credential keys.

Where each value lives, and why (the app/core boundary, ``provider-boundary.md``
§2.5/§2.6):

- IMAP/SMTP **hosts, ports, TLS mode, logins, mailbox address, folder and poll
  cadence** are NON-secret behavioral config, so they live in this app's own
  ``ProviderSettings`` store (``~/.personalclaw/apps/email-channel/data/config.json``),
  NOT in core ``config.json``. Core defines no email config.
- The IMAP and SMTP **passwords** are SECRETS. They are the settings ``imap_password`` /
  ``smtp_password``, declared ``x-meta.sensitive``, so the settings FILE never holds one:
  :class:`ProviderSettings` keeps each value in the credential store under a key this app
  owns and writes a reference in its place, and uninstalling the app removes them. Setup
  and the Configure form both write them there; :func:`load_credentials` is the one place
  they are read. Never logged. (Setup used to save them to the shared store under the
  plain names ``EMAIL_IMAP_PASS`` / ``EMAIL_SMTP_PASS``, which no uninstall can attribute
  to this app; those are still read, for an install configured that way.)

Who is allowed to talk is owned by the **core sender-trust seam** (``channel_trust``,
provider ``"email"``), so this app keeps NO allowlist of its own — that is the whole
point of CE-1. (The sibling ``mail-inbox`` app has an app-local allowlist because it is
an *inbox source*, not a channel; a channel binds to the seam.)

The plan's credential keys are ``EMAIL_IMAP_{HOST,USER,PASS,PORT}`` /
``EMAIL_SMTP_{...}``. Only the two passwords are actually secret, so only those two live
in the credential store; host/user/port live in the app store where the user can see and
edit them in the Configure form. Putting a hostname in the credential store would claim a
secrecy it does not have and hide it from the UI.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from personalclaw.sdk.channel import ProviderSettings

logger = logging.getLogger(__name__)

_APP = "email-channel"

#: The two passwords' settings keys (declared ``x-meta.sensitive``; see the module docstring).
KEY_IMAP_PASSWORD = "imap_password"
KEY_SMTP_PASSWORD = "smtp_password"

#: The plain credential-store names an earlier release's setup saved the passwords under.
#: Nothing writes them any more; :func:`load_credentials` still reads them.
CRED_IMAP_PASS = "EMAIL_IMAP_PASS"
CRED_SMTP_PASS = "EMAIL_SMTP_PASS"

# Non-secret settings keys, named to match the plan's EMAIL_IMAP_*/EMAIL_SMTP_*
# vocabulary. These are the app-store keys (settingsSchema properties).
KEY_IMAP_HOST = "imap_host"
KEY_IMAP_PORT = "imap_port"
KEY_IMAP_USER = "imap_user"
KEY_SMTP_HOST = "smtp_host"
KEY_SMTP_PORT = "smtp_port"
KEY_SMTP_USER = "smtp_user"

DEFAULT_IMAP_PORT = 993
DEFAULT_SMTP_PORT = 587
DEFAULT_FOLDER = "INBOX"
#: The plan's inbound cadence: poll IMAP every 60s. IDLE is deferred (see the
#: transport's module docstring).
DEFAULT_POLL_SECS = 60

# SMTP transport modes. "starttls" (587) upgrades a plaintext connection; "ssl" (465)
# is implicit TLS from the first byte; "plain" is unencrypted and only sane against a
# local relay on loopback.
SMTP_STARTTLS = "starttls"
SMTP_SSL = "ssl"
SMTP_PLAIN = "plain"
_VALID_SMTP_SECURITY = frozenset({SMTP_STARTTLS, SMTP_SSL, SMTP_PLAIN})

# DM activation modes, mirroring the telegram/discord apps: "always" answers every
# paired sender's mail; "off" disables inbound entirely (outbound delivery still works,
# which is how a user runs email as a notification sink without a conversation).
ACTIVATION_ALWAYS = "always"
ACTIVATION_OFF = "off"
_VALID_ACTIVATIONS = frozenset({ACTIVATION_ALWAYS, ACTIVATION_OFF})


def _validate_activation(value: str) -> str:
    return value if value in _VALID_ACTIVATIONS else ACTIVATION_ALWAYS


class LiveConfig:
    """The provider config a transport runs on: the dict it was built with, until this app's store
    is written — then the store.

    The registry builds a transport from ``ProviderSettings.load`` once, when the app is enabled.
    The Apps page's Configure → Save writes the store and re-cycles nothing, so a transport that
    overlaid its build-time dict on the store kept probing and sending with the host, port and
    user it was built with. A store that differs from the one this transport last saw was written
    after it was built, so it wins; an unchanged store leaves the build dict in charge, which
    keeps an explicitly constructed transport isolated from whatever store the machine holds.
    """

    def __init__(self, config: dict | None) -> None:
        self._seen = ProviderSettings.load(_APP)
        self._config = dict(self._seen if config is None else config)

    def current(self) -> dict:
        store = ProviderSettings.load(_APP)
        if store != self._seen:
            self._seen, self._config = store, dict(store)
        return self._config


def _validate_smtp_security(value: str) -> str:
    return value if value in _VALID_SMTP_SECURITY else SMTP_STARTTLS


def _coerce_port(value: object, default: int) -> int:
    try:
        port = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return port if 1 <= port <= 65535 else default


def _coerce_poll_secs(value: object) -> int:
    """Clamp the poll cadence to a sane band.

    Below 10s a poll loop hammers the provider (and many hosts throttle or ban for
    it); above an hour the channel stops feeling conversational. An unparseable value
    falls back to the plan's 60s rather than disabling the loop."""
    try:
        secs = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_POLL_SECS
    return max(10, min(secs, 3600))


@dataclass
class EmailSettings:
    """The email-channel app's behavioral config (its own store). No secrets here."""

    imap_host: str = ""
    imap_port: int = DEFAULT_IMAP_PORT
    imap_user: str = ""
    imap_use_ssl: bool = True
    folder: str = DEFAULT_FOLDER
    smtp_host: str = ""
    smtp_port: int = DEFAULT_SMTP_PORT
    smtp_user: str = ""
    smtp_security: str = SMTP_STARTTLS
    address: str = ""
    poll_secs: int = DEFAULT_POLL_SECS
    dm_activation: str = ACTIVATION_ALWAYS

    @property
    def mailbox_address(self) -> str:
        """The address this channel sends AS and receives AT.

        Falls back to the IMAP login, which is the full address at every provider
        whose app-password flow we document. This value is load-bearing twice: it is
        the ``From`` of every outbound message AND the anchor of the self-message
        filter, so an empty one would let the bot's own mail back in."""
        return self.address or self.imap_user

    @property
    def inbound_configured(self) -> bool:
        """True once IMAP is fully specified (the password lives elsewhere)."""
        return bool(self.imap_host and self.imap_user)

    @property
    def outbound_configured(self) -> bool:
        """True once SMTP is fully specified (the password lives elsewhere)."""
        return bool(self.smtp_host and self.smtp_user)

    @classmethod
    def from_dict(cls, d: dict) -> "EmailSettings":
        """Coerce a raw settings dict. THE one coercion path.

        Both :meth:`load` (the app store) and the transport's per-instance config overlay
        go through here, so a value can never be validated on one route and trusted raw on
        the other."""
        return cls(
            imap_host=str(d.get(KEY_IMAP_HOST, "")).strip(),
            imap_port=_coerce_port(d.get(KEY_IMAP_PORT, DEFAULT_IMAP_PORT), DEFAULT_IMAP_PORT),
            imap_user=str(d.get(KEY_IMAP_USER, "")).strip(),
            imap_use_ssl=bool(d.get("imap_use_ssl", True)),
            folder=str(d.get("folder", DEFAULT_FOLDER)).strip() or DEFAULT_FOLDER,
            smtp_host=str(d.get(KEY_SMTP_HOST, "")).strip(),
            smtp_port=_coerce_port(d.get(KEY_SMTP_PORT, DEFAULT_SMTP_PORT), DEFAULT_SMTP_PORT),
            smtp_user=str(d.get(KEY_SMTP_USER, "")).strip(),
            smtp_security=_validate_smtp_security(str(d.get("smtp_security", SMTP_STARTTLS))),
            address=str(d.get("address", "")).strip(),
            poll_secs=_coerce_poll_secs(d.get("poll_secs", DEFAULT_POLL_SECS)),
            dm_activation=_validate_activation(str(d.get("dm_activation", ACTIVATION_ALWAYS))),
        )

    @classmethod
    def load(cls) -> "EmailSettings":
        """Read + coerce the app store (never the credential store)."""
        return cls.from_dict(ProviderSettings.load(_APP))


# One cached live instance, mirroring the telegram/discord apps: re-read whenever the store
# changes.
_settings: EmailSettings | None = None
#: The raw store ``_settings`` was built from — the cache is valid only while the store says this.
_settings_store: dict | None = None


def get_settings() -> EmailSettings:
    """The app's live settings: cached, and re-read whenever the app's store changed since.

    The Configure form writes the store directly and nothing tells this app it did, so a cache
    refreshed only by :func:`reload_settings` kept a saved change until the gateway restarted."""
    global _settings, _settings_store
    store = ProviderSettings.load(_APP)
    if _settings is None or store != _settings_store:
        _settings, _settings_store = EmailSettings.from_dict(store), store
    return _settings


def reload_settings() -> EmailSettings:
    """Force a re-read of the app store and refresh the cached instance."""
    global _settings, _settings_store
    _settings_store = ProviderSettings.load(_APP)
    _settings = EmailSettings.from_dict(_settings_store)
    return _settings


def load_raw_settings() -> dict:
    """The app store's RAW dict — the base a per-instance config overlays before coercion."""
    return ProviderSettings.load(_APP)


def load_credentials(
    config: dict | None = None, creds: dict[str, str] | None = None
) -> tuple[str, str]:
    """``(imap_password, smtp_password)`` — THE one resolution order, shared by the
    transport, setup and doctor.

    Each password comes from this app's store (``config``, or the store itself when
    ``None``), else from the plain name an earlier release's setup saved it under in the
    shared credential store (``creds``, read from ``AppConfig`` when not given and only
    when the store leaves a password unset).

    An empty SMTP password falls back to the IMAP one: at Gmail/Fastmail/iCloud a
    single app password authenticates BOTH protocols, so asking for it twice is the
    kind of friction that ends in a half-configured channel. A separate SMTP secret is
    still honored when the user sets one (some corporate relays differ)."""
    src = load_raw_settings() if config is None else config
    imap_pass = str(src.get(KEY_IMAP_PASSWORD) or "")
    smtp_pass = str(src.get(KEY_SMTP_PASSWORD) or "")
    if not (imap_pass and smtp_pass):
        shared = _shared_credentials() if creds is None else creds
        imap_pass = imap_pass or shared.get(CRED_IMAP_PASS, "")
        smtp_pass = smtp_pass or shared.get(CRED_SMTP_PASS, "")
    return imap_pass, smtp_pass or imap_pass


def _shared_credentials() -> dict[str, str]:
    from personalclaw.sdk.channel import AppConfig

    try:
        return AppConfig.load().load_credentials()
    except Exception:
        logger.debug("email: credential load failed", exc_info=True)
        return {}
