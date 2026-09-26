"""Put the app dir on sys.path so app tests import the ``slack_runtime`` package
the way the gateway's app loader does at runtime.

Also hosts the slack-suite autouse fixtures that used to live in the CORE test
conftest (moved here with the slack-internal tests so the core suite runs on a
standalone clone with no sibling apps/ directory), and the SDK contract rail that
makes every test in this suite check the app's calls into core."""

import asyncio
import collections
import sys
from pathlib import Path

import pytest

_APP_DIR = Path(__file__).resolve().parents[1]
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))
if str(_APP_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_APP_DIR.parent))  # the repo root, for ``apps_testkit``

from apps_testkit import sdk_contract  # noqa: E402
from slack_runtime.handler import _PHASE_EMOJIS, _build_phase_emojis  # noqa: E402

#: Every app→SDK call site the suite drove, across all tests (reported once, at the end).
_SDK_CALLS_SEEN: collections.Counter = collections.Counter()


@pytest.fixture(autouse=True)
def _sdk_calls_conform(monkeypatch):
    """Every call this app makes into ``personalclaw.sdk.*`` during a test must conform to the
    INSTALLED core's signature and annotations — the check core #3599 needed.

    That change kept ``compress_thread_history``'s arity and swapped its first parameter's type,
    so every import resolved, every call bound, and the one test that reached the call passed
    because the ``TypeError`` was swallowed by the handler. A swallowed error is invisible to an
    assertion about behaviour, so this rail records the non-conformance where it happens and
    fails the test after the fact, whatever the code under test did with the exception.
    """
    with sdk_contract.record_sdk_calls(_APP_DIR, monkeypatch) as recorder:
        yield recorder
    _SDK_CALLS_SEEN.update(recorder.seen)
    if recorder.violations:
        pytest.fail(
            "the app called the installed SDK against its declared contract:\n  "
            + "\n  ".join(recorder.violations),
            pytrace=False,
        )


def pytest_terminal_summary(terminalreporter):
    """Say how much of the SDK surface the suite actually drove, so a green run is legible."""
    sites = {(path, line) for path, line, _target in _SDK_CALLS_SEEN}
    targets = {target for _path, _line, target in _SDK_CALLS_SEEN}
    static = sdk_contract.census(_APP_DIR)
    terminalreporter.write_line(
        f"SDK contract: {sum(_SDK_CALLS_SEEN.values())} app→SDK calls checked at runtime across "
        f"{len(sites)} call sites and {len(targets)} SDK callables; {len(static.calls)} call sites "
        "bind statically (test_sdk_contract.py)"
    )


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
def _isolate_channel_trust(tmp_path_factory, monkeypatch):
    """Point core's channel_trust entity store (and PERSONALCLAW_HOME, which its
    SEL audit rows resolve through) at a per-test tmp dir. The EA-7 write-throughs
    fire on owner actions (Allow/Deny/Track buttons, owner claim, thread linking),
    so without this every such test would write the REAL ~/.personalclaw."""
    home = tmp_path_factory.mktemp("pclaw-trust")
    monkeypatch.setattr(
        "personalclaw.providers.entity_routes._entity_settings_path",
        lambda entity: home / "entity_settings" / f"{entity}.json",
    )
    monkeypatch.setenv("PERSONALCLAW_HOME", str(home))
    # A saved token goes through core's credential store, whose reads consult the OS keychain
    # whenever `keyring` is importable. Keep every test off the real one.
    monkeypatch.setattr("personalclaw.config.credentials._usable_keyring", lambda: None)
    yield


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
