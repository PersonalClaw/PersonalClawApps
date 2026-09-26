"""The Bot and App tokens belong to this app: kept under keys the app owns, gone once it is uninstalled.

``personalclaw setup`` saved both tokens to the shared credential store under the plain names
``SLACK_BOT_TOKEN`` / ``SLACK_APP_TOKEN``. No uninstall can attribute a plain name to an app, so
removing the app left both tokens behind. Setup now writes them through ``ProviderSettings``,
which keeps each value in the credential store under a key this app owns and puts only a
reference in the settings file (core #3607), and both removal rungs purge those keys.

Driven end to end on the real bundle: installed with core's own installer into a scratch home,
setup run with a ``SetupContext`` wired the way ``personalclaw setup`` wires it (the real
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
from personalclaw.sdk.channel import CRED_OWNER_ID, CRED_SLACK_APP_TOKEN, CRED_SLACK_BOT_TOKEN
from personalclaw.sdk.cli import SetupContext

import cli_doctor
import cli_setup
from slack_runtime.transport import create_provider

_APP = "slack-channel"
_BUNDLE = Path(__file__).resolve().parents[1]
#: Made up per run: the installed copy of the bundle carries this file, so a literal would be
#: found in it by every "where is the token on disk" check below.
BOT = f"xoxb-{secrets.token_hex(12)}"
APP_TOKEN = f"xapp-1-{secrets.token_hex(12)}"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """The real bundle installed into a scratch home, nothing configured."""
    monkeypatch.setenv("PERSONALCLAW_HOME", str(tmp_path))
    for key in (CRED_SLACK_BOT_TOKEN, CRED_SLACK_APP_TOKEN, CRED_OWNER_ID):
        # setenv first so teardown restores the variable even if the code under test sets it.
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
    assert app_manager.install(_BUNDLE, confirm=True).ok
    yield tmp_path
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


def test_setup_keeps_both_tokens_under_keys_this_app_owns(home):
    _run_setup(["y", APP_TOKEN, BOT, "", ""])

    for token in (BOT, APP_TOKEN):
        assert _hits(home, token) == [".env"], "a token is somewhere besides the credential store"
    settings_file = ProviderSettings.config_path(_APP).read_text()
    assert settings_file.count("{{secret:PCSECRET_APP_") == 2
    assert BOT not in settings_file and APP_TOKEN not in settings_file
    names = credentials.credential_names()
    assert CRED_SLACK_BOT_TOKEN not in names and CRED_SLACK_APP_TOKEN not in names
    # The channel runs on them: the transport the registry builds, and doctor.
    assert create_provider(ProviderSettings.load(_APP))._tokens() == (BOT, APP_TOKEN)
    assert {line.label: line.status for line in cli_doctor.probe()}["tokens"] == "ok"


def test_uninstalling_removes_the_tokens_setup_saved(home):
    _run_setup(["y", APP_TOKEN, BOT, "", ""])

    assert app_manager.uninstall_keep_data(_APP) is True

    assert _hits(home, BOT) == [] and _hits(home, APP_TOKEN) == [], "a token survived uninstall"
    assert not [k for k in credentials.credential_names() if k.startswith("PCSECRET_")]


@pytest.mark.asyncio
async def test_uninstalling_removes_tokens_saved_on_configure(home):
    await _configure_save({"bot_token": BOT, "app_token": APP_TOKEN})
    assert _hits(home, BOT) == [".env"] and _hits(home, APP_TOKEN) == [".env"]

    assert app_manager.uninstall_keep_data(_APP) is True

    assert _hits(home, BOT) == [] and _hits(home, APP_TOKEN) == []
