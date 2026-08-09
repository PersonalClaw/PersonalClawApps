"""MailInboxSettings — coercion, the app store round-trip, and the fail-closed posture.

Also asserts the secret boundary: the password key lives in the credential store, and
``MailInboxSettings`` carries no password field at all (secrets never round-trip through
ProviderSettings).
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
    assert MailInboxSettings(username="u@x.com").receiving_address == "u@x.com"
    assert MailInboxSettings(username="u@x.com", address="bound@x.com").receiving_address == "bound@x.com"


def test_configured_requires_host_and_username_not_allowlist():
    # An empty allowlist is a deliberate posture, NOT "unconfigured".
    s = MailInboxSettings(host="imap.x.com", username="u@x.com", allow_senders=[])
    assert s.configured is True


def test_port_coercion():
    assert _coerce_port("143") == 143
    assert _coerce_port("bogus") == 993
    assert _coerce_port(70000) == 993  # out of range → default
    assert _coerce_port(0) == 993


def test_sender_coercion_dedupes_and_lowercases():
    assert _coerce_senders([" A@X.com ", "a@x.com", "", "b@x.com"]) == ["a@x.com", "b@x.com"]
    assert _coerce_senders("not a list") == []


def test_load_roundtrips_app_store():
    from personalclaw.sdk.settings import ProviderSettings

    ProviderSettings.update(
        _APP,
        {"host": "imap.x.com", "port": 993, "username": "u@x.com", "allow_senders": ["a@x.com"]},
    )
    s = MailInboxSettings.load()
    assert s.host == "imap.x.com" and s.username == "u@x.com"
    assert s.allow_senders == ["a@x.com"] and s.configured is True


def test_no_password_field_on_settings():
    # The secret must never be a settings field (it lives only in the credential store).
    field_names = {f.name for f in dataclasses.fields(MailInboxSettings)}
    assert "password" not in field_names
    assert CRED_MAIL_PASSWORD == "MAIL_INBOX_PASSWORD"


def test_cache_refreshes_on_reload():
    from personalclaw.sdk.settings import ProviderSettings

    ProviderSettings.update(_APP, {"host": "one.x.com", "username": "u@x.com"})
    assert get_settings().host == "one.x.com"
    ProviderSettings.update(_APP, {"host": "two.x.com", "username": "u@x.com"})
    # Cached until an explicit reload.
    assert get_settings().host == "one.x.com"
    assert reload_settings().host == "two.x.com"
