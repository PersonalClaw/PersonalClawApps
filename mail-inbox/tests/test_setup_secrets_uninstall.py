"""The IMAP and SMTP passwords belong to this app: kept under keys it owns, gone once it is uninstalled.

``personalclaw setup`` saved both passwords to the shared credential store under the plain names
``MAIL_INBOX_PASSWORD`` / ``MAIL_INBOX_SMTP_PASSWORD``. No uninstall can attribute a plain name to
an app, so removing the app left both passwords behind, and the app's own Configure form could set
neither. They are now the settings ``password`` / ``smtp_password``, declared ``x-meta.sensitive``:
setup and the Configure form write them through ``ProviderSettings``, which keeps each value in the
credential store under a key this app owns and puts only a reference in the settings file (core
#3607), and the keep-data uninstall purges those keys. email-channel made the same move in #124.

Driven end to end on the real bundle: installed with core's own installer into the isolated home,
setup run with a ``SetupContext`` wired the way ``personalclaw setup`` wires it (the real
credential store, the real ``ProviderSettings``), and core's own keep-data uninstall, which is the
Apps page's Uninstall.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path

import pytest

from personalclaw.apps import app_manager
from personalclaw.config import credentials
from personalclaw.dashboard.handlers.apps import api_app_config_put
from personalclaw.providers.settings import ProviderSettings
from personalclaw.sdk.cli import SetupContext

import cli_doctor
import cli_setup
from mail_inbox_runtime.provider import MailInboxProvider
from mail_inbox_runtime.settings import CRED_MAIL_PASSWORD, CRED_SMTP_PASSWORD

_APP = "mail-inbox"
_BUNDLE = Path(__file__).resolve().parents[1]
#: Made up per run: the installed copy of the bundle carries this file, so a literal would be
#: found in it by every "where is the password on disk" check below.
PASSWORD = secrets.token_hex(12)
SMTP_PASSWORD = secrets.token_hex(12)


@pytest.fixture
def home(_isolate_home):
    """The real bundle installed into the isolated home, nothing configured."""
    assert app_manager.install(_BUNDLE, confirm=True).ok
    yield _isolate_home
    app_manager.force_uninstall(_APP)  # a no-op once the test removed it


def _setup_answers(imap_password: str, smtp_password: str, *, reuse: str = "") -> list[str]:
    # confirm; imap host, port, username, address, folder; imap password; allowed senders;
    # configure smtp; smtp host, port, tls mode, username; smtp password; reuse the imap one
    return [
        "y", "imap.example.com", "993", "me@example.com", "", "", imap_password,
        "*@example.com", "y", "smtp.example.com", "587", "starttls", "", smtp_password, reuse,
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


def _configure_save(values: dict) -> None:
    """The Apps page's Configure → Save: core's PUT /api/apps/{name}/config handler."""

    class _Request(dict):
        match_info = {"name": _APP}

        async def json(self):
            return values

    resp = asyncio.run(api_app_config_put(_Request()))
    assert resp.status == 200, resp.text


def _hits(home: Path, needle: str) -> list[str]:
    """Every file under the home whose bytes contain ``needle``."""
    return sorted(
        str(p.relative_to(home))
        for p in home.rglob("*")
        if p.is_file() and not p.is_symlink() and needle.encode() in p.read_bytes()
    )


def _doctor(label: str):
    return next(line for line in cli_doctor.probe() if line.label == label)


def test_both_passwords_are_declared_sensitive():
    """The declaration is what masks them on every read route and renders a password input."""
    props = json.loads((_BUNDLE / "app.json").read_text())["provider"]["settingsSchema"]
    for field in ("password", "smtp_password"):
        assert props["properties"][field]["x-meta"]["sensitive"] is True


def test_setup_keeps_the_passwords_under_keys_this_app_owns(home):
    _run_setup(_setup_answers(PASSWORD, SMTP_PASSWORD))

    for secret in (PASSWORD, SMTP_PASSWORD):
        assert _hits(home, secret) == [".env"], "a password is somewhere besides the store"
    settings_file = ProviderSettings.config_path(_APP).read_text()
    assert settings_file.count("{{secret:PCSECRET_APP_") == 2
    names = credentials.credential_names()
    assert CRED_MAIL_PASSWORD not in names and CRED_SMTP_PASSWORD not in names
    # The source runs on them, and the doctor agrees with it.
    assert MailInboxProvider._resolve_password() == PASSWORD
    assert MailInboxProvider._resolve_smtp_password() == SMTP_PASSWORD
    assert _doctor("password").status == "ok"


def test_reusing_the_imap_password_copies_it_into_the_smtp_setting(home):
    _run_setup(_setup_answers(PASSWORD, "", reuse="y"))

    assert MailInboxProvider._resolve_smtp_password() == PASSWORD
    assert ProviderSettings.config_path(_APP).read_text().count("{{secret:PCSECRET_APP_") == 2
    assert CRED_SMTP_PASSWORD not in credentials.credential_names()


def test_uninstalling_removes_the_passwords_setup_saved(home):
    _run_setup(_setup_answers(PASSWORD, SMTP_PASSWORD))

    assert app_manager.uninstall_keep_data(_APP) is True

    assert _hits(home, PASSWORD) == [] and _hits(home, SMTP_PASSWORD) == []
    assert not [k for k in credentials.credential_names() if k.startswith("PCSECRET_")]


def test_a_password_saved_on_configure_reaches_the_source_and_uninstall_removes_it(home):
    _configure_save(
        {"host": "imap.example.com", "username": "me@example.com", "password": PASSWORD}
    )
    assert _hits(home, PASSWORD) == [".env"]
    assert MailInboxProvider._resolve_password() == PASSWORD
    assert _doctor("password").status == "ok"

    assert app_manager.uninstall_keep_data(_APP) is True

    assert _hits(home, PASSWORD) == []


def test_a_password_an_earlier_setup_saved_by_name_still_reads_and_the_setting_wins(home):
    """An install configured before this change keeps polling; the app's own setting wins."""
    credentials.save_credential(CRED_MAIL_PASSWORD, "saved-by-an-earlier-setup")
    credentials.save_credential(CRED_SMTP_PASSWORD, "smtp-saved-by-an-earlier-setup")
    assert MailInboxProvider._resolve_password() == "saved-by-an-earlier-setup"
    assert MailInboxProvider._resolve_smtp_password() == "smtp-saved-by-an-earlier-setup"

    ProviderSettings.update(_APP, {"password": PASSWORD, "smtp_password": SMTP_PASSWORD})

    assert MailInboxProvider._resolve_password() == PASSWORD
    assert MailInboxProvider._resolve_smtp_password() == SMTP_PASSWORD
