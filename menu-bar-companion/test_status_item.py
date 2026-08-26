"""The macOS status-item render path, driven against a STUBBED ``rumps``.

This is the repo's own convention for a dependency CI cannot install (the model apps stub
their vendor SDKs into ``sys.modules`` for exactly this reason). It does not prove pixels
appear in a real menu bar — nothing automated can — but it does prove the path is not dead
code: that ``resolve_host`` finds a backend when one exists, that the badge reaches the
status-item title, and that clicking the Approve item reaches the write.

The remaining unverified clause is narrow and named: a real ``NSStatusItem`` drawn by real
PyObjC on a real Mac with a window server. See the README's "status-item host" section.
"""

from __future__ import annotations

import json
import sys
import types

import pytest
from _ws_fakes import FakeOpener
from menubar_companion.app import build_companion
from menubar_companion.settings import Settings
from menubar_companion.tray import resolve_host

APPROVALS = [{"id": "a1", "tool": "bash"}]
LOOPS = {"loops": [{"id": "L2", "name": "which db?", "status": "needs_input"}]}


class _FakeMenu(list):
    def clear(self) -> None:
        del self[:]

    def add(self, item) -> None:
        self.append(item)


def _fake_rumps() -> types.ModuleType:
    """The subset of ``rumps`` this app uses: ``App``, ``MenuItem``, ``Timer``.

    Keeping the stub to exactly those three is itself a check — if ``run.py`` starts
    reaching for a fourth attribute, this raises ``AttributeError`` instead of silently
    passing while the real host would break.
    """
    mod = types.ModuleType("rumps")

    class MenuItem:
        def __init__(self, title, callback=None):
            self.title = title
            self.callback = callback

    class Timer:
        instances: list["Timer"] = []

        def __init__(self, fn, interval):
            self.fn, self.interval, self.started = fn, interval, False
            Timer.instances.append(self)

        def start(self) -> None:
            self.started = True

        def fire(self) -> None:
            self.fn(self)

    class App:
        def __init__(self, name, quit_button=None):
            self.title = name
            self.quit_button = quit_button
            self.menu = _FakeMenu()
            self.ran = False

        def run(self) -> None:
            self.ran = True  # a real rumps App.run() blocks forever; return instead

    mod.App, mod.MenuItem, mod.Timer = App, MenuItem, Timer  # type: ignore[attr-defined]
    return mod


@pytest.fixture
def stub_rumps(monkeypatch):
    mod = _fake_rumps()
    monkeypatch.setitem(sys.modules, "rumps", mod)
    return mod


def _companion():
    opener = FakeOpener(
        {
            "/api/approvals": json.dumps(APPROVALS).encode(),
            "/api/loops": json.dumps(LOOPS).encode(),
            "/api/approvals/": b'{"ok": true}',
        }
    )
    companion = build_companion(
        Settings(url="http://127.0.0.1:10000", token="tok"),
        opener=opener,
        runner=lambda _argv: None,
    )
    companion.model.refresh()
    return companion, opener


def test_resolve_host_finds_a_backend_when_one_is_importable(stub_rumps):
    host, reason = resolve_host()
    assert host is stub_rumps
    assert reason == ""


def test_resolve_host_reports_a_reason_when_the_backend_is_missing(monkeypatch):
    """Vacuity floor for the test above: the same call answers the other way."""
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

    def blocked(name, *a, **kw):
        if name == "rumps":
            raise ModuleNotFoundError("No module named 'rumps'")
        return real_import(name, *a, **kw)

    monkeypatch.setitem(sys.modules, "rumps", None)
    monkeypatch.setattr("builtins.__import__", blocked)
    host, reason = resolve_host()
    assert host is None
    assert "no macOS status-item backend" in reason


def test_the_status_item_title_carries_the_derived_badge(stub_rumps, monkeypatch):
    import run as launcher

    companion, _ = _companion()
    # The socket + floor poll belong to the live app, not to this render assertion.
    monkeypatch.setattr(launcher, "start_background", lambda _c: [])
    captured: list = []
    real_app = stub_rumps.App

    class Recording(real_app):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            captured.append(self)

    monkeypatch.setattr(stub_rumps, "App", Recording)
    assert launcher._run_status_item(stub_rumps, companion, companion.settings) == 0

    (timer,) = stub_rumps.Timer.instances
    assert timer.started and timer.interval == 1, "the redraw timer is armed"
    (app,) = captured
    assert app.ran, "the host loop was entered"
    # 1 approval + 1 needs-input loop = the same number the model derives.
    assert companion.model.badge == 2
    assert app.title == "PersonalClaw 2"

    titles = [i.title for i in app.menu]
    assert "Approvals waiting (1)" in titles
    assert any(t.strip() == "Approve" for t in titles)
    assert any(t.strip() == "Deny" for t in titles)
    assert "Settings: Mute notifications" in titles

    # Clicking Approve reaches the write (rumps passes the sender as the first arg).
    approve = next(i for i in app.menu if i.title.strip() == "Approve")
    approve.callback(approve)
    assert companion.model.last_error == "", "the stubbed POST succeeded"

    # And the redraw is revision-driven: a change bumps the revision the timer watches.
    before = companion.revision
    companion.toggle_mute()
    assert companion.revision > before
