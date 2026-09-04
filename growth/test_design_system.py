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
    design_rails.assert_dist_host_resolvable(_APP_DIR)
