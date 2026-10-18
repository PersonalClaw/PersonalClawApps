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

import copy
import json
import secrets
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from personalclaw.apps import app_manager
from personalclaw.config import credentials
from personalclaw.dashboard.handlers.apps import register_app_routes
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


# --- scan once per file: begin (kept the same in slack-channel's copy of this file) ---
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
def home(_isolate_home, monkeypatch, scans):
    """The real bundle installed into the isolated home, nothing configured."""
    for key in (CRED_IMAP_PASS, CRED_SMTP_PASS):
        # setenv first so teardown restores the variable even if the code under test sets it.
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
    scans.use(monkeypatch)
    assert app_manager.install(_BUNDLE, confirm=True).ok
    yield _isolate_home
    app_manager.force_uninstall(_APP)  # a no-op once the test removed it


def _gmail_setup(imap_password: str, smtp_password: str = "") -> list[str]:
    # confirm, provider, address, imap user, imap host, imap port, imap ssl, folder,
    # smtp user, smtp host, smtp port, security, imap password, smtp password,
    # poll secs, activation
    return [
        "y", "gmail", "bot@example.com", "", "", "", "", "", "", "", "", "",
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
