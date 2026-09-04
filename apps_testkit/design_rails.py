"""Shared design-system rails for every UI-bearing bundle in this repo.

One implementation, parameterized by app directory, imported by each bundle's
``test_design_system.py`` — a rail copy-pasted per app is the drift these rails
exist to stop. The assertions pin the CALL SITES that ``quality.designSystem``
depends on, so a regression fails here with a reason rather than failing in the
``quality-declarations`` job as an unexplained token count.

App-SPECIFIC rails (a pinned control shape, a banned local constant) stay in the
app's own test file; only the bundle-contract rails live here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import ModuleType

# The bare specifiers the HOST can actually resolve for a contributed bundle: the
# union of ``installAppSdk()``'s ``window.__personalclaw_modules`` keys and
# ``resolvableAppSpecs()``, as of core ``web/src/app/appSdk.tsx``. NOT the app's
# ``external`` list — declaring something external only decides whether it survives
# INTO the bundle; whether it then resolves is the host's call, and a specifier the
# host lacks makes the blob import() throw.
HOST_RESOLVABLE = {
    "react", "react-dom", "react-dom/client",
    "@personalclaw/app-sdk", "@personalclaw/app-sdk/ui", "@personalclaw/app-sdk/genui",
}


def ui_src(app_dir: Path) -> Path:
    return app_dir / "ui" / "src" / "index.tsx"


def code_lines(app_dir: Path) -> str:
    """The UI source with comment-only lines dropped, so a text scan can't read
    design rationale as markup (the same skip the shared token lint applies)."""
    return "\n".join(
        ln for ln in ui_src(app_dir).read_text(encoding="utf-8").split("\n")
        if not ln.strip().startswith(("//", "*", "/*"))
    )


def assert_ui_capability_pairs_with_import(app_dir: Path) -> None:
    """The bundle imports ``@personalclaw/app-sdk/ui``, and the host's loader leaves
    that specifier UNREWRITTEN (so it fails to resolve at mount) unless the manifest
    declares ``shell-primitives``. Import and declaration are one fact — drop either
    and the page white-screens. The subpath is a DIFFERENT module from the base SDK,
    so importing the base module is not evidence the primitives resolved."""
    src = ui_src(app_dir).read_text(encoding="utf-8")
    assert "from '@personalclaw/app-sdk/ui'" in src, "the UI no longer imports the host primitives"
    caps = json.loads((app_dir / "app.json").read_text(encoding="utf-8")).get("uiCapabilities")
    assert caps is not None and "shell-primitives" in caps, f"manifest declares {caps!r}"
    assert "from '@personalclaw/app-sdk'" in src


def assert_token_clean(app_dir: Path, quality: ModuleType) -> None:
    """0 violations under the ONE token-lint rule set — what ``designSystem: "v2"``
    claims. Imported, never reimplemented. Vacuity guard: "0 violations" is also the
    answer for an EMPTY input set, so pin that the linter actually saw this bundle's
    frontend source before trusting the zero.

    ``quality`` is ``personalclaw.apps.quality``, passed in rather than imported here:
    the SDK-only import boundary exempts ``test_*.py`` (dev-tree, not an installed app)
    and this kit is not test-named, so the core reach stays at the exempt call site.
    """
    frontend_sources, token_lint_bundle = quality.frontend_sources, quality.token_lint_bundle

    seen = [p.relative_to(app_dir).as_posix() for p in frontend_sources(app_dir)]
    assert "ui/src/index.tsx" in seen, f"linter input set was {seen!r}"
    assert token_lint_bundle(app_dir) == {}


def assert_no_second_h1(app_dir: Path) -> None:
    """``AppFrame`` renders the page ``<h1>`` (``PageTitle``). An app page that draws
    its own gives the document two, so the screen heading is an ``<h2>``. Scanned over
    CODE lines only — rationale comments legitimately say the words ``<h1>``. Vacuity
    guard: the heading is DEMOTED, not deleted, so an ``<h2>`` must be present."""
    code = code_lines(app_dir)
    assert "<h1" not in code, "the app page draws its own h1; AppFrame already renders one"
    assert "<h2" in code, "no screen heading at all — the page lost its heading structure"


def assert_classic_jsx_transform(app_dir: Path) -> None:
    """Vite's TSX default flipped to the AUTOMATIC runtime in 8, whose
    ``react/jsx-runtime`` import the host resolves for nobody — the page then throws
    on mount instead of rendering. The transform is pinned rather than left to a vite
    default that has already changed under a bundle in this repo once."""
    cfg = (app_dir / "ui" / "vite.config.ts").read_text(encoding="utf-8")
    assert "esbuild: { jsx: 'transform' }" in cfg, "the automatic JSX runtime breaks the mount"
    assert "'react/jsx-runtime'" in cfg, "react/jsx-runtime is no longer named — re-argue this pin"


def assert_dist_host_resolvable(app_dir: Path) -> None:
    """The real artifact, when it exists: a bare specifier outside ``HOST_RESOLVABLE``
    is left un-rewritten by ``loadContributedModule`` and the dynamic import throws.
    When no dist is committed this asserts nothing — the config pin above already
    holds the cause, and CI does not build app UI bundles."""
    dist = app_dir / "ui" / "dist" / "index.mjs"
    if not dist.exists():
        return
    imports = set(re.findall(r'from\s*"([^"]+)"', dist.read_text(encoding="utf-8")))
    bare = {i for i in imports if not i.startswith((".", "/", "blob:", "http"))}
    assert bare, "found no bare imports at all — the bundle is not the lib build"
    assert bare <= HOST_RESOLVABLE, f"host cannot resolve: {sorted(bare - HOST_RESOLVABLE)}"
