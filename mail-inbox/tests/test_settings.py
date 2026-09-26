"""MailInboxSettings — coercion, the app store round-trip, and the fail-closed posture.

Also asserts the secret boundary: ``MailInboxSettings`` carries no password field at all, so
the behavioral config that gets logged and compared never holds one. The passwords are read
only through ``load_passwords`` (see ``test_setup_secrets_uninstall.py``).
"""

from __future__ import annotations

import dataclasses

from mail_inbox_runtime.settings import (
    CRED_MAIL_PASSWORD,
    MailInboxSettings,
    _APP,
    _coerce_port,
    _coerce_senders,
    get_settings,
    reload_settings,
)


def test_defaults():
    s = MailInboxSettings()
    assert s.port == 993 and s.use_ssl is True and s.folder == "INBOX"
    assert s.allow_senders == []
    assert s.configured is False


def test_receiving_address_falls_back_to_username():
    assert MailInboxSettings(username="u@example.com").receiving_address == "u@example.com"
    assert MailInboxSettings(username="u@example.com", address="bound@example.com").receiving_address == "bound@example.com"


def test_configured_requires_host_and_username_not_allowlist():
    # An empty allowlist is a deliberate posture, NOT "unconfigured".
    s = MailInboxSettings(host="imap.example.com", username="u@example.com", allow_senders=[])
    assert s.configured is True


def test_port_coercion():
    assert _coerce_port("143") == 143
    assert _coerce_port("bogus") == 993
    assert _coerce_port(70000) == 993  # out of range → default
    assert _coerce_port(0) == 993


def test_sender_coercion_dedupes_and_lowercases():
    assert _coerce_senders([" A@EXAMPLE.com ", "a@example.com", "", "b@example.com"]) == ["a@example.com", "b@example.com"]
    assert _coerce_senders("not a list") == []


def test_load_roundtrips_app_store():
    from personalclaw.sdk.settings import ProviderSettings

    ProviderSettings.update(
        _APP,
        {"host": "imap.example.com", "port": 993, "username": "u@example.com", "allow_senders": ["a@example.com"]},
    )
    s = MailInboxSettings.load()
    assert s.host == "imap.example.com" and s.username == "u@example.com"
    assert s.allow_senders == ["a@example.com"] and s.configured is True


def test_no_password_field_on_settings():
    # The behavioral dataclass never carries a secret; load_passwords is the one reader.
    field_names = {f.name for f in dataclasses.fields(MailInboxSettings)}
    assert "password" not in field_names and "smtp_password" not in field_names
    # The plain name an earlier setup saved the IMAP password under, still read.
    assert CRED_MAIL_PASSWORD == "MAIL_INBOX_PASSWORD"


def test_cache_refreshes_on_reload():
    from personalclaw.sdk.settings import ProviderSettings

    ProviderSettings.update(_APP, {"host": "one.example.com", "username": "u@example.com"})
    assert get_settings().host == "one.example.com"
    ProviderSettings.update(_APP, {"host": "two.example.com", "username": "u@example.com"})
    # Cached until an explicit reload.
    assert get_settings().host == "one.example.com"
    assert reload_settings().host == "two.example.com"
