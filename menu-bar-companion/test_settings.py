"""Local settings, the Settings-menu mute switch, and where state is allowed to live."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from menubar_companion import settings as settings_mod
from menubar_companion.notify import Notifier
from menubar_companion.settings import Settings, companion_home, settings_path


def test_state_lives_under_the_env_home_and_nowhere_else(_isolate_companion_home):
    home = _isolate_companion_home
    assert companion_home() == Path(home)
    settings = Settings(url="http://127.0.0.1:10000", token="tok")
    written = settings.save()
    assert written == Path(home) / "settings.json"
    # The default (unset) location is the user's own Library dir, never ~/.personalclaw:
    # this app is client-installed, so the platform never grants it a DATA_DIR.
    assert "Library/Application Support" in settings_mod._DEFAULT_HOME
    assert ".personalclaw" not in settings_mod._DEFAULT_HOME


def test_the_settings_file_is_0600_because_it_holds_a_gateway_token():
    path = Settings(url="http://h:1", token="secret-token").save()
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, oct(mode)
    assert "secret-token" in path.read_text(encoding="utf-8")


def test_mute_round_trips_through_the_file():
    settings = Settings(url="http://h:1", token="t")
    settings.save()
    assert settings.notifications_muted is False

    settings.set_muted(True)
    assert Settings.load().notifications_muted is True

    settings.set_muted(False)
    assert Settings.load().notifications_muted is False


def test_toggling_mute_does_not_persist_an_env_supplied_token(monkeypatch):
    """A preference click must not quietly write a credential the user kept out of the file."""
    monkeypatch.setenv("PERSONALCLAW_COMPANION_URL", "http://env-host:10000")
    monkeypatch.setenv("PERSONALCLAW_COMPANION_TOKEN", "env-only-token")
    settings = Settings.load()
    assert settings.token == "env-only-token"

    settings.set_muted(True)

    on_disk = json.loads(settings_path().read_text(encoding="utf-8"))
    assert on_disk == {"notifications_muted": True, "poll_seconds": 60}
    assert "env-only-token" not in settings_path().read_text(encoding="utf-8")
    # Vacuity floor: the full save DOES persist it, so the assertion above is about
    # save_preferences specifically and not about the token being unpersistable.
    settings.save()
    assert "env-only-token" in settings_path().read_text(encoding="utf-8")


def test_an_env_value_wins_over_the_file(monkeypatch):
    Settings(url="http://file-host:1", token="file-token").save()
    assert Settings.load().url == "http://file-host:1"
    monkeypatch.setenv("PERSONALCLAW_COMPANION_URL", "http://env-host:2")
    assert Settings.load().url == "http://env-host:2"


def test_a_corrupt_preferences_file_still_starts():
    settings_path().parent.mkdir(parents=True, exist_ok=True)
    settings_path().write_text("{not json", encoding="utf-8")
    loaded = Settings.load()
    assert loaded.url == "" and loaded.notifications_muted is False


# ── the mute actually suppresses ──


def test_muting_suppresses_the_notification_at_the_single_post_site():
    muted = {"on": False}
    calls: list[list[str]] = []
    notifier = Notifier(lambda: muted["on"], runner=calls.append, osascript="/usr/bin/osascript")

    assert notifier.post("Approval needed", "bash") is True
    assert len(calls) == 1 and notifier.posted == 1

    muted["on"] = True
    assert notifier.post("Approval needed", "bash") is False
    assert len(calls) == 1, "nothing was run while muted"
    assert notifier.suppressed == 1

    # Live check, not a snapshot: unmuting works on the same object.
    muted["on"] = False
    assert notifier.post("Approval needed", "bash") is True
    assert len(calls) == 2


def test_notification_text_is_quoted_before_it_reaches_osascript():
    calls: list[list[str]] = []
    notifier = Notifier(lambda: False, runner=calls.append, osascript="/usr/bin/osascript")
    notifier.post('a "loop" name', 'body with " quote and \\ backslash')
    (argv,) = calls
    script = argv[2]
    assert '\\"loop\\"' in script, "a quote in a loop name is escaped, not script syntax"
    assert "\\\\" in script, "a backslash is escaped too, or it eats the next character"
    # Removing the escaped sequences must leave exactly the four delimiters of the two
    # string literals — i.e. no user text escaped the literal it belongs to.
    bare = script.replace("\\\\", "").replace('\\"', "")
    assert bare.count('"') == 4, bare


def test_no_osascript_means_silent_rather_than_crashing():
    notifier = Notifier(lambda: False, runner=lambda _a: None, osascript="")
    assert notifier.post("t", "b") is False
    assert notifier.suppressed == 1
