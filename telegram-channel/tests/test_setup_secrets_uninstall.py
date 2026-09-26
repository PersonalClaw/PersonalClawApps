"""The bot token belongs to this app: kept under a key the app owns, and gone once it is uninstalled.

``personalclaw setup`` saved the token to the shared credential store under the plain name
``TELEGRAM_BOT_TOKEN``. No uninstall can attribute a plain name to an app, so removing the app
left its token behind. Setup now writes the token through ``ProviderSettings``, which keeps the
value in the credential store under a key this app owns and puts only a reference in the
settings file (core #3607), and both removal rungs purge those keys.

Driven end to end on the real bundle: installed with core's own installer into the isolated
home, setup run with a ``SetupContext`` wired the way ``personalclaw setup`` wires it (the real
credential store, the real ``ProviderSettings``), the transport built the way the registry
builds it, and core's own keep-data uninstall, which is the Apps page's Uninstall.
"""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest

from personalclaw.apps import app_manager
from personalclaw.config import credentials
from personalclaw.dashboard.handlers.apps import api_app_config_put
from personalclaw.providers.settings import ProviderSettings
from personalclaw.sdk.channel import CRED_OWNER_ID
from personalclaw.sdk.cli import SetupContext

import cli_doctor
import cli_setup
from telegram_runtime.settings import CRED_TELEGRAM_BOT_TOKEN
from telegram_runtime.transport import create_provider

_APP = "telegram-channel"
_BUNDLE = Path(__file__).resolve().parents[1]
#: Made up per run: the installed copy of the bundle carries this file, so a literal would be
#: found in it by every "where is the token on disk" check below.
TOKEN = f"123456:{secrets.token_hex(12)}"


@pytest.fixture
def home(_isolate_home, monkeypatch):
    """The real bundle installed into the isolated home, nothing configured."""
    for key in (CRED_TELEGRAM_BOT_TOKEN, CRED_OWNER_ID):
        # setenv first so teardown restores the variable even if the code under test sets it.
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
    assert app_manager.install(_BUNDLE, confirm=True).ok
    yield _isolate_home
    app_manager.force_uninstall(_APP)  # a no-op once the test removed it


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


def test_setup_keeps_the_token_under_a_key_this_app_owns(home):
    _run_setup(["y", TOKEN, "", ""])

    assert _hits(home, TOKEN) == [".env"], "the token is in the credential store and nowhere else"
    settings_file = ProviderSettings.config_path(_APP).read_text()
    assert "{{secret:PCSECRET_APP_" in settings_file and TOKEN not in settings_file
    assert CRED_TELEGRAM_BOT_TOKEN not in credentials.credential_names()
    # The channel runs on it: the transport the registry builds, and doctor.
    assert create_provider(ProviderSettings.load(_APP))._token() == TOKEN
    assert {line.label: line.status for line in cli_doctor.probe()}["token"] == "ok"


def test_uninstalling_removes_the_token_setup_saved(home):
    _run_setup(["y", TOKEN, "", ""])

    assert app_manager.uninstall_keep_data(_APP) is True

    assert _hits(home, TOKEN) == [], "the uninstalled app's token survived"
    assert not [k for k in credentials.credential_names() if k.startswith("PCSECRET_")]


@pytest.mark.asyncio
async def test_uninstalling_removes_a_token_saved_on_configure(home):
    await _configure_save({"bot_token": TOKEN})
    assert _hits(home, TOKEN) == [".env"]

    assert app_manager.uninstall_keep_data(_APP) is True

    assert _hits(home, TOKEN) == []
