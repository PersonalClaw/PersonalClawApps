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

import copy
import secrets
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from personalclaw.apps import app_manager
from personalclaw.config import credentials
from personalclaw.dashboard.handlers.apps import register_app_routes
from personalclaw.providers.settings import ProviderSettings
from personalclaw.sdk.channel import CRED_SLACK_APP_TOKEN, CRED_SLACK_BOT_TOKEN, owner_id_credential
from personalclaw.sdk.cli import SetupContext

import cli_doctor
import cli_setup
from slack_runtime.transport import create_provider

_APP = "slack-channel"
_BUNDLE = Path(__file__).resolve().parents[1]
#: Made up per run: the installed copy of the bundle carries this file, so a literal would be
#: found in it by every "where is the token on disk" check below.
BOT = f"xoxb-{secrets.token_hex(12)}"
APP_TOKEN = f"fake-app-token-1{secrets.token_hex(12)}"


# --- scan once per file: begin (kept the same in email-channel's copy of this file) ---
class _Remembering:
    """Stands in for core's ``default_scanner`` while a test runs; scans through the real one."""

    def __init__(self, owner, real):
        self._owner = owner
        self._real = real

    def scan(self, staged_dir, *args, **kwargs):
        return self._owner._scan(self._real, Path(staged_dir), args, kwargs)

    def __getattr__(self, name):
        # Anything else the installer asks of the scanner goes to the real one, unchanged.
        return getattr(self._real, name)


class ScanOnce:
    """Core's install scan, run once per staged bundle's bytes for one test file.

    Each install in this file runs core's real installer; only its supply-chain scan, the slow
    part, is remembered by what was scanned: core's own digest of the staged bundle (every path
    and byte) plus the scan's other arguments. A repeat on the same bytes gets a copy of the
    first report; a bundle changed in any file is scanned afresh. Staging, the signature check,
    consent, hooks, writing the home and uninstalling all stay real.
    ``.github/tests/test_install_scan_once.py`` checks this copy and the other one.
    """

    def __init__(self):
        self._reports = {}
        #: How many installs ran core's real scan, and how many reused a report.
        self.real_scans = 0
        self.reused = 0

    def use(self, monkeypatch):
        """Route core's installer through this file's reports for the current test."""
        real = app_manager.default_scanner
        if not isinstance(real, _Remembering):
            monkeypatch.setattr(app_manager, "default_scanner", _Remembering(self, real))

    def _scan(self, real, staged, args, kwargs):
        from personalclaw.apps.disclosure import bundle_digest

        key = (bundle_digest(staged), repr(args), repr(sorted(kwargs.items())))
        if key in self._reports:
            self.reused += 1
            # A copy: the installer sets the report's signature on what it is handed.
            return copy.deepcopy(self._reports[key])
        report = real.scan(staged, *args, **kwargs)
        self.real_scans += 1
        self._reports[key] = copy.deepcopy(report)  # as the scan returned it
        return report
# --- scan once per file: end ---


@pytest.fixture(scope="module")
def scans():
    """Core's install scan of this bundle, run once for this file and reused while its bytes
    are the same (``ScanOnce`` above)."""
    return ScanOnce()


@pytest.fixture
def home(tmp_path, monkeypatch, scans):
    """The real bundle installed into a scratch home, nothing configured."""
    monkeypatch.setenv("PERSONALCLAW_HOME", str(tmp_path))
    for key in (CRED_SLACK_BOT_TOKEN, CRED_SLACK_APP_TOKEN, owner_id_credential("slack")):
        # setenv first so teardown restores the variable even if the code under test sets it.
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
    scans.use(monkeypatch)
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
    """The Apps page's Configure → Save, over core's own routes: read the settings, then save
    ``values`` over the revision that read reported. The save replaces the whole file, so it
    names the copy it replaces (``If-Match``), as the page does."""
    app = web.Application()
    register_app_routes(app)
    async with TestClient(TestServer(app)) as client:
        read = await client.get(f"/api/apps/{_APP}/config")
        assert read.status == 200, await read.text()
        revision = (await read.json())["revision"]
        resp = await client.put(
            f"/api/apps/{_APP}/config", json=values, headers={"If-Match": f'"{revision}"'}
        )
        assert resp.status == 200, await resp.text()


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
