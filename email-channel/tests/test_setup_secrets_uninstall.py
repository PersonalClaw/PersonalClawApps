"""The app passwords belong to this app: kept under keys the app owns, gone once it is uninstalled.

``personalclaw setup`` saved the IMAP and SMTP passwords to the shared credential store under the
plain names ``EMAIL_IMAP_PASS`` / ``EMAIL_SMTP_PASS``. No uninstall can attribute a plain name to
an app, so removing the app left both passwords behind. They are now the settings
``imap_password`` / ``smtp_password``, declared ``x-meta.sensitive``: setup and the Configure form
write them through ``ProviderSettings``, which keeps each value in the credential store under a
key this app owns and puts only a reference in the settings file (core #3607), and both removal
rungs purge those keys.

Driven end to end on the real bundle: installed with core's own installer into the isolated
home, setup run with a ``SetupContext`` wired the way ``personalclaw setup`` wires it (the real
credential store, the real ``ProviderSettings``), the transport built the way the registry
builds it, and core's own keep-data uninstall, which is the Apps page's Uninstall.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path

import pytest

from personalclaw.apps import app_manager
from personalclaw.config import credentials
from personalclaw.dashboard.handlers.apps import api_app_config_put
from personalclaw.providers.settings import ProviderSettings
from personalclaw.sdk.cli import SetupContext

import cli_setup
from email_runtime.settings import CRED_IMAP_PASS, CRED_SMTP_PASS
from email_runtime.transport import create_provider

_APP = "email-channel"
_BUNDLE = Path(__file__).resolve().parents[1]
#: Made up per run: the installed copy of the bundle carries this file, so a literal would be
#: found in it by every "where is the password on disk" check below.
PASSWORD = secrets.token_hex(12)
SMTP_PASSWORD = secrets.token_hex(12)


@pytest.fixture
def home(_isolate_home, monkeypatch):
    """The real bundle installed into the isolated home, nothing configured."""
    for key in (CRED_IMAP_PASS, CRED_SMTP_PASS):
        # setenv first so teardown restores the variable even if the code under test sets it.
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
    assert app_manager.install(_BUNDLE, confirm=True).ok
    yield _isolate_home
    app_manager.force_uninstall(_APP)  # a no-op once the test removed it


def _gmail_setup(imap_password: str, smtp_password: str = "") -> list[str]:
    # confirm, provider, address, imap user, imap host, imap port, imap ssl, folder,
    # smtp user, smtp host, smtp port, security, imap password, smtp password,
    # poll secs, activation
    return [
        "y", "gmail", "bot@gmail.com", "", "", "", "", "", "", "", "", "",
        imap_password, smtp_password, "90", "always",
    ]


def _run_setup(answers: list[str]) -> None:
    """``cli_setup:run`` with the context core's setup runner builds for it."""
    replies = iter(answers)
    cli_setup.run(
        SetupContext(
            app_name=_APP,
            get_credential=credentials.get_credential,
            save_credential=credentials.save_credential,
            settings=ProviderSettings,
            print=lambda *_: None,
            input=lambda _prompt: next(replies, ""),
        )
    )


async def _configure_save(values: dict) -> None:
    """The Apps page's Configure → Save: core's PUT /api/apps/{name}/config handler."""

    class _Request(dict):
        match_info = {"name": _APP}

        async def json(self):
            return values

    resp = await api_app_config_put(_Request())
    assert resp.status == 200, resp.text


def _hits(home: Path, needle: str) -> list[str]:
    """Every file under the home whose bytes contain ``needle``."""
    return sorted(
        str(p.relative_to(home))
        for p in home.rglob("*")
        if p.is_file() and not p.is_symlink() and needle.encode() in p.read_bytes()
    )


def test_both_passwords_are_declared_sensitive():
    """The declaration is what masks them on every read route and renders a password input."""
    props = json.loads((_BUNDLE / "app.json").read_text())["provider"]["settingsSchema"]
    for field in ("imap_password", "smtp_password"):
        assert props["properties"][field]["x-meta"]["sensitive"] is True


def test_setup_keeps_the_passwords_under_keys_this_app_owns(home):
    _run_setup(_gmail_setup(PASSWORD, SMTP_PASSWORD))

    for secret in (PASSWORD, SMTP_PASSWORD):
        assert _hits(home, secret) == [".env"], "a password is somewhere besides the store"
    settings_file = ProviderSettings.config_path(_APP).read_text()
    assert settings_file.count("{{secret:PCSECRET_APP_") == 2
    names = credentials.credential_names()
    assert CRED_IMAP_PASS not in names and CRED_SMTP_PASS not in names
    # The channel runs on them: the transport the registry builds.
    assert create_provider(ProviderSettings.load(_APP))._passwords() == (PASSWORD, SMTP_PASSWORD)


def test_an_unset_smtp_password_reuses_the_imap_one(home):
    _run_setup(_gmail_setup(PASSWORD))

    assert create_provider(ProviderSettings.load(_APP))._passwords() == (PASSWORD, PASSWORD)


def test_uninstalling_removes_the_passwords_setup_saved(home):
    _run_setup(_gmail_setup(PASSWORD, SMTP_PASSWORD))

    assert app_manager.uninstall_keep_data(_APP) is True

    assert _hits(home, PASSWORD) == [] and _hits(home, SMTP_PASSWORD) == []
    assert not [k for k in credentials.credential_names() if k.startswith("PCSECRET_")]


@pytest.mark.asyncio
async def test_a_password_saved_on_configure_reaches_the_transport_and_uninstall_removes_it(home):
    await _configure_save(
        {"imap_host": "imap.test", "imap_user": "u@test", "imap_password": PASSWORD}
    )
    assert _hits(home, PASSWORD) == [".env"]
    assert create_provider(ProviderSettings.load(_APP))._passwords()[0] == PASSWORD

    assert app_manager.uninstall_keep_data(_APP) is True

    assert _hits(home, PASSWORD) == []
