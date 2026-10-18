"""``scripts/gate.py`` runs the parts asked of it, keeps going past a red, and writes its receipt.

The gate is the one apps gate: CI's ``static`` job runs its ``static`` part, the ``tests`` job its
``rails`` and ``bundles`` parts, and ``make gate`` all three. These tests drive the same script,
unmodified, from a copy in a seeded git repository whose parts are planted, so what it runs is
exactly what was seeded; none of them runs a real check or a real bundle suite.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import planted_envs as planted

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "scripts" / "gate.py"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _seeded(
    tmp_path: Path, *, check: str = "print('OK')\n", rail: str | None = None,
    lock: str | None = None,
) -> Path:
    """A committed repository holding the gate, the runners it calls, one planted static check,
    one planted ``.github/tests`` test, one planted bundle and, given *lock*, a ``core.lock``."""
    root = tmp_path / "tree"
    (root / "scripts").mkdir(parents=True)
    (root / ".github" / "scripts").mkdir(parents=True)
    (root / ".github" / "tests").mkdir(parents=True)
    shutil.copy2(GATE, root / "scripts" / "gate.py")
    shutil.copy2(ROOT / "scripts" / "test_bundles.py", root / "scripts" / "test_bundles.py")
    shutil.copy2(ROOT / "scripts" / "core_lock.py", root / "scripts" / "core_lock.py")
    shutil.copy2(ROOT / "scripts" / "bundle_lock.py", root / "scripts" / "bundle_lock.py")
    shutil.copy2(ROOT / ".github" / "scripts" / "run_checks.py", root / ".github" / "scripts")
    (root / ".github" / "scripts" / "check_planted.py").write_text(check, encoding="utf-8")
    (root / ".github" / "tests" / "test_planted.py").write_text(
        rail or "def test_planted():\n    assert True\n", encoding="utf-8"
    )
    for name in ("first-app", "second-app"):
        app = root / name
        app.mkdir()
        (app / "app.json").write_text(json.dumps({"name": name}), encoding="utf-8")
        (app / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    if lock is not None:
        (root / "core.lock").write_text(lock, encoding="utf-8", newline="")
    # The bundles part builds each bundle's environment from a lock: a planted one, against an
    # empty local index, with a stub core, so no part reaches the network.
    (root / ".gitignore").write_text(".bundle-venvs/\n.planted/\nout/\n__pycache__/\n", encoding="utf-8")
    planted.lock(root, planted.Index(root / ".planted" / "index"), root / ".planted" / "lock")
    planted.stub_core(root / ".planted" / "core")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(
        root, "-c", "user.name=Gate Test", "-c", "user.email=gate@example.com",
        "commit", "-q", "-m", "seed",
    )
    return root


def _gate(root: Path, *args: str, results: str = "out") -> subprocess.CompletedProcess:
    (core,) = (root / ".planted" / "core").glob("*.whl")
    return subprocess.run(
        [sys.executable, str(root / "scripts" / "gate.py"), "--results", str(root / results),
         *args],
        cwd=root,
        capture_output=True,
        text=True,
        env=planted.environ(root / ".planted" / "lock", str(core), root / ".bundle-venvs"),
    )


def _receipt(root: Path) -> dict:
    return json.loads((root / "out" / "receipt.json").read_text(encoding="utf-8"))


def _installed_vcs() -> dict:
    try:
        dist = importlib.metadata.distribution("personalclaw")
    except importlib.metadata.PackageNotFoundError:
        return {}
    direct = json.loads(dist.read_text("direct_url.json") or "{}")
    return direct.get("vcs_info") or {}


def _installed_core_commit() -> str | None:
    return _installed_vcs().get("commit_id")


def test_the_receipt_names_both_revisions_the_toolchain_and_the_parts(tmp_path):
    root = _seeded(tmp_path)
    run = _gate(root, "static")
    assert run.returncode == 0, run.stdout + run.stderr
    receipt = _receipt(root)
    assert receipt["apps"] == {"sha": _git(root, "rev-parse", "HEAD"), "dirty": False}
    assert receipt["core"]["version"] == importlib.metadata.version("personalclaw")
    assert receipt["core"]["commit"] == _installed_core_commit()
    toolchain = receipt["toolchain"]
    assert toolchain["python"] == ".".join(map(str, sys.version_info[:3]))
    assert {"uv", "node", "npm", "runner"} <= set(toolchain)
    assert [(p["part"], p["result"]) for p in receipt["parts"]] == [("static", "pass")]
    assert [r["row"] for r in receipt["static_checks"]] == ["planted"]
    assert receipt["verdict"] == "pass"


def test_an_uncommitted_change_is_named_in_the_receipt(tmp_path):
    root = _seeded(tmp_path)
    (root / "first-app" / "test_ok.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")
    assert _gate(root, "static").returncode == 0
    assert _receipt(root)["apps"]["dirty"] is True


def test_a_red_part_reds_the_gate_and_the_next_part_still_runs(tmp_path):
    root = _seeded(tmp_path, check="import sys\nprint('planted defect')\nsys.exit(1)\n")
    run = _gate(root, "static", "rails")
    assert run.returncode == 1, run.stdout + run.stderr
    receipt = _receipt(root)
    assert [(p["part"], p["result"]) for p in receipt["parts"]] == [
        ("static", "FAIL"), ("rails", "pass"),
    ]
    assert receipt["verdict"] == "FAIL"
    assert "planted defect" in run.stdout


def test_a_red_rail_reds_the_gate(tmp_path):
    root = _seeded(tmp_path, rail="def test_planted():\n    assert False, 'planted'\n")
    run = _gate(root, "rails")
    assert run.returncode == 1
    assert [(p["part"], p["result"]) for p in _receipt(root)["parts"]] == [("rails", "FAIL")]


def test_the_rails_run_on_as_many_workers_as_jobs_and_the_receipt_says_how_many(tmp_path):
    """The rails are a fixed cost in every pull request's run, so they take the same --jobs as the
    bundles (CI's four), not a fixed two."""
    root = _seeded(tmp_path)
    run = _gate(root, "rails", "--jobs", "3")
    assert run.returncode == 0, run.stdout + run.stderr
    assert "-m pytest .github/tests -n 3 -q" in run.stdout
    (rails,) = _receipt(root)["parts"]
    assert (rails["part"], rails["workers"]) == ("rails", 3)


def test_without_jobs_the_rails_take_the_runner_s_default(tmp_path):
    root = _seeded(tmp_path)
    run = _gate(root, "rails")
    assert run.returncode == 0, run.stdout + run.stderr
    spec = importlib.util.spec_from_file_location("_runner", ROOT / "scripts" / "test_bundles.py")
    runner = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = runner
    spec.loader.exec_module(runner)
    workers = runner.default_jobs()
    assert f"-m pytest .github/tests -n {workers} -q" in run.stdout
    assert _receipt(root)["parts"][0]["workers"] == workers


def test_with_no_part_named_the_gate_runs_all_three_in_order(tmp_path):
    root = _seeded(tmp_path)
    run = _gate(root)
    assert run.returncode == 0, run.stdout + run.stderr
    receipt = _receipt(root)
    assert [p["part"] for p in receipt["parts"]] == ["static", "rails", "bundles"]
    assert receipt["selection"] == {
        "bundles": ["first-app", "second-app"],
        "reason": "every app bundle that ships tests",
    }
    results = json.loads((root / "out" / "bundles" / "results.json").read_text())
    assert [(b["bundle"], b["returncode"]) for b in results["bundles"]] == [
        ("first-app", 0), ("second-app", 0),
    ]


def test_bundles_named_on_the_command_line_are_the_selection(tmp_path):
    root = _seeded(tmp_path)
    run = _gate(root, "bundles", "--bundle", "second-app")
    assert run.returncode == 0, run.stdout + run.stderr
    assert _receipt(root)["selection"] == {
        "bundles": ["second-app"],
        "reason": "named on the command line",
    }
    assert "first-app" not in run.stdout


def test_a_core_other_than_the_one_named_is_refused_before_anything_runs(tmp_path):
    root = _seeded(tmp_path)
    run = _gate(root, "--core", "0" * 40)
    assert run.returncode == 2
    assert "core" in run.stderr and "0" * 40 in run.stderr
    assert not (root / "out" / "receipt.json").exists()


def test_the_core_named_is_accepted_when_it_is_the_one_installed(tmp_path):
    commit = _installed_core_commit()
    if not commit:
        pytest.skip("the installed core does not record the commit it was built from")
    root = _seeded(tmp_path)
    run = _gate(root, "static", "--core", commit[:12])
    assert run.returncode == 0, run.stdout + run.stderr
    assert _receipt(root)["core"]["commit"] == commit


def test_an_unknown_part_is_refused(tmp_path):
    root = _seeded(tmp_path)
    run = _gate(root, "everything")
    assert run.returncode == 2
    assert "everything" in run.stderr


# -- core.lock: the receipt records the lock beside the installed core -----------------------------

def test_the_receipt_records_the_lock_and_warns_that_another_core_is_installed(tmp_path):
    root = _seeded(tmp_path, lock="0" * 40 + "\n")
    run = _gate(root, "static")
    assert run.returncode == 0, run.stdout + run.stderr
    core = _receipt(root)["core"]
    assert core["lock"] == "0" * 40
    assert core["matches_lock"] is False
    assert core["requested"] == _installed_vcs().get("requested_revision")
    # A local run warns and goes on: work against an unpushed core stays possible.
    assert "warning" in run.stderr and "0" * 40 in run.stderr and "core.lock" in run.stderr


def test_the_receipt_says_so_when_the_installed_core_is_the_locked_one(tmp_path):
    commit = _installed_core_commit()
    if not commit:
        pytest.skip("the installed core does not record the commit it was built from")
    root = _seeded(tmp_path, lock=commit + "\n")
    run = _gate(root, "static")
    assert run.returncode == 0, run.stdout + run.stderr
    core = _receipt(root)["core"]
    assert (core["lock"], core["matches_lock"]) == (commit, True)
    assert "warning" not in run.stderr


@pytest.mark.parametrize("lock", [None, "main\n"], ids=["no core.lock", "a branch name"])
def test_a_missing_or_bad_lock_is_recorded_as_none_and_warned_about(tmp_path, lock):
    root = _seeded(tmp_path, lock=lock)
    run = _gate(root, "static")
    assert run.returncode == 0, run.stdout + run.stderr
    core = _receipt(root)["core"]
    assert (core["lock"], core["matches_lock"]) == (None, None)
    assert "warning" in run.stderr and "core.lock" in run.stderr


# -- one results folder, one run --------------------------------------------------------------

def test_a_second_run_in_the_same_results_folder_is_refused_and_names_results(tmp_path):
    import fcntl

    root = _seeded(tmp_path)
    (root / "out").mkdir()
    with open(root / "out" / ".gate.lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run = _gate(root, "static")
    assert run.returncode == 2, run.stdout + run.stderr
    assert "--results" in run.stderr and str(root / "out") in run.stderr
    assert not (root / "out" / "receipt.json").exists()
    # Another folder is the way to run a second one, and the first folder is free again after.
    assert _gate(root, "static", results="elsewhere").returncode == 0
    assert _gate(root, "static").returncode == 0


def test_the_bundles_part_records_each_environment_and_the_jobs_it_ran_with(tmp_path):
    root = _seeded(tmp_path)
    run = _gate(root, "bundles", "--jobs", "2")
    assert run.returncode == 0, run.stdout + run.stderr
    receipt = _receipt(root)
    assert receipt["bundles"]["jobs"] == 2
    assert receipt["bundles"]["resolution"] == "locked"
    (environment,) = receipt["bundles"]["environments"]
    assert environment["action"] in {"built", "reused"}
    assert set(environment["inputs"]) >= {"core", "requirements", "python", "abi", "os", "arch",
                                          "uv", "index", "resolution"}
    assert (root / "out" / "bundles" / "environments" / f"{environment['key']}.json").is_file()


def test_changed_is_the_selection_the_receipt_records(tmp_path):
    root = _seeded(tmp_path)
    base = _git(root, "rev-parse", "HEAD")
    (root / "second-app" / "provider.py").write_text("X = 1\n", encoding="utf-8")
    run = _gate(root, "bundles", "--changed", base)
    assert run.returncode == 0, run.stdout + run.stderr
    selection = _receipt(root)["selection"]
    assert selection["bundles"] == ["second-app"], selection
    assert base in selection["reason"]



def test_a_part_left_running_by_a_killed_gate_still_keeps_a_second_gate_out(tmp_path):
    import os
    import signal
    import time

    root = _seeded(tmp_path)
    marker, release = root / "started", root / "release"
    (root / "second-app" / "test_ok.py").write_text(
        "import pathlib, time\n\n"
        "def test_ok():\n"
        f"    pathlib.Path({str(marker)!r}).write_text('x')\n"
        "    deadline = time.monotonic() + 30\n"
        f"    while not pathlib.Path({str(release)!r}).exists() and time.monotonic() < deadline:\n"
        "        time.sleep(0.02)\n", encoding="utf-8")
    (core,) = (root / ".planted" / "core").glob("*.whl")
    env = planted.environ(root / ".planted" / "lock", str(core), root / ".bundle-venvs")
    gate = subprocess.Popen([sys.executable, str(root / "scripts" / "gate.py"), "--results",
                             str(root / "out"), "bundles", "--bundle", "second-app"], cwd=root,
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 60
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert marker.exists(), "the bundle never started"
    os.kill(gate.pid, signal.SIGKILL)
    gate.wait()
    try:
        run = _gate(root, "bundles", "--bundle", "first-app")
        assert run.returncode == 2, run.stdout + run.stderr
        assert "--results" in run.stderr
    finally:
        release.touch()


def test_a_bundle_s_pytest_left_running_by_a_stopped_gate_and_runner_keeps_a_second_gate_out(
        tmp_path):
    # Ctrl-C: the gate kills its runner, but the runner's pytest finishes its own cleanup and
    # writes its JUnit into the folder, so it holds the folder's lock too.
    import os
    import signal
    import time

    root = _seeded(tmp_path)
    marker, release = root / "started", root / "release"
    (root / "second-app" / "test_ok.py").write_text(
        "import pathlib, time\n\n"
        "def test_ok():\n"
        f"    pathlib.Path({str(marker)!r}).write_text('x')\n"
        "    deadline = time.monotonic() + 30\n"
        f"    while not pathlib.Path({str(release)!r}).exists() and time.monotonic() < deadline:\n"
        "        time.sleep(0.02)\n", encoding="utf-8")
    (core,) = (root / ".planted" / "core").glob("*.whl")
    env = planted.environ(root / ".planted" / "lock", str(core), root / ".bundle-venvs")
    gate = subprocess.Popen([sys.executable, str(root / "scripts" / "gate.py"), "--results",
                             str(root / "out"), "bundles", "--bundle", "second-app"], cwd=root,
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 60
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert marker.exists(), "the bundle never started"
    runner = next(int(line.split()[1].rstrip(",")) for line in
                  (root / ".bundle-venvs" / ".runner.lock").read_text().splitlines())
    os.kill(gate.pid, signal.SIGKILL)
    os.kill(runner, signal.SIGKILL)
    gate.wait()
    try:
        run = _gate(root, "bundles", "--bundle", "first-app", results="out")
        assert run.returncode == 2, run.stdout + run.stderr
        assert "--results" in run.stderr
    finally:
        release.touch()
