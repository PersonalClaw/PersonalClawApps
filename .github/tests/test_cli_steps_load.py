"""Every app's ``cli.setup`` / ``cli.doctor`` step loads the way ``personalclaw setup`` / ``doctor`` load it.

Core's CLI runner (``personalclaw.app_cli._import_app_callable``) loads the declared module by path
out of the INSTALLED copy, holding the app's directory on ``sys.path`` while the step imports and
while it runs (core #3621) — so a step imports its own package (``telegram_runtime``, a top-level
``provider``) exactly as the app's provider module does, with no path code of its own. Before that,
ten steps across five apps failed there with ``ModuleNotFoundError`` while every app's own suite
passed, because each suite's conftest puts the app directory on ``sys.path`` before anything is
imported.

So each step is loaded through core's own loader, from a copy placed where an install puts it,
in a FRESH interpreter: a provider imported earlier in the same process would have cached the
app's package in ``sys.modules`` and hidden a step that cannot load.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: What the child runs: core's loader, nothing else. Its exit status is the verdict.
_LOAD = (
    "import sys\n"
    "from personalclaw.app_cli import _import_app_callable\n"
    "_import_app_callable(sys.argv[1], sys.argv[2])\n"
)


def _steps(apps_root: Path) -> list[tuple[Path, str, str, str]]:
    """``(bundle dir, app name, kind, "module:function")`` for every declared CLI step."""
    found = []
    for manifest in sorted(apps_root.glob("*/app.json")):
        data = json.loads(manifest.read_text(encoding="utf-8"))
        for kind in ("setup", "doctor"):
            ref = (data.get("cli") or {}).get(kind)
            if ref:
                found.append((manifest.parent, data["name"], kind, ref))
    return found


def _load_error(bundle: Path, name: str, ref: str, home: Path) -> str:
    """Load one step as ``personalclaw setup``/``doctor`` would; ``""`` when it loads."""
    installed = home / "apps" / name
    if not installed.exists():
        shutil.copytree(bundle, installed, ignore=shutil.ignore_patterns("__pycache__"))
    env = {**os.environ, "PERSONALCLAW_HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("PYTHONPATH", None)  # nothing but the interpreter's own site-packages
    proc = subprocess.run(
        [sys.executable, "-c", _LOAD, name, ref],
        capture_output=True, text=True, env=env, cwd=home, timeout=120,
    )
    if proc.returncode == 0:
        return ""
    return (proc.stderr.strip().splitlines() or ["(no output)"])[-1]


def test_every_declared_cli_step_loads_through_cores_loader(tmp_path):
    steps = _steps(ROOT)
    # Vacuity floor: 14 apps declare 28 steps as this rail lands; a glob that matched nothing
    # would pass every assertion below.
    assert len(steps) >= 20, f"only {len(steps)} cli steps found — did the manifest key move?"
    broken = [
        f"{name} cli.{kind} ({ref}): {error}"
        for bundle, name, kind, ref in steps
        if (error := _load_error(bundle, name, ref, tmp_path))
    ]
    assert not broken, "core's CLI runner cannot load:\n  " + "\n  ".join(broken)


def _probe_step(tmp_path: Path, body: str) -> str:
    """A one-step probe app whose ``cli_setup`` module is *body*, loaded through core's loader."""
    bundle = tmp_path / "src" / "probe-app"
    (bundle / "probe_runtime").mkdir(parents=True)
    (bundle / "probe_runtime" / "__init__.py").write_text("VALUE = 1\n")
    (bundle / "cli_setup.py").write_text(body + "\ndef run(ctx):\n    return VALUE\n")
    return _load_error(bundle, "probe-app", "cli_setup:run", tmp_path / "home")


def test_a_step_imports_its_own_package_with_no_path_code(tmp_path):
    """The contract every step above relies on since the apps dropped their own ``sys.path``
    prologues: core holds the app directory on the path while a step imports."""
    assert _probe_step(tmp_path, "from probe_runtime import VALUE\n") == ""


def test_the_check_sees_a_step_that_cannot_load(tmp_path):
    """Positive control: an import that resolves nowhere fails through the loader, so the rail
    above can fail — and names the module that is missing."""
    error = _probe_step(tmp_path, "from probe_runtime import VALUE\nimport not_installed_anywhere\n")
    assert "No module named 'not_installed_anywhere'" in error
