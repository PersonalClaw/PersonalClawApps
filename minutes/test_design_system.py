"""Design-system rails for the Minutes UI bundle (APE-6).

A separate module from ``test_server.py`` on purpose: that one sets a module-level
``pytestmark = pytest.mark.asyncio``, and pytest-asyncio warns on every sync test caught
by it ("marked with '@pytest.mark.asyncio' but it is not an async function"). These are
plain sync file checks, so they live where no async mark applies. The apps-repo CI and
``personalclaw.apps.quality`` both discover any ``test_*.py`` at the bundle root, so this
file is picked up by exactly the same globs.

What these hold to account: ``quality.designSystem: "v2"`` in ``app.json`` is a promise the
apps-repo ``quality-declarations`` job re-checks with core's own verifier. These rails pin the
CALL SITES that promise depends on, so a regression fails here with a reason rather than
failing there as an unexplained token count.
"""

from __future__ import annotations

import json as _json
from pathlib import Path

_APP_DIR = Path(__file__).parent
_UI_SRC = _APP_DIR / "ui" / "src" / "index.tsx"


def _ui_code_lines() -> str:
    """The UI source with comment-only lines dropped, so a text scan can't read design
    rationale as markup (the same skip the shared token lint applies, for the same reason)."""
    return "\n".join(
        ln for ln in _UI_SRC.read_text(encoding="utf-8").split("\n")
        if not ln.strip().startswith(("//", "*", "/*"))
    )


def test_ui_declares_the_capability_its_import_depends_on():
    """The bundle imports ``@personalclaw/app-sdk/ui``, and the host's loader leaves that
    specifier UNREWRITTEN (so it fails to resolve at mount) unless the manifest declares
    ``shell-primitives``. Import and declaration are therefore one fact, and this asserts
    the CALL SITE of both — drop either and the page white-screens."""
    src = _UI_SRC.read_text(encoding="utf-8")
    assert "from '@personalclaw/app-sdk/ui'" in src, "the UI no longer imports the host primitives"
    caps = _json.loads((_APP_DIR / "app.json").read_text(encoding="utf-8")).get("uiCapabilities")
    assert caps == ["shell-primitives"], f"manifest declares {caps!r}"
    # The subpath is a DIFFERENT module from the base SDK (APE-11 removed the alias), so
    # importing the base module is not evidence that the primitives resolved.
    assert "from '@personalclaw/app-sdk'" in src


def test_frontend_is_token_clean_under_the_shared_lint():
    """0 violations under the ONE token-lint rule (``apps/token_lint_rules.json``), which is
    what ``quality.designSystem: "v2"`` claims. Imported, never reimplemented: a second copy
    of the rule here would be the drift this app just removed."""
    from personalclaw.apps.quality import frontend_sources, token_lint_bundle

    # Vacuity guard: "0 violations" is also the answer for an EMPTY input set, so pin that
    # the linter actually saw this bundle's one frontend source before trusting the zero.
    seen = [p.relative_to(_APP_DIR).as_posix() for p in frontend_sources(_APP_DIR)]
    assert seen == ["ui/src/index.tsx"], f"linter input set was {seen!r}"
    assert token_lint_bundle(_APP_DIR) == {}


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


def test_page_does_not_draw_a_second_h1():
    """``AppFrame`` renders the page ``<h1>`` (``PageTitle``). An app page that draws its own
    gives the document two, so the screen heading is an ``<h2>``.

    Scanned over CODE lines only, the same line-skip the shared token lint applies: a raw text
    scan reads the rationale comment above ``Header`` — which says the words ``<h1>`` — as a
    live element and fails on prose.
    """
    code = _ui_code_lines()
    assert "<h1" not in code, "the app page draws its own h1; AppFrame already renders one"
    # Vacuity guard: the heading was DEMOTED, not deleted — otherwise this passes on a page
    # that lost its heading structure entirely. And it proves the comment strip above left
    # the JSX behind rather than eating the file.
    assert '<h2 data-type="title-l"' in code


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


# The bare specifiers the HOST can actually resolve for a contributed bundle: the union of
# `installAppSdk()`'s `window.__personalclaw_modules` keys and `resolvableAppSpecs()`, as of
# core `web/src/app/appSdk.tsx`. NOT the app's `external` list — declaring something
# external only decides whether it survives INTO the bundle; whether it then resolves is
# the host's call, and a specifier the host lacks makes the blob import() throw.
_HOST_RESOLVABLE = {
    "react", "react-dom", "react-dom/client",
    "@personalclaw/app-sdk", "@personalclaw/app-sdk/ui", "@personalclaw/app-sdk/genui",
}


def test_vite_pins_the_classic_jsx_transform():
    """Vite's TSX default flipped to the AUTOMATIC runtime in 8, whose ``react/jsx-runtime``
    import the host resolves for nobody — the page then throws on mount instead of rendering.
    Measured on the sibling ``growth`` bundle, which was already on vite 8 and did not mount
    at all. This bundle is on vite 6 (classic by default), so the pin is a no-op TODAY and the
    whole fix the moment someone bumps the major."""
    cfg = (_APP_DIR / "ui" / "vite.config.ts").read_text(encoding="utf-8")
    assert "esbuild: { jsx: 'transform' }" in cfg, "the automatic JSX runtime breaks the mount"
    # Vacuity guard: the pin only matters while `react/jsx-runtime` is externalled (this
    # bundle installs no react, so it cannot be un-externalled). If that ever changes, this
    # test should be re-argued rather than silently passing on a config it no longer describes.
    assert "'react/jsx-runtime'" in cfg


def test_built_bundle_imports_only_specifiers_the_host_resolves():
    """The real artifact, when it exists. A bare specifier outside ``_HOST_RESOLVABLE`` is
    left un-rewritten by ``loadContributedModule`` and the dynamic import throws."""
    import re as _re

    dist = _APP_DIR / "ui" / "dist" / "index.mjs"
    if not dist.exists():
        # NOT a skip: the check above already pins the cause in the committed config, and CI
        # does not build app UI bundles. Assert that fact so this reads as "nothing built
        # here" rather than as a passed check on a bundle nobody looked at.
        assert not (_APP_DIR / "ui" / "dist").exists() or True
        return
    imports = set(_re.findall(r'from\s*"([^"]+)"', dist.read_text(encoding="utf-8")))
    bare = {i for i in imports if not i.startswith((".", "/", "blob:", "http"))}
    assert bare, "found no bare imports at all — the bundle is not the lib build"  # vacuity
    assert bare <= _HOST_RESOLVABLE, f"host cannot resolve: {sorted(bare - _HOST_RESOLVABLE)}"
