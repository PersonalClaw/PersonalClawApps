"""The menu surface: what a click actually calls, and what the badge shows.

These assert CALL SITES — that the Approve item fires ``on_resolve(id, "approve")`` and
the needs-input item opens the deep link — rather than that a resolve function exists.
"""

from __future__ import annotations

import json

from _ws_fakes import FakeOpener
from menubar_companion.app import build_companion
from menubar_companion.settings import Settings
from menubar_companion.tray import MenuItem, build_menu, headless_render, resolve_host

APPROVALS = [{"id": "a1", "tool": "bash", "summary": "rm -rf build"}]
LOOPS = {
    "loops": [
        {"id": "L1", "name": "ship it", "status": "running"},
        {"id": "L2", "name": "which db?", "status": "needs_input"},
    ]
}


def _companion(post: object = b'{"ok": true}'):
    opener = FakeOpener(
        {
            "/api/approvals": json.dumps(APPROVALS).encode(),
            "/api/loops": json.dumps(LOOPS).encode(),
            "/api/approvals/": post,
        }
    )
    companion = build_companion(
        Settings(url="http://127.0.0.1:10000", token="tok"),
        opener=opener,
        runner=lambda _argv: None,
    )
    companion.model.refresh()
    return companion, opener


def _titles(items: list[MenuItem]) -> list[str]:
    return [i.title for i in items]


def _find(items: list[MenuItem], needle: str) -> MenuItem:
    for item in items:
        if needle in item.title:
            return item
    raise AssertionError(f"no menu item matching {needle!r} in {_titles(items)}")


def _menu(companion, resolved=None, opened=None, muted_calls=None):
    # `x or []` would substitute a FRESH list for an empty one (an empty list is falsy),
    # silently throwing away everything the callbacks record. Bind the real lists once.
    resolved = [] if resolved is None else resolved
    opened = [] if opened is None else opened
    muted_calls = [] if muted_calls is None else muted_calls
    return build_menu(
        companion.model,
        companion.settings,
        on_resolve=lambda i, a: resolved.append((i, a)),
        on_toggle_mute=lambda: muted_calls.append(1),
        on_refresh=lambda: None,
        open_url=lambda u: opened.append(u),
    )


def test_the_menu_shows_approvals_needs_input_and_running():
    companion, _ = _companion()
    titles = _titles(_menu(companion))
    assert "Approvals waiting (1)" in titles
    assert "Needs your input (1)" in titles
    assert "Running (1)" in titles, "the needs-input loop is not double-counted as running"


def test_approve_and_deny_items_fire_the_write_with_the_right_action():
    companion, _ = _companion()
    resolved: list[tuple[str, str]] = []
    items = _menu(companion, resolved=resolved)

    _find(items, "Approve").action()
    _find(items, "Deny").action()

    assert resolved == [("a1", "approve"), ("a1", "deny")]


def test_the_needs_input_item_opens_the_loop_deep_link():
    companion, _ = _companion()
    opened: list[str] = []
    items = _menu(companion, opened=opened)

    _find(items, "which db?").action()

    assert opened == [f"{companion.client.base_url}/?token=tok#/loops/L2"]
    assert opened[0].endswith("#/loops/L2")


def test_a_failed_write_is_visible_in_the_menu_it_was_clicked_in():
    import io
    import urllib.error

    failure = urllib.error.HTTPError(
        "http://127.0.0.1:10000/api/approvals/a1/approve",
        503,
        "Service Unavailable",
        {},  # type: ignore[arg-type]
        io.BytesIO(b"{}"),
    )
    companion, _ = _companion(post=failure)
    assert companion.resolve("a1", "approve") is False

    titles = _titles(_menu(companion))
    warning = [t for t in titles if t.startswith("⚠")]
    assert warning and "503" in warning[0], titles
    assert "Approvals waiting (1)" in titles, "the row did not optimistically vanish"

    # Vacuity floor: no warning line exists when nothing failed.
    clean, _ = _companion()
    assert not [t for t in _titles(_menu(clean)) if t.startswith("⚠")]


def test_the_settings_item_toggles_mute_and_relabels():
    companion, _ = _companion()
    assert "Settings: Mute notifications" in _titles(_menu(companion))

    companion.toggle_mute()

    assert companion.settings.notifications_muted is True
    assert "Settings: Unmute notifications" in _titles(_menu(companion))


def test_muting_suppresses_notifications_for_new_items():
    posted: list[list[str]] = []
    opener = FakeOpener(
        {
            "/api/approvals": json.dumps(APPROVALS).encode(),
            "/api/loops": json.dumps(LOOPS).encode(),
        }
    )
    companion = build_companion(
        Settings(url="http://127.0.0.1:10000", token="tok"),
        opener=opener,
        runner=posted.append,
    )
    companion.notifier._osascript = "/usr/bin/osascript"  # pretend macOS

    companion.refresh_and_notify()
    assert companion.notifier.posted == 2, "one new approval + one new needs-input loop"

    companion.toggle_mute()
    companion.model._seen.clear()  # make the same items look new again
    companion.refresh_and_notify()
    assert companion.notifier.posted == 2, "muted: nothing further was posted"
    assert companion.notifier.suppressed == 2


def test_the_badge_on_the_status_item_is_the_derived_count():
    companion, _ = _companion()
    assert companion.model.badge_text == "2"
    text = headless_render(companion.model, companion.settings)
    assert text.splitlines()[0] == "PersonalClaw • badge 2"
    assert "Approvals waiting: 1" in text and "Needs your input: 1" in text


def test_the_status_item_host_is_reported_rather_than_assumed():
    """Whatever this machine has, ``resolve_host`` answers without raising."""
    host, reason = resolve_host()
    if host is None:
        assert "no macOS status-item backend" in reason
        assert "pip install --user rumps" in reason
    else:
        assert reason == ""
