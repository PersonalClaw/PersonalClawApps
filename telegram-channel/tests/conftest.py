"""Put the app dir on sys.path so app tests import the ``telegram_runtime`` package
the way the gateway's app loader does at runtime, and pin an isolated home.

Every core surface this app touches — ``ProviderSettings`` (the app store),
``channel_trust`` (the trust store), ``AppConfig`` (the credential store) — routes
its path through ``config_dir()``, which re-reads ``PERSONALCLAW_HOME`` live on each
call. Pointing that at a per-test tmp dir isolates all of them at once, so nothing
touches the real ``~/.personalclaw`` (the lesson: patching ``config_dir`` alone
misses import-bound stores — set the env)."""

import sys
from pathlib import Path

import pytest

_APP_DIR = Path(__file__).resolve().parents[1]
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path_factory, monkeypatch):
    """Point PERSONALCLAW_HOME at a per-test tmp dir so the app store, trust store
    and credential store all resolve under it, never the real home."""
    home = tmp_path_factory.mktemp("pclaw-tg-home")
    monkeypatch.setenv("PERSONALCLAW_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def _live_writes_baseline(monkeypatch):
    """Pin the live-writes kill switch OFF as the suite-wide baseline.

    ``transport.send()`` returns a typed :class:`SendRefused` (falsy, not a bool) while
    ``PERSONALCLAW_DISABLE_LIVE_WRITES`` is set, and core's channel conformance kit
    asserts ``send()`` returns a ``bool``. Both are correct in their own frame, so the
    guard state has to be an EXPLICIT precondition rather than whatever the ambient
    environment happens to carry: a developer (or a CI job) exporting the var would
    otherwise turn the conformance clause red for a reason unrelated to the change
    being tested. Tests that want the guard ON set it themselves with monkeypatch."""
    monkeypatch.delenv("PERSONALCLAW_DISABLE_LIVE_WRITES", raising=False)


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """Drop the module-global settings cache around every test (it's a deliberate
    process singleton, refreshed on write — reset it so one test's activation mode
    can't leak into the next)."""
    from telegram_runtime import settings as s

    s._settings = None
    yield
    s._settings = None
