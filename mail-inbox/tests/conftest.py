"""Put the app dir on sys.path so app tests import ``mail_inbox_runtime`` the way the
gateway's app loader does at runtime, and pin an isolated home.

Every core surface this app touches — ``ProviderSettings`` (the app store),
``AppConfig`` (the credential store), ``app_data_dir`` (the dedup file), and ``sel()``
(the security log) — routes its path through ``config_dir()`` / ``PERSONALCLAW_HOME``,
re-read live on each call. Pointing that at a per-test tmp dir isolates all of them at
once, so nothing touches the real ``~/.personalclaw`` (patching ``config_dir`` alone
misses import-bound stores — set the env).
"""

import sys
from pathlib import Path

import pytest

_APP_DIR = Path(__file__).resolve().parents[1]
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path_factory, monkeypatch):
    """Point PERSONALCLAW_HOME at a per-test tmp dir so the app store, credential store,
    data dir and SEL log all resolve under it, never the real home."""
    home = tmp_path_factory.mktemp("pclaw-mail-home")
    monkeypatch.setenv("PERSONALCLAW_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """Drop the module-global settings cache around every test (a deliberate process
    singleton, refreshed on write) so one test's config can't leak into the next."""
    from mail_inbox_runtime import settings as s

    s._settings = None
    yield
    s._settings = None


@pytest.fixture(autouse=True)
def _reset_sel_singleton():
    """The SEL is a process singleton that pins its home on first init. Reset it around
    each test so it re-binds under this test's PERSONALCLAW_HOME (else the first test's
    home is reused for every later assertion).

    The SEL *class* isn't an SDK export, so reach it SDK-legally via ``type(sel())`` —
    ``sel`` is re-exported from ``personalclaw.sdk.channel``. Getting the class this way
    may spin up the current singleton, which we immediately discard."""
    from personalclaw.sdk.channel import sel

    cls = type(sel())
    cls._instance = None
    cls._initialized = False
    yield
    cls._instance = None
    cls._initialized = False
