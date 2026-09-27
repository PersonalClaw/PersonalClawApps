"""Design-system rails for the Growth UI bundle.

The bundle-contract rails live in ``apps_testkit.design_rails`` — one shared
implementation for every UI-bearing bundle, because this app is exactly the one
that shipped without them and regressed the JSX-runtime mount. A separate module
from ``test_server.py`` so no async pytest mark applies to these sync file checks.
"""

from __future__ import annotations

import sys
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_APP_DIR.parent))

from personalclaw.apps import quality  # noqa: E402

from apps_testkit import design_rails  # noqa: E402


def test_ui_declares_the_capability_its_import_depends_on():
    design_rails.assert_ui_capability_pairs_with_import(_APP_DIR)


def test_frontend_is_token_clean_under_the_shared_lint():
    design_rails.assert_token_clean(_APP_DIR, quality)


def test_page_does_not_draw_a_second_h1():
    design_rails.assert_no_second_h1(_APP_DIR)


def test_vite_pins_the_classic_jsx_transform():
    design_rails.assert_classic_jsx_transform(_APP_DIR)


def test_built_bundle_imports_only_specifiers_the_host_resolves():
    design_rails.assert_bundle_host_resolvable(_APP_DIR)


def test_the_page_ships_built_and_installing_it_runs_no_npm():
    """🔴 Red on main: the entry was ``ui/dist/index.mjs``, a path the repo ignores, and the
    install hook ran ``npm install && npx vite build``."""
    design_rails.assert_ships_built(_APP_DIR)


def test_the_store_installs_it_without_a_scanner_warning(tmp_path):
    """The shipped bundle is scanned as a script at install. Its ``RegExp.exec`` call matched
    the scanner's rule for Python's ``exec`` and made the consent say "The security scanner
    raised warnings". The app's own files are scanned too, this one included."""
    from personalclaw.supply_chain import default_scanner

    design_rails.assert_scans_clean(_APP_DIR, default_scanner, tmp_path)
