#!/usr/bin/env python3
"""Every committed app UI bundle is exactly the build of the sources beside it.

An install copies an app as it is. Minutes and Growth used to ship only their UI sources and
build them from a ``setup.sh`` install hook (``npm install && npx vite build``), which core
bounds at 60 seconds. Without Node the hook printed "UI build skipped" and exited 0, so the
install succeeded and the page could not load; on a slow network it timed out and the
install failed. So each app's UI is built here and committed under ``<app>/ui/bundle/``, and
an install never runs npm.

A committed build has two ways to go wrong, and this rail closes both:

* STALE: ``ui/src`` changed and nobody rebuilt, so users get the old page.
* NOT THE SOURCE: the bundle says something the sources do not. Reviewers read the sources
  and users run the bundle, so a bundle nobody can reproduce is code nobody reviewed.

For every app with a ``ui/package.json``, it copies ``ui/`` without ``node_modules`` and the
bundle directory into a scratch directory, runs ``npm ci --ignore-scripts`` and
``npm run build`` there, and compares what the build wrote with the committed files, file
for file. The working tree is never written to, so it is safe to run locally. It needs Node
and npm on ``PATH`` and network access to the npm registry.

Vacuity floor: it must find at least one UI app and compare at least one file, or it fails.
A rail that finds nothing to check prints OK forever.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]

#: The directory each app's vite config builds into, and the manifest's entries point at.
BUNDLE_DIR = "bundle"

#: ``npm ci`` fetches from the registry; a hung fetch must fail the job, not stall it.
_NPM_TIMEOUT_S = 600


def _tracked() -> set[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True)
    return {p.decode("utf-8", "surrogateescape") for p in out.stdout.split(b"\0") if p}


def _ui_apps() -> list[Path]:
    return sorted(
        p.parent.parent
        for p in ROOT.glob("*/ui/package.json")
        if (p.parent.parent / "app.json").is_file()
    )


def _declared_entries(app: Path) -> list[str]:
    ui = json.loads((app / "app.json").read_text(encoding="utf-8")).get("ui") or {}
    declared = {str(p.get("entryPoint") or "") for p in ui.get("pages") or []}
    declared |= {str(ui.get("entry") or ""), str(ui.get("components") or "")}
    return sorted(d for d in declared if d)


def _run(argv: list[str], cwd: Path) -> str:
    """Run one npm step; on failure return the tail of its output as the problem."""
    try:
        proc = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=_NPM_TIMEOUT_S, check=False
        )
    except subprocess.TimeoutExpired:
        return f"`{' '.join(argv)}` timed out after {_NPM_TIMEOUT_S}s"
    if proc.returncode != 0:
        tail = (proc.stdout + proc.stderr).strip()[-800:]
        return f"`{' '.join(argv)}` exited {proc.returncode}:\n{tail}"
    return ""


def check_app(app: Path, tracked: set[str], scratch: Path) -> tuple[list[str], int]:
    """Problems for one app, and how many files it compared."""
    name = app.name
    problems: list[str] = []
    entries = _declared_entries(app)
    if not entries:
        return [f"{name}: ui/package.json exists but app.json declares no UI entry"], 0
    for rel in entries:
        if PurePosixPath(rel).parts[0] != BUNDLE_DIR:
            problems.append(f"{name}: UI entry {rel!r} is outside ui/{BUNDLE_DIR}/")
        elif f"{name}/ui/{rel}" not in tracked:
            problems.append(f"{name}: UI entry ui/{rel} is not committed, so no install has it")
    if problems:
        return problems, 0

    work = scratch / name / "ui"
    shutil.copytree(
        app / "ui", work, ignore=shutil.ignore_patterns("node_modules", BUNDLE_DIR)
    )
    for step in (
        ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
        ["npm", "run", "build"],
    ):
        failure = _run(step, work)
        if failure:
            return [f"{name}: {failure}"], 0

    built = {
        p.relative_to(work).as_posix(): p for p in (work / BUNDLE_DIR).rglob("*") if p.is_file()
    }
    committed = {
        t[len(f"{name}/ui/") :]
        for t in tracked
        if t.startswith(f"{name}/ui/{BUNDLE_DIR}/")
    }
    for rel in sorted(set(built) - committed):
        problems.append(f"{name}: the build writes ui/{rel}, which is not committed")
    for rel in sorted(committed - set(built)):
        problems.append(f"{name}: ui/{rel} is committed but the build no longer writes it")
    compared = 0
    for rel in sorted(set(built) & committed):
        compared += 1
        if built[rel].read_bytes() != (app / "ui" / rel).read_bytes():
            problems.append(f"{name}: ui/{rel} is not the build of ui/src (stale or edited)")
    return problems, compared


def main() -> int:
    if shutil.which("npm") is None:
        print("ui-bundles: FAIL: needs Node and npm on PATH to rebuild the bundles")
        return 1
    tracked = _tracked()
    apps = _ui_apps()
    problems: list[str] = []
    compared = 0
    with tempfile.TemporaryDirectory(prefix="ui-bundles-") as tmp:
        for app in apps:
            found, n = check_app(app, tracked, Path(tmp))
            problems += found
            compared += n
    if not apps or (not problems and compared == 0):
        problems.append("vacuity floor: found no UI app, or compared no file")
    if problems:
        print("ui-bundles: FAIL")
        for line in problems:
            print(f"  {line}")
        print(
            "\nRebuild with `npm ci && npm run build` in the app's ui/ directory and commit "
            f"ui/{BUNDLE_DIR}/. Do not edit a bundle by hand."
        )
        return 1
    names = ", ".join(a.name for a in apps)
    print(f"OK: {compared} committed bundle file(s) are the build of their sources ({names})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
