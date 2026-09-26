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
import shutil
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


def ui_entries(app_dir: Path) -> list[Path]:
    """Every UI file the manifest points the host at (``ui.entry``, ``ui.components`` and each
    page's ``entryPoint``). The host resolves them against ``<app>/ui/``."""
    ui = json.loads((app_dir / "app.json").read_text(encoding="utf-8")).get("ui") or {}
    declared = {str(p.get("entryPoint") or "") for p in ui.get("pages") or []}
    declared |= {str(ui.get("entry") or ""), str(ui.get("components") or "")}
    return [app_dir / "ui" / rel for rel in sorted(d for d in declared if d)]


#: A JavaScript package manager or build tool. An install hook that runs one needs Node on
#: the user's machine and a network fetch inside core's 60-second hook budget.
_JS_TOOLCHAIN = re.compile(r"\b(?:npm|npx|yarn|pnpm|vite)\b")


def assert_ships_built(app_dir: Path) -> None:
    """The page is in the app as built, so an install copies it and never builds it.

    Minutes and Growth used to point the host at ``ui/dist/index.mjs``, a path the repo
    ignores, so it was never in the app, and ``setup.sh`` ran ``npm install && npx vite
    build`` from the install hook. Without Node that hook printed "UI build skipped" and
    exited 0: the install succeeded and the page could not load. Checked two ways:

    * every declared entry is a file in the app, outside ``ui/src``. In a CI checkout only
      tracked files exist, so this is also the proof that the bundle is committed;
    * no setup hook, and no file in the app that a hook names, runs a JS toolchain.

    That the committed bundle is the build of ``ui/src`` is the ``ui-bundles`` CI job's
    check (``.github/scripts/check_ui_bundles.py``), because only a rebuild can show it.
    """
    entries = ui_entries(app_dir)
    assert entries, "the manifest declares no UI entry: there is nothing to check"
    for entry in entries:
        rel = entry.relative_to(app_dir).as_posix()
        assert entry.is_file(), f"{rel} is not in the app, so an install ships no page"
        assert not rel.startswith("ui/src/"), f"{rel} is a source file, not the built bundle"

    setup = json.loads((app_dir / "app.json").read_text(encoding="utf-8")).get("setup") or {}
    hooks = [str(v) for k, v in setup.items() if k.startswith("on") and isinstance(v, str)]
    scanned = list(hooks)
    for hook in hooks:
        for word in hook.split():
            named = app_dir / word
            if named.is_file():
                scanned.append(named.read_text(encoding="utf-8", errors="replace"))
    for text in scanned:
        assert not _JS_TOOLCHAIN.search(text), f"an install hook runs a JS toolchain: {text[:120]!r}"


def assert_scans_clean(app_dir: Path, scanner: object, scratch: Path) -> None:
    """Core's install scanner finds nothing in the app, so the Store installs it without "The
    security scanner raised warnings".

    Shipping the bundle is what puts this at risk: the scanner reads an ``.mjs`` as a script,
    and it never read the TSX. In Minutes and Growth the build's ``RegExp.exec`` call matched
    the ``python_exec`` rule and turned a first-party install into a warning. The app is copied
    first without ``node_modules`` and caches, which no install receives, so a local build
    tree cannot change the answer.

    ``scanner`` is ``personalclaw.supply_chain.default_scanner``, passed in for the same reason
    ``quality`` is in :func:`assert_token_clean`.
    """
    staged = scratch / app_dir.name
    shutil.copytree(
        app_dir, staged, ignore=shutil.ignore_patterns("node_modules", "__pycache__", ".pytest_cache")
    )
    report = scanner.scan(staged)  # type: ignore[attr-defined]
    found = [f"{f.rule} in {f.path}: {str(f.evidence)[:100]}" for f in report.findings]
    assert not found, f"the install scanner flags the app: {found}"
    assert getattr(report.verdict, "value", report.verdict) == "clean"


def assert_bundle_host_resolvable(app_dir: Path) -> None:
    """Every bare specifier the shipped bundle imports is one the host resolves. One outside
    ``HOST_RESOLVABLE`` is left un-rewritten by ``loadContributedModule`` and the dynamic
    import throws, so the page does not mount. Reads the committed files the manifest
    declares, and fails if one is missing: a check that returns early on a missing bundle
    passed for as long as no bundle shipped."""
    entries = ui_entries(app_dir)
    assert entries, "the manifest declares no UI entry"
    for entry in entries:
        assert entry.is_file(), f"{entry.relative_to(app_dir).as_posix()} is not in the app"
        text = entry.read_text(encoding="utf-8")
        imports = set(re.findall(r'(?:\bfrom\s*|\bimport\s*\(?\s*)"([^"]+)"', text))
        bare = {i for i in imports if not i.startswith((".", "/", "blob:", "http"))}
        assert bare, "found no bare imports at all — the bundle is not the lib build"
        assert bare <= HOST_RESOLVABLE, f"host cannot resolve: {sorted(bare - HOST_RESOLVABLE)}"
