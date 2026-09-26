"""MailInboxSettings — the mail-inbox app's OWN non-secret config + credential keys.

Where each value lives, and why (the app/core boundary, provider-boundary.md §2.5/§2.6):

- IMAP host/port/ssl/username/address/folder and the sender **allowlist** are
  NON-secret behavioral config, so they live in this app's own ``ProviderSettings``
  store (``~/.personalclaw/apps/mail-inbox/data/config.json``), NOT in core
  ``config.json``. Core defines no mail config.
- The IMAP and SMTP **passwords** are SECRETS. They are the settings ``password`` /
  ``smtp_password``, declared ``x-meta.sensitive``, so the settings FILE never holds one:
  :class:`ProviderSettings` keeps each value in the credential store under a key this app
  owns and writes a reference in its place, and uninstalling the app removes them (the EIAT
  guardrail, "credentials come only from the credential store", holds for the value). Setup
  and the Configure form both write them there; :func:`load_passwords` is the one place they
  are read. Never echoed into a log. (Setup used to save them to the shared store under the
  plain names ``MAIL_INBOX_PASSWORD`` / ``MAIL_INBOX_SMTP_PASSWORD``, which no uninstall can
  attribute to this app; those are still read, for an install configured that way.)

The allowlist is the inbound security surface: it is stored here but ENFORCED in the
provider, fail-closed — an empty/absent allowlist surfaces ZERO messages (§2.7).

The **prompt-bound address table** (EIAT-4, contract C4) is non-secret behavioral config
too, so it lives in the same store under ``bound_addresses`` — see ``addresses.py`` for
the row shape and the fail-closed per-address rule. It is declared in ``app.json``'s
schema, which is what makes it editable from the platform's generated app-settings page
(core's config PUT rejects any key the schema does not declare) and what puts the write
path and this read path on the SAME file (``data/config.json``).

**Outbound (EIAT-3, contract C3)** follows the same split: SMTP host/port/TLS-mode/login
are non-secret and live here; the SMTP **password** is a second secret, its own setting. It
is deliberately NOT the IMAP one — the runtime never silently reuses one credential for the
other transport. (The setup step may COPY the IMAP password into it when the user says so;
that is an explicit, visible choice rather than a hidden fallback.) ``send_enabled`` defaults
to **False**: guardrail 4 means a fully configured mailbox with a working SMTP password still
only ever composes drafts until the user turns sending on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from personalclaw.sdk.channel import AppConfig
from personalclaw.sdk.settings import ProviderSettings

from mail_inbox_runtime.addresses import (
    SETTINGS_KEY as _ADDRESSES_KEY,
    BoundAddress,
    load_bound_addresses,
    # ONE definition of allowlist normalization, shared with the per-address lists — so the
    # app-wide list and a bound row's can never disagree about what a pattern means.
    normalize_senders as _coerce_senders,
)
from mail_inbox_runtime.smtp_client import (
    DEFAULT_SMTP_PORT,
    SMTP_STARTTLS,
    VALID_SMTP_SECURITY,
)

logger = logging.getLogger(__name__)

_APP = "mail-inbox"

#: The two passwords' settings keys (declared ``x-meta.sensitive``; see the module docstring).
#: The SMTP one is a SEPARATE secret: the runtime never falls back to the IMAP one, so an unset
#: outbound credential fails closed (the reply is drafted) instead of quietly authenticating
#: with the inbound one.
KEY_PASSWORD = "password"
KEY_SMTP_PASSWORD = "smtp_password"

#: The plain credential-store names an earlier release's setup saved the passwords under.
#: Nothing writes them any more; :func:`load_passwords` still reads them.
CRED_MAIL_PASSWORD = "MAIL_INBOX_PASSWORD"
CRED_SMTP_PASSWORD = "MAIL_INBOX_SMTP_PASSWORD"

_DEFAULT_PORT = 993
_DEFAULT_FOLDER = "INBOX"


def _coerce_port(value: object, default: int = _DEFAULT_PORT) -> int:
    try:
        port = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return port if 1 <= port <= 65535 else default


def _coerce_security(value: object) -> str:
    """An unknown TLS mode falls back to STARTTLS, never to ``plain``: a typo must not
    silently put an app password on the wire in the clear."""
    mode = str(value or "").strip().lower()
    return mode if mode in VALID_SMTP_SECURITY else SMTP_STARTTLS


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
    # ── outbound (C3) ──
    #: Guardrail 4: **draft-by-default**. False means a reply is composed and written to
    #: the drafts dir and nothing is sent. Turning this on is the user's explicit opt-in to
    #: an irreversible, outward-facing action.
    send_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = DEFAULT_SMTP_PORT
    smtp_security: str = SMTP_STARTTLS
    smtp_username: str = ""

    @property
    def receiving_address(self) -> str:
        """The address mail arrives at (the inbox channel id). Falls back to the login."""
        return self.address or self.username

    @property
    def smtp_login(self) -> str:
        """The SMTP login. Defaults to the IMAP username (the same account), which is a
        non-secret convenience — the PASSWORD never falls back that way."""
        return self.smtp_username or self.username

    @property
    def smtp_ready(self) -> bool:
        """True once the outbound transport is fully specified. Says nothing about whether
        sending is ALLOWED — that is :attr:`send_enabled` plus the platform posture."""
        return bool(self.smtp_host and self.smtp_login)

    @property
    def configured(self) -> bool:
        """True once the mailbox connection is fully specified. The allowlist being
        empty does NOT count as unconfigured — it is a deliberate fail-closed posture."""
        return bool(self.host and self.username)

    @classmethod
    def load(cls) -> "MailInboxSettings":
        """Read + coerce the app store's behavioral config. The passwords are not part of it:
        :func:`load_passwords` reads them."""
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
            # Guardrail 4: the DEFAULT is False, and an absent/unparseable value keeps it
            # False — the safe side for an irreversible outbound action.
            send_enabled=d.get("send_enabled", False) is True,
            smtp_host=str(d.get("smtp_host", "")).strip(),
            smtp_port=_coerce_port(d.get("smtp_port", DEFAULT_SMTP_PORT), DEFAULT_SMTP_PORT),
            smtp_security=_coerce_security(d.get("smtp_security", SMTP_STARTTLS)),
            smtp_username=str(d.get("smtp_username", "")).strip(),
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


def load_passwords(
    config: dict | None = None, creds: dict[str, str] | None = None
) -> tuple[str, str]:
    """``(imap_password, smtp_password)`` — THE one resolution order, shared by the source,
    setup and doctor.

    Each password comes from this app's store (``config``, or the store itself when ``None``),
    else from the plain name an earlier release's setup saved it under in the shared
    credential store (``creds``, read from ``AppConfig`` when not given and only when the store
    leaves a password unset). The SMTP password never falls back to the IMAP one."""
    src = ProviderSettings.load(_APP) if config is None else config
    imap_pass = str(src.get(KEY_PASSWORD) or "")
    smtp_pass = str(src.get(KEY_SMTP_PASSWORD) or "")
    if not (imap_pass and smtp_pass):
        shared = _shared_credentials() if creds is None else creds
        imap_pass = imap_pass or shared.get(CRED_MAIL_PASSWORD, "")
        smtp_pass = smtp_pass or shared.get(CRED_SMTP_PASSWORD, "")
    return imap_pass, smtp_pass


def _shared_credentials() -> dict[str, str]:
    try:
        return AppConfig.load().load_credentials()
    except Exception:
        logger.debug("mail-inbox: credential load failed", exc_info=True)
        return {}
