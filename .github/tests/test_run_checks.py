"""The static-check runner reports every check on its own row, and a red never hides another.

The runner is ``.github/scripts/run_checks.py``, which CI's ``static`` job runs through
``scripts/gate.py static``. These tests drive that same script, unmodified, against seeded trees:
a copy sits at the same place in each, beside the checks planted for it, so the checks it finds
are exactly the seeded ones. None of them runs a check over the real tree: that is the ``static``
job's work, done once per run.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / ".github" / "scripts"
RUNNER = SCRIPTS / "run_checks.py"

_OK = "print('OK: planted check is clean')\n"
_RED = "import sys\nprint('planted defect: the-file.py')\nsys.exit(1)\n"


def _seeded(tmp_path: Path, checks: dict[str, str], *copies: str) -> Path:
    """A tree holding the runner, *checks* as planted ``.github/scripts`` files, and *copies*
    of real scripts from this repository's ``.github/scripts``."""
    root = tmp_path / "tree"
    scripts = root / ".github" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(RUNNER, scripts / RUNNER.name)
    for name in copies:
        shutil.copy2(SCRIPTS / name, scripts / name)
    for name, body in checks.items():
        (scripts / name).write_text(body, encoding="utf-8")
    return root


def _run(root: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run the seeded runner, as the gate runs it."""
    return subprocess.run(
        [sys.executable, str(root / ".github" / "scripts" / RUNNER.name), *args],
        cwd=root,
        capture_output=True,
        text=True,
        env=env,
    )


def _rows(stdout: str) -> dict[str, str]:
    """``{row: result}`` from the table the runner prints last."""
    rows = {}
    for line in stdout.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 3 and cells[1] in {"pass", "FAIL"}:
            rows[cells[0]] = cells[1]
    return rows


def test_a_failing_check_has_its_own_row_and_makes_the_run_red(tmp_path):
    root = _seeded(tmp_path, {"check_alpha.py": _OK, "check_beta.py": _RED})
    run = _run(root)
    assert run.returncode == 1, run.stdout + run.stderr
    assert _rows(run.stdout) == {"alpha": "pass", "beta": "FAIL"}, run.stdout
    # The failing check's own words reach the log, not only its verdict.
    assert "planted defect: the-file.py" in run.stdout


def test_a_check_after_a_failed_one_still_runs(tmp_path):
    marker = tmp_path / "later-ran"
    later = f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran')\nprint('OK')\n"
    root = _seeded(tmp_path, {"check_aaa_first.py": _RED, "check_zzz_later.py": later})
    run = _run(root)
    assert run.returncode == 1, run.stdout + run.stderr
    assert marker.read_text() == "ran"
    assert _rows(run.stdout) == {"aaa-first": "FAIL", "zzz-later": "pass"}, run.stdout


def test_a_check_that_crashes_is_a_failed_row(tmp_path):
    root = _seeded(tmp_path, {"check_crash.py": "raise RuntimeError('boom')\n", "check_ok.py": _OK})
    run = _run(root)
    assert run.returncode == 1
    assert _rows(run.stdout) == {"crash": "FAIL", "ok": "pass"}, run.stdout
    assert "RuntimeError: boom" in run.stdout


def test_every_check_in_the_folder_is_a_row(tmp_path):
    checks = {f"check_{n}.py": _OK for n in ("one", "two_words", "three")}
    root = _seeded(tmp_path, {**checks, "helper.py": _RED, "run_other.py": _RED})
    run = _run(root)
    assert run.returncode == 0, run.stdout + run.stderr
    # Only check_*.py is a check: a helper beside them is not run, so its red is not reported.
    assert _rows(run.stdout) == {"one": "pass", "two-words": "pass", "three": "pass"}


def test_a_folder_with_no_check_is_red(tmp_path):
    root = _seeded(tmp_path, {"helper.py": _OK})
    run = _run(root)
    assert run.returncode == 1
    assert "no check" in (run.stdout + run.stderr)


def test_the_table_reaches_the_job_summary_and_the_report(tmp_path):
    summary = tmp_path / "summary.md"
    report = tmp_path / "report.json"
    root = _seeded(tmp_path, {"check_alpha.py": _OK, "check_beta.py": _RED})
    env = {**_env(), "GITHUB_ACTIONS": "true", "GITHUB_STEP_SUMMARY": str(summary)}
    run = _run(root, "--report", str(report), env=env)
    assert run.returncode == 1
    table = summary.read_text(encoding="utf-8")
    assert "| alpha | pass |" in table and "| beta | FAIL |" in table, table
    # In CI a failed row is also an annotation that names it.
    assert "::error title=static check beta failed::" in run.stdout
    rows = json.loads(report.read_text(encoding="utf-8"))["checks"]
    assert [(r["row"], r["script"], r["returncode"]) for r in rows] == [
        ("alpha", ".github/scripts/check_alpha.py", 0),
        ("beta", ".github/scripts/check_beta.py", 1),
    ]


def test_every_check_here_is_a_row_under_the_name_the_docs_use():
    """The real folder, listed rather than run. Every ``check_*.py`` is a row, and each name the
    docs cite (once a job of its own) is one of them."""
    run = subprocess.run(
        [sys.executable, str(RUNNER), "--list"], capture_output=True, text=True, check=False
    )
    assert run.returncode == 0, run.stdout + run.stderr
    listed = dict(line.split(maxsplit=1) for line in run.stdout.splitlines())
    scripts = sorted(f".github/scripts/{p.name}" for p in SCRIPTS.glob("check_*.py"))
    assert sorted(listed.values()) == scripts
    assert {
        "boundary", "manifest-validate", "live-writes-posture", "child-process-env",
        "launches-declared", "network-declared", "active-home", "egress-policy",
        "network-clients", "prompt-cache-posture", "settings-schema-posture",
        "settings-declared-reads", "settings-route-ratchet", "trigger-source-adoption",
        "readme-census", "readme-license", "store-card-copy", "ui-bundles",
        "publication-hygiene", "test-values",
    } <= set(listed)


# -- the two checks that were inline programs in the workflow ---------------------------------

def _env() -> dict[str, str]:
    import os

    return dict(os.environ)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _app(root: Path, name: str, manifest: dict | str, licence: str | None = "MIT License\n") -> None:
    app = root / name
    app.mkdir(parents=True, exist_ok=True)
    text = manifest if isinstance(manifest, str) else json.dumps(manifest)
    (app / "app.json").write_text(text, encoding="utf-8")
    if licence is not None:
        (app / "LICENSE").write_text(licence, encoding="utf-8")


def _manifest(name: str, **extra) -> dict:
    return {
        "name": name, "version": "1.0.0", "displayName": "Planted App",
        "description": "Says hello.", **extra,
    }


def _boundary_tree(tmp_path: Path) -> Path:
    root = _seeded(tmp_path, {}, "check_boundary.py")
    _git(root, "init", "-q")
    (root / ".gitignore").write_text("/.venv/\n", encoding="utf-8")
    (root / "good-app").mkdir()
    (root / "good-app" / "provider.py").write_text(
        "from personalclaw.sdk.net import fetch\nimport json\n", encoding="utf-8"
    )
    _git(root, "add", "-A")
    return root


def test_the_boundary_check_is_green_on_sdk_imports(tmp_path):
    root = _boundary_tree(tmp_path)
    run = _run(root)
    assert run.returncode == 0, run.stdout + run.stderr
    assert _rows(run.stdout) == {"boundary": "pass"}
    assert "OK: all app imports go through personalclaw.sdk.*" in run.stdout


def test_a_tracked_deep_import_reds_the_boundary_check(tmp_path):
    root = _boundary_tree(tmp_path)
    (root / "good-app" / "deep.py").write_text(
        "from personalclaw.apps.manifest import AppManifest\n", encoding="utf-8"
    )
    _git(root, "add", "-A")
    run = _run(root)
    assert run.returncode == 1
    assert _rows(run.stdout) == {"boundary": "FAIL"}
    assert "good-app/deep.py: ['personalclaw.apps.manifest']" in run.stdout


def test_an_untracked_deep_import_reds_the_boundary_check(tmp_path):
    """A file not yet added is still read: the check skips only what git ignores."""
    root = _boundary_tree(tmp_path)
    (root / "good-app" / "new.py").write_text("import personalclaw.config\n", encoding="utf-8")
    run = _run(root)
    assert run.returncode == 1
    assert "good-app/new.py: ['personalclaw.config']" in run.stdout


def test_the_boundary_check_skips_ignored_paths_tests_and_caches(tmp_path):
    root = _boundary_tree(tmp_path)
    deep = "from personalclaw.apps import manifest\n"
    site = root / ".venv" / "lib" / "personalclaw"
    site.mkdir(parents=True)
    (site / "core_module.py").write_text(deep, encoding="utf-8")
    (root / "good-app" / "test_provider.py").write_text(deep, encoding="utf-8")
    (root / "good-app" / "__pycache__").mkdir()
    (root / "good-app" / "__pycache__" / "cached.py").write_text(deep, encoding="utf-8")
    (root / "top_level.py").write_text(deep, encoding="utf-8")  # not */*.py, as before
    _git(root, "add", "-A")
    run = _run(root)
    assert run.returncode == 0, run.stdout + run.stderr


def test_the_boundary_check_outside_a_git_tree_is_red(tmp_path):
    root = _seeded(tmp_path, {}, "check_boundary.py")
    (root / "app").mkdir()
    (root / "app" / "provider.py").write_text("import json\n", encoding="utf-8")
    env = {**_env(), "GIT_CEILING_DIRECTORIES": str(tmp_path)}
    run = _run(root, env=env)
    assert run.returncode == 1
    assert _rows(run.stdout) == {"boundary": "FAIL"}


def _manifest_tree(tmp_path: Path) -> Path:
    root = _seeded(tmp_path, {}, "check_manifests.py")
    _app(root, "good-app", _manifest("good-app"))
    return root


def test_the_manifest_check_is_green_on_a_valid_licensed_app(tmp_path):
    root = _manifest_tree(tmp_path)
    _app(root, "zero-app", _manifest("zero-app", license="MIT-0"), "MIT No Attribution\n")
    run = _run(root)
    assert run.returncode == 0, run.stdout + run.stderr
    assert _rows(run.stdout) == {"manifest-validate": "pass"}
    assert "OK: 2 manifests valid + licensed" in run.stdout


def test_a_manifest_core_rejects_reds_the_manifest_check(tmp_path):
    root = _manifest_tree(tmp_path)
    _app(root, "bad-app", _manifest("Bad Name", version="one"))
    run = _run(root)
    assert run.returncode == 1
    assert _rows(run.stdout) == {"manifest-validate": "FAIL"}
    assert "bad-app/app.json: name must be kebab-case" in run.stdout


def test_an_unparseable_manifest_reds_the_manifest_check(tmp_path):
    """Red, with the parse error in its output. (The licence half re-reads the manifest, so the
    check stops there on the parse error rather than listing it: it behaves as it did inline.)"""
    root = _manifest_tree(tmp_path)
    _app(root, "broken-app", "{not json")
    run = _run(root)
    assert run.returncode == 1
    assert _rows(run.stdout) == {"manifest-validate": "FAIL"}
    assert "JSONDecodeError" in run.stdout


def test_an_app_without_its_licence_reds_the_manifest_check(tmp_path):
    root = _manifest_tree(tmp_path)
    _app(root, "bare-app", _manifest("bare-app"), licence=None)
    run = _run(root)
    assert run.returncode == 1
    assert "bare-app: missing LICENSE" in run.stdout


def test_a_declared_licence_its_file_contradicts_reds_the_manifest_check(tmp_path):
    root = _manifest_tree(tmp_path)
    _app(root, "wrong-app", _manifest("wrong-app", license="MIT-0"), "MIT License\n")
    run = _run(root)
    assert run.returncode == 1
    assert "wrong-app/app.json: declares license MIT-0 but LICENSE file is MIT" in run.stdout
