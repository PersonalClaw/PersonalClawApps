"""The menu surface: what a click actually calls, and what the badge shows.

These assert CALL SITES — that the Approve item fires ``on_resolve(id, "approve")`` and
the needs-input item opens the deep link — rather than that a resolve function exists.

And they assert what stands above Approve: the brief of the call, the same parts the
dashboard's approval card shows, each whole. A row whose brief cannot be read or cannot fit
offers "Review in PersonalClaw" instead, never a blind Approve.
"""

from __future__ import annotations

import json
import urllib.parse

import pytest
from _rows import DENY_ENDS_THE_TURN, REACH, approval, radius
from _ws_fakes import FakeOpener
from menubar_companion.app import build_companion
from menubar_companion.settings import Settings
from menubar_companion.tray import MenuItem, build_menu, headless_render, resolve_host

BASE = "http://127.0.0.1:10000"
TOKEN = "fake-owner-token"
APPROVALS = [approval(id="a1")]
QUESTION = "Should the release use the staging database or the production one?"
LOOPS = {
    "loops": [
        {"id": "L1", "name": "ship it", "status": "running"},
        {
            "id": "L2",
            "name": "which db?",
            "status": "needs_input",
            "pending_question": {"question": QUESTION, "ts": 1_700_000_000.0},
        },
    ]
}

#: What a row the menu cannot read shows, and what it offers.
UNREADABLE = "This menu can't read this approval's details."
REVIEW = "Review in PersonalClaw"


def _companion(approvals: list | None = None, post: object = b'{"ok": true}'):
    opener = FakeOpener(
        {
            "/api/approvals": json.dumps(APPROVALS if approvals is None else approvals).encode(),
            "/api/loops": json.dumps(LOOPS).encode(),
            "/api/approvals/": post,
        }
    )
    companion = build_companion(
        Settings(url=BASE, token=TOKEN),
        opener=opener,
        runner=lambda _argv: None,
    )
    companion.model.refresh()
    return companion, opener


def _titles(items: list[MenuItem]) -> list[str]:
    return [i.title for i in items]


def _walk(items):
    """Every item, and every item of every submenu, depth first."""
    for item in items:
        yield item
        yield from _walk(getattr(item, "children", ()))


def _find(items: list[MenuItem], needle: str) -> MenuItem:
    for item in _walk(items):
        if needle in item.title:
            return item
    raise AssertionError(f"no menu item matching {needle!r} in {_titles(items)}")


def _answer(menu: list[MenuItem], title: str) -> MenuItem:
    """The answer titled exactly *title* (a brief's own line may contain the word)."""
    (item,) = [i for i in menu if i.title == title]
    return item


def _approval_menu(items: list[MenuItem]) -> list[MenuItem]:
    """The submenu of the one approval in *items*: its brief, then its answers."""
    rows = [i for i in items if getattr(i, "children", ())]
    assert len(rows) == 1, f"expected one approval with a brief under it, got: {_titles(items)}"
    return list(rows[0].children)


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


# ── the brief stands above Approve ──


def test_the_brief_is_shown_before_approve():
    """Everything the dashboard's card shows about the call, in its order, then the answers."""
    row = approval(
        id="a1",
        tool_purpose="Clear the stale build cache",
        source_label="loop “Fix the README”",
        risk="destructive",
        blast_radius=radius(writes=True, shell=True),
        reach=REACH,
        deny_effect=DENY_ENDS_THE_TURN,
    )
    companion, _ = _companion([row])
    resolved: list[tuple[str, str]] = []
    items = _menu(companion, resolved=resolved)

    # Nothing answers an approval from the top of the menu: only from under its brief.
    assert not [i for i in items if i.title.strip() in ("Approve", "Deny")], _titles(items)
    (approval_row,) = [i for i in items if "bash" in i.title]
    assert approval_row.title == "  bash — from loop “Fix the README”"
    titles = _titles(_approval_menu(items))
    assert titles[-3:] == ["Approve", "Deny", REVIEW], titles
    brief = titles[:-3]
    assert brief[:5] == [
        "Permission needed to run bash",
        '  {"command": "rm -rf build/cache"}',
        "Clear the stale build cache",
        "From loop “Fix the README”",
        "Can: writes files, runs a command · Risk: Destructive",
    ], brief
    # Then where it reaches and what Deny does, each whole across its lines.
    assert " ".join(brief[5:]) == f"{REACH} {DENY_ENDS_THE_TURN}"
    assert all(len(line) <= 64 for line in brief), [len(line) for line in brief]

    # The brief's lines answer nothing; the answers do what they say.
    menu = _approval_menu(items)
    assert all(item.action is None for item in menu[:-3])
    _answer(menu, "Approve").action()
    _answer(menu, "Deny").action()
    assert resolved == [("a1", "approve"), ("a1", "deny")]


def test_a_brief_with_nothing_beyond_the_call_shows_only_what_it_has():
    """A part the row leaves empty is left out, never shown as a claim (no "no network")."""
    row = approval(id="a1", tool_purpose="", reach="", deny_effect="", blast_radius=None, risk="")
    companion, _ = _companion([row])
    titles = _titles(_approval_menu(_menu(companion)))
    assert titles == [
        "Permission needed to run bash",
        '  {"command": "rm -rf build/cache"}',
        "From chat “Release prep”",
        "Approve",
        "Deny",
        REVIEW,
    ], titles


UNREADABLE_ROWS: dict[str, dict[str, object]] = {
    "no tool": {"tool": ""},
    "no id": {"id": ""},
    "a purpose that is not text": {"tool_purpose": ["Clear the cache"]},
    "arguments that are a number": {"tool_input": 7},
    "a blast radius that is not one": {"blast_radius": "writes files"},
    "a blast radius missing facets": {"blast_radius": {"writes": True}},
    "a facet this menu has no words for": {"blast_radius": {**radius(writes=True), "erases": True}},
    "a risk this menu has no words for": {"risk": "severe"},
    "a deny effect that is not text": {"deny_effect": {"ends": True}},
}


@pytest.mark.parametrize("case", sorted(UNREADABLE_ROWS))
def test_a_brief_that_cannot_be_read_offers_only_the_review(case):
    """No Approve and no Deny: what the menu would say about the call could not be trusted."""
    row = {**approval(id="a1"), **UNREADABLE_ROWS[case]}
    companion, _ = _companion([row])
    opened: list[str] = []
    items = _menu(companion, opened=opened)

    menu = _approval_menu(items)
    assert _titles(menu) == [UNREADABLE, REVIEW], _titles(menu)
    assert not [i for i in _walk(items) if i.title.strip() in ("Approve", "Deny")]
    _answer(menu, REVIEW).action()
    assert opened == [f"{BASE}/#/companion?approval=a1" if row["id"] else f"{BASE}/#/companion"]
    # It is still waiting on you, whatever the menu can say about it.
    assert companion.model.badge == 2


def test_vacuity_floor_the_same_row_unbroken_offers_approve():
    """The floor for the cases above: the row they each break offers all three answers."""
    companion, _ = _companion([approval(id="a1")])
    assert _titles(_approval_menu(_menu(companion)))[-3:] == ["Approve", "Deny", REVIEW]


def test_arguments_too_long_for_the_menu_offer_the_review_instead_of_approve():
    """A part that cannot fit is said to be too long, never shown cut, and Approve goes."""
    script = "\n".join(f"echo step {n}" for n in range(1, 41))
    row = approval(id="a1", tool_input=script, deny_effect=DENY_ENDS_THE_TURN)
    companion, _ = _companion([row])
    titles = _titles(_approval_menu(_menu(companion)))

    assert "What it would run is too long to show here." in titles
    assert not [t for t in titles if "echo step" in t], "no part of a cut script is shown"
    assert "Approve" not in titles
    # Deny stays: what it does is shown whole, and declining is what it means.
    assert titles[-2:] == ["Deny", REVIEW], titles
    # The parts that fit are still there, whole.
    assert "Clear the stale build cache" in titles
    assert "From chat “Release prep”" in titles
    assert DENY_ENDS_THE_TURN in " ".join(titles)


def test_a_deny_effect_too_long_for_the_menu_takes_deny_away_too():
    row = approval(id="a1", deny_effect="Deny ends the agent's turn and every step after it. " * 12)
    companion, _ = _companion([row])
    titles = _titles(_approval_menu(_menu(companion)))
    assert "What Deny does is too long to show here." in titles
    assert "Approve" not in titles and "Deny" not in titles
    assert titles[-1] == REVIEW


@pytest.mark.parametrize(
    "arguments",
    [
        '{"command": "find build -name \\"*.log\\" -mtime +7 -print", "description": '
        '"List the build logs older than a week, so they can be cleared next"}',
        "set -e\n  cd build\n\tmake clean\necho done\necho done",
    ],
    ids=["a long one-line command", "a script with indents and a repeated line"],
)
def test_what_it_would_run_is_shown_whole_line_for_line(arguments):
    """Joining each line's pieces gives the arguments back exactly: nothing trimmed or dropped.

    A line wider than the menu goes on in the lines under it, each marked as continuing, so a
    line break in what will run never reads like a wrapped line.
    """
    companion, _ = _companion([approval(id="a1", tool_input=arguments)])
    shown = [t for t in _titles(_approval_menu(_menu(companion))) if t.startswith(("  ", "↪ "))]
    assert shown, "no argument lines: this test would pass vacuously"
    rebuilt: list[str] = []
    for line in shown:
        if line.startswith("↪ "):
            rebuilt[-1] += line[2:]
        else:
            rebuilt.append(line[2:])
    assert "\n".join(rebuilt) == arguments.expandtabs(4)
    assert all(len(line) <= 64 for line in shown)


def test_text_that_prints_nothing_is_shown_as_its_code_point():
    """A character that would print nothing is shown as text; ordinary text stays as written."""
    row = approval(
        id="a1",
        tool_input='{"path": "build\u200b/cache"}',
        tool_purpose="Vider le cache “déjà” construit",
    )
    companion, _ = _companion([row])
    titles = _titles(_approval_menu(_menu(companion)))
    assert '  {"path": "build\\u200b/cache"}' in titles, titles
    assert "Vider le cache “déjà” construit" in titles


# ── what the menu opens ──


def test_the_needs_input_item_opens_the_loop_deep_link():
    companion, _ = _companion()
    opened: list[str] = []
    items = _menu(companion, opened=opened)

    _find(items, "which db?").action()

    assert opened == [f"{BASE}/#/loops/L2"]


def test_no_link_the_menu_opens_carries_the_token():
    """Each link opens the dashboard with no credential in it, before the route or after."""
    companion, _ = _companion([approval(id="dashboard:chat-1:call-1")])
    opened: list[str] = []
    items = _menu(companion, opened=opened)
    for item in _walk(items):
        if item.url:
            item.action()

    assert sorted(opened) == [
        f"{BASE}/#/companion?approval=dashboard%3Achat-1%3Acall-1",
        f"{BASE}/#/loops/L2",
    ], opened
    for url in opened:
        assert TOKEN not in url, url
        assert urllib.parse.urlsplit(url).query == "", url


def test_the_menu_shows_what_a_loop_asks_under_it():
    companion, _ = _companion()
    titles = _titles(_menu(companion))
    at = titles.index("  which db?")
    assert titles[at + 1 : at + 3] == [
        "    Should the release use the staging database or the production",
        "    one?",
    ], titles


# ── writes, mute and the badge ──


def test_a_failed_write_is_visible_in_the_menu_it_was_clicked_in():
    import io
    import urllib.error

    failure = urllib.error.HTTPError(
        f"{BASE}/api/approvals/a1/approve",
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
        Settings(url=BASE, token=TOKEN),
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


def test_the_headless_menu_prints_the_brief_and_the_question_and_never_the_token():
    """``--check`` prints what the menu would show: the same brief, the same answers."""
    companion, _ = _companion()
    text = headless_render(companion.model, companion.settings)
    assert "      Permission needed to run bash" in text
    assert "      Can: writes files, runs a command · Risk: Destructive" in text
    assert f"[Approve] [Deny] [{REVIEW} → {BASE}/#/companion?approval=a1]" in text
    assert f"  • which db? → {BASE}/#/loops/L2" in text
    assert "      Should the release use the staging database or the production" in text
    assert TOKEN not in text


def test_the_status_item_host_is_reported_rather_than_assumed():
    """Whatever this machine has, ``resolve_host`` answers without raising."""
    host, reason = resolve_host()
    if host is None:
        assert "no macOS status-item backend" in reason
        assert "pip install --user rumps" in reason
    else:
        assert reason == ""
