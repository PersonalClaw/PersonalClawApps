"""Design-system rails for the Minutes UI bundle (APE-6).

A separate module from ``test_server.py`` on purpose: that one sets a module-level
``pytestmark = pytest.mark.asyncio``, and pytest-asyncio warns on every sync test caught
by it ("marked with '@pytest.mark.asyncio' but it is not an async function"). These are
plain sync file checks, so they live where no async mark applies. The apps-repo CI and
``personalclaw.apps.quality`` both discover any ``test_*.py`` at the bundle root, so this
file is picked up by exactly the same globs.

The bundle-contract rails live in ``apps_testkit.design_rails`` — one shared,
parameterized implementation for every UI-bearing bundle, so a new bundle starts
protected instead of copy-pasting (or skipping) them. Only the rails specific to THIS
app's surfaces stay here.
"""

from __future__ import annotations

import sys
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_APP_DIR.parent))

from personalclaw.apps import quality  # noqa: E402

from apps_testkit import design_rails  # noqa: E402

_ui_code_lines = lambda: design_rails.code_lines(_APP_DIR)  # noqa: E731


def test_ui_declares_the_capability_its_import_depends_on():
    design_rails.assert_ui_capability_pairs_with_import(_APP_DIR)


def test_frontend_is_token_clean_under_the_shared_lint():
    design_rails.assert_token_clean(_APP_DIR, quality)


def test_page_does_not_draw_a_second_h1():
    design_rails.assert_no_second_h1(_APP_DIR)


def test_vite_pins_the_classic_jsx_transform():
    design_rails.assert_classic_jsx_transform(_APP_DIR)


def test_built_bundle_imports_only_specifiers_the_host_resolves():
    design_rails.assert_dist_host_resolvable(_APP_DIR)


# ── App-specific rails — these pin THIS bundle's surfaces, not the contract ──────────


def test_no_local_re_declaration_of_a_host_primitive():
    """APE-6 deleted this bundle's copies of the host component spec — a `cardStyle` that
    re-declared `Surface`'s tone+radius plus a border the neumorphic ground does not have, and
    four button constants re-declaring `Button`'s variant token values. A new one would fork
    the design system again silently, which is the failure mode the migration removed."""
    src = _ui_code_lines()
    for name in ("primaryBtn", "smallBtn", "linkBtn", "tabStyle", "tabActive", "cardStyle"):
        assert name not in src, f"{name} is back — use the host Button/Surface instead"
    # Vacuity guard: the scan above passes trivially on an empty/renamed file, so pin that
    # what replaced those constants is present.
    assert "<Button " in src and "<Surface " in src


def test_seeking_a_transcript_line_is_keyboard_reachable():
    """Clicking a transcript line seeks the media — an ACTION, so it is a real ``<button>``.
    It used to be a ``<div onClick>``, which no keyboard user can reach at all. Cheap to
    regress (a div is shorter to write), so the shape is pinned."""
    code = _ui_code_lines()
    assert 'data-testid="transcript-line"' in code, "the transcript rows are gone"
    line = next(ln for ln in code.split("\n") if 'data-testid="transcript-line"' in ln)
    assert line.lstrip().startswith("<button"), f"transcript line is not a button: {line.strip()[:90]}"
    # Vacuity guard: a <button> with no handler seeks nothing, so the row must still be wired.
    assert "seek(s.start)" in line
