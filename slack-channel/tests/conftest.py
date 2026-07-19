"""Put the app dir on sys.path so app tests import the ``slack_runtime`` package
the way the gateway's app loader does at runtime.

Also hosts the slack-suite autouse fixtures that used to live in the CORE test
conftest (moved here with the slack-internal tests so the core suite runs on a
standalone clone with no sibling apps/ directory)."""

import asyncio
import sys
from pathlib import Path

import pytest

_APP_DIR = Path(__file__).resolve().parents[1]
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

from slack_runtime.handler import _PHASE_EMOJIS, _build_phase_emojis  # noqa: E402


@pytest.fixture(autouse=True)
def _ensure_event_loop():
    """Ensure a current event loop exists for code that constructs asyncio
    primitives (e.g. Semaphore) at import/init time outside a running loop."""
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


@pytest.fixture(autouse=True)
def _isolate_session_map(tmp_path_factory, monkeypatch):
    """Point the SESSION MAP at a per-test tmp dir so nothing touches the real
    ~/.personalclaw/session_map.json (SessionManager construction rewrites it)."""
    map_home = tmp_path_factory.mktemp("pclaw-sessmap")
    monkeypatch.setattr("personalclaw.session_map.config_dir", lambda: map_home)


@pytest.fixture(autouse=True)
def _isolate_migration_marker(tmp_path_factory, monkeypatch):
    """Point migrate_from_core's done-marker FILE at a per-test tmp path,
    pre-created so any unpatched SlackSettings.load() short-circuits the
    migration (never reads the real core config.json or touches the real
    app data dir). Migration tests re-patch _migration_marker_path themselves."""
    marker = tmp_path_factory.mktemp("pclaw-migmark") / ".core_migration_done"
    marker.touch()
    monkeypatch.setattr("slack_runtime.settings._migration_marker_path", lambda: marker)


@pytest.fixture(autouse=True)
def _reset_trust_mode():
    """Reset the process-global YOLO/auto-approve trust state around every test
    (``personalclaw.trust_mode`` is a deliberate process singleton)."""
    import importlib

    _tm = importlib.import_module("personalclaw.trust_mode")
    _tm._TRUST.disable()
    yield
    _tm._TRUST.disable()


@pytest.fixture(autouse=True)
def _enterprise_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set a default validated team_id so _route_message doesn't reject messages."""
    monkeypatch.setattr("slack_runtime.enterprise._validated_team_id", "TTEST")
    monkeypatch.setattr("slack_runtime.enterprise._validated_enterprise_id", "ETEST")


@pytest.fixture(autouse=True)
def _clean_emojis():
    """Reset _PHASE_EMOJIS to defaults before each test (suppresses local config)."""
    original = dict(_PHASE_EMOJIS)
    _PHASE_EMOJIS.clear()
    _PHASE_EMOJIS.update(_build_phase_emojis({})[0])
    yield
    _PHASE_EMOJIS.clear()
    _PHASE_EMOJIS.update(original)


@pytest.fixture(autouse=True)
def _reset_slack_allowlist():
    """Reset the Slack handler's module-global allowlist/owner/channel state around
    every test. These are process-globals (owner-claim, tracked channels, open
    channels) that otherwise leak across test files and skew message-routing tests."""
    import slack_runtime.handler as h

    saved = (h._owner_id, set(h._allowed_users), set(h._tracking_channels), set(h._open_channels))
    h._owner_id = ""
    h._allowed_users = set()
    h._tracking_channels = set()
    h._open_channels = set()
    yield
    h._owner_id, _au, _tc, _oc = saved
    h._allowed_users = _au
    h._tracking_channels = _tc
    h._open_channels = _oc
