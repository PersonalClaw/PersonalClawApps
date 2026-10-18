"""The quality check judges each ``tested`` claim from the bundle results the ``tests`` job wrote.

Core's verifier, ``python -m personalclaw.apps.quality TREE --results DIR``, reads
``DIR/results.json``'s ``apps`` list and starts no pytest: a claim with a ``fail`` result, or with
no result at all, is a violation. This file holds the apps half of that contract:

- the bundle runner's ``results.json`` carries that ``apps`` list (``pass`` only for a result of
  0, with each bundle's JUnit), and core's real verifier accepts it, run over a planted tree: a
  passing claimant passes; a failing one, and one the run never ran, each fail the verifier;
- ``--changed`` always runs every bundle that claims ``quality.tested`` and says why, so a pull
  request that touches none of them still records a result for each;
- ``ci.yml``: the ``tests`` job's ``quality-declarations`` step runs the verifier once, after the
  gate step and whatever its verdict, over the ``bundles`` folder that step just wrote; no other
  job runs the verifier or downloads results to judge, and the tests job still uploads its
  results folder; the canary passes its own gate's results.

Each workflow rule runs over the real file and over a planted violation of it.
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import planted_envs as planted

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
TESTED = {"tested": True}
FAILS = {"test_fails.py": "def test_fails():\n    assert False\n"}


# -- the runner's results.json, read by core's verifier ----------------------------------------

@pytest.fixture
def world(tmp_path):
    root = planted.tree(tmp_path)
    index = planted.Index(tmp_path / "index")
    core = planted.stub_core(tmp_path / "core")
    return root, index, core


def _verify(root: Path, results: Path) -> subprocess.CompletedProcess:
    """Core's own verifier, as the tests job's quality step runs it, over the planted tree."""
    return subprocess.run([sys.executable, "-m", "personalclaw.apps.quality", str(root),
                           "--results", str(results)], cwd=root, capture_output=True, text=True)


def test_the_runner_writes_the_apps_list_core_s_verifier_reads(world):
    root, index, core = world
    planted.bundle(root, "claiming-app", quality=TESTED)
    planted.bundle(root, "failing-app", tests=FAILS)
    run = planted.runner(root, planted.lock(root, index), core, "--jobs", "2", "--results", "out")
    assert run.returncode == 1, run.stdout + run.stderr
    apps = json.loads((root / "out" / "results.json").read_text(encoding="utf-8"))["apps"]
    assert apps == [
        {"app": "claiming-app", "outcome": "pass", "junit": "claiming-app.junit.xml"},
        {"app": "failing-app", "outcome": "fail", "junit": "failing-app.junit.xml"},
    ]
    for entry in apps:
        assert (root / "out" / entry["junit"]).is_file()
    # failing-app claims nothing, so its fail is ignored; claiming-app's pass backs its claim.
    verified = _verify(root, root / "out")
    assert verified.returncode == 0, verified.stdout + verified.stderr
    assert "every declared claim is backed" in verified.stdout


def test_a_claimant_whose_bundle_failed_fails_the_quality_check(world):
    root, index, core = world
    planted.bundle(root, "claiming-app", quality=TESTED, tests=FAILS)
    run = planted.runner(root, planted.lock(root, index), core, "--results", "out")
    assert run.returncode == 1, run.stdout + run.stderr
    verified = _verify(root, root / "out")
    assert verified.returncode == 1, verified.stdout + verified.stderr
    assert "claiming-app" in verified.stdout and 'records outcome "fail"' in verified.stdout
    assert "claiming-app.junit.xml" in verified.stdout


def test_a_claimant_the_run_never_ran_fails_the_quality_check(world):
    root, index, core = world
    planted.bundle(root, "claiming-app", quality=TESTED)
    planted.bundle(root, "other-app")
    run = planted.runner(root, planted.lock(root, index), core, "other-app", "--results", "out")
    assert run.returncode == 0, run.stdout + run.stderr
    verified = _verify(root, root / "out")
    assert verified.returncode == 1, verified.stdout + verified.stderr
    assert "claiming-app" in verified.stdout
    assert "hold no result for it" in verified.stdout


def test_an_entry_passes_only_for_a_result_of_zero_and_names_only_top_level_apps():
    runner = planted.load(ROOT / "scripts" / "test_bundles.py", "_quality_runner")
    rows = [{"bundle": "b-app", "returncode": 3, "junit": None},
            {"bundle": "a-app", "returncode": 0, "junit": "a-app.junit.xml"},
            {"bundle": "c-app", "returncode": 2, "junit": None},
            {"bundle": "nested/d-app", "returncode": 0, "junit": "nested__d-app.junit.xml"}]
    assert runner.quality_results(rows) == [
        {"app": "a-app", "outcome": "pass", "junit": "a-app.junit.xml"},
        {"app": "b-app", "outcome": "fail"},
        {"app": "c-app", "outcome": "fail"},
    ]


# -- --changed always runs the claimants -------------------------------------------------------

@pytest.fixture
def repo(tmp_path):
    root = planted.tree(tmp_path)
    planted.bundle(root, "claiming-app", quality=TESTED)
    planted.bundle(root, "touched-app")
    planted.bundle(root, "quiet-app", quality={"tested": False})
    (root / "docs").mkdir()
    (root / "docs" / "guide.md").write_text("guide\n", encoding="utf-8")
    planted.git(root, "init", "-q")
    return root, planted.commit(root, "base")


def _selected(root: Path, base: str) -> tuple[list[str], str]:
    run = subprocess.run([sys.executable, str(root / "scripts" / "test_bundles.py"), "--list",
                          "--changed", base], cwd=root, capture_output=True, text=True,
                         env=planted.environ())
    assert run.returncode == 0, run.stdout + run.stderr
    lines = run.stdout.splitlines()
    reason = next(line for line in lines if line.startswith("selection: "))
    return [line for line in lines if line and not line.startswith("selection: ")], reason


def test_changed_always_runs_every_bundle_that_claims_tested_and_says_why(repo):
    root, base = repo
    (root / "touched-app" / "provider.py").write_text("X = 1\n", encoding="utf-8")
    planted.commit(root)
    bundles, reason = _selected(root, base)
    # quiet-app declares tested: false, which claims nothing, so it is not added.
    assert bundles == ["claiming-app", "touched-app"], (bundles, reason)
    assert "plus every bundle claiming quality.tested" in reason and "claiming-app" in reason


def test_a_change_no_bundle_reads_still_runs_the_claimants(repo):
    root, base = repo
    (root / "docs" / "guide.md").write_text("guide, again\n", encoding="utf-8")
    planted.commit(root)
    bundles, reason = _selected(root, base)
    assert bundles == ["claiming-app"], (bundles, reason)
    assert "no bundle" in reason and "claiming-app" in reason


def test_a_touched_claimant_is_named_once(repo):
    root, base = repo
    (root / "claiming-app" / "provider.py").write_text("X = 1\n", encoding="utf-8")
    planted.commit(root)
    bundles, reason = _selected(root, base)
    assert bundles == ["claiming-app"], (bundles, reason)
    assert "plus every bundle" not in reason


# -- the workflows -----------------------------------------------------------------------------

def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _steps(job: dict) -> list[dict]:
    return job.get("steps") or []


def _run(step: dict) -> str:
    return step.get("run") or ""


def _folder(path: str) -> str:
    """One spelling for the runner's temporary folder, as an expression or as a variable."""
    path = re.sub(r"\$\{\{\s*runner\.temp\s*\}\}", "$RUNNER_TEMP", str(path))
    return path.replace("${RUNNER_TEMP}", "$RUNNER_TEMP").strip().strip('"').rstrip("/")


_VERIFY = re.compile(r"python -m personalclaw\.apps\.quality \.(?P<rest>[^\n]*)")
_RESULTS = re.compile(r'--results\s+("[^"]+"|\S+)')
#: Anything that runs a bundle suite, or installs what one needs.
_SUITE = re.compile(r"\bpytest\b|scripts/test_bundles\.py|scripts/test-bundles|"
                    r"scripts/gate\.py[^\n]*\bbundles\b|\$deps\b")


def _verify_results(steps: list[dict]) -> list[str | None]:
    """The --results folder of each step that runs core's verifier (None for one with none)."""
    found = []
    for step in steps:
        for match in _VERIFY.finditer(_run(step)):
            given = _RESULTS.search(match["rest"])
            found.append(_folder(given.group(1)) if given else None)
    return found


def _runs_verifier(step: dict) -> bool:
    return bool(_VERIFY.search(_run(step)))


def ci_quality_problems(wf: dict) -> list[str]:
    """The quality check is a step of the tests job, after its gate step, over the results that
    step just wrote; no other job runs it, and no job downloads results to run it."""
    problems: list[str] = []
    jobs = wf.get("jobs") or {}
    tests = jobs.get("tests") or {}
    steps = _steps(tests)
    gate_at = [i for i, s in enumerate(steps) if "scripts/gate.py" in _run(s)]
    written = None
    if len(gate_at) != 1 or not re.search(r"\bbundles\b", _run(steps[gate_at[0]])):
        problems.append("tests: no single step runs the gate's bundles part")
    else:
        given = _RESULTS.search(_run(steps[gate_at[0]]))
        written = _folder(given.group(1)) if given else None
        if written is None:
            problems.append("tests: the gate writes no results folder")
    uploads = [s for s in steps
               if str(s.get("uses", "")).startswith("actions/upload-artifact@")
               and _folder((s.get("with") or {}).get("path", "")) == written]
    if not uploads:
        problems.append("tests: never uploads the gate's results folder")
    elif "always()" not in str(uploads[0].get("if", "")) and \
            "!cancelled()" not in str(uploads[0].get("if", "")):
        problems.append("tests: uploads its results only when the job passed")

    verify_at = [i for i, s in enumerate(steps) if _runs_verifier(s)]
    verify = _verify_results(steps)
    if len(verify) != 1:
        problems.append(f"tests: runs core's verifier {len(verify)} time(s), not once")
    elif verify[0] is None:
        problems.append("tests: runs core's verifier without --results, so it reruns every "
                        "claimant's suite")
    else:
        if written is not None and verify[0] != f"{written}/bundles":
            problems.append(f"tests: --results {verify[0]} is not the bundle results the gate "
                            f"wrote, {written}/bundles")
        if gate_at and verify_at[0] < gate_at[0]:
            problems.append("tests: runs core's verifier before the gate writes its results")
        step = steps[verify_at[0]]
        if step.get("name") != "quality-declarations":
            problems.append(f"tests: the verifier step is named {step.get('name')!r}, not "
                            "'quality-declarations', the name the docs cite")
        if "!cancelled()" not in str(step.get("if", "")) and "always()" not in str(step.get("if", "")):
            problems.append("tests: the quality check runs only when the gate passed, so a red "
                            "bundle's claim goes unreported")
    for name, job in jobs.items():
        if name == "tests":
            continue
        for step in _steps(job):
            if _runs_verifier(step):
                problems.append(f"{name}: runs core's verifier in a job of its own, a runner "
                                "hop after tests")
            if str(step.get("uses", "")).startswith("actions/download-artifact@"):
                problems.append(f"{name}: downloads an artifact to judge what tests already holds")
    return problems


def canary_quality_problems(wf: dict) -> list[str]:
    problems: list[str] = []
    check = (wf.get("jobs") or {}).get("check") or {}
    gate = [s for s in _steps(check) if "scripts/gate.py" in _run(s)]
    given = _RESULTS.search(_run(gate[0])) if len(gate) == 1 else None
    written = _folder(given.group(1)) if given else None
    if written is None:
        problems.append("check: the gate writes no results folder")
    verify = _verify_results(_steps(check))
    if len(verify) != 1:
        problems.append(f"check: runs core's verifier {len(verify)} time(s)")
    elif verify[0] is None:
        problems.append("check: runs core's verifier without --results, so it reruns every "
                        "claimant's suite")
    elif written is not None and verify[0] != f"{written}/bundles":
        problems.append(f"check: --results {verify[0]} is not the gate's bundle results, "
                        f"{written}/bundles")
    for step in _steps(check):
        if re.search(r"\$deps\b", _run(step)):
            problems.append(f"check: step {step.get('name')!r} installs the claimants' ranges")
    return problems


def test_the_tests_job_s_quality_step_reads_the_results_it_just_wrote():
    assert ci_quality_problems(_load("ci.yml")) == []


def test_the_canary_s_quality_check_reads_its_own_gate_s_results():
    assert canary_quality_problems(_load("core-main-canary.yml")) == []


def _job(wf, name):
    return wf["jobs"][name]


def _step(wf, job, needle):
    return next(s for s in _steps(_job(wf, job)) if needle in _run(s) or needle in
                str(s.get("uses", "")))


def _replace(wf, job, needle, old, new):
    step = _step(wf, job, needle)
    assert old in step["run"], step["run"]
    step["run"] = step["run"].replace(old, new)


def _add(wf, job, step, at=-1):
    steps = _job(wf, job)["steps"]
    steps.insert(len(steps) if at == -1 else at, step)


def _verifier_step(wf):
    return next(s for s in _steps(_job(wf, "tests")) if _runs_verifier(s))


def _move_verifier_before_gate(wf):
    steps = _job(wf, "tests")["steps"]
    step = _verifier_step(wf)
    steps.remove(step)
    steps.insert(next(i for i, s in enumerate(steps) if "scripts/gate.py" in _run(s)), step)


def _split_back_into_a_job(wf):
    step = _verifier_step(wf)
    _job(wf, "tests")["steps"].remove(step)
    wf["jobs"]["quality-declarations"] = {"needs": ["tests"], "steps": [
        {"uses": "actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333",
         "with": {"name": "gate-tests", "path": "${{ runner.temp }}/gate"}}, step]}


CI_PLANTS = {
    "the verifier without --results": (
        lambda wf: _replace(wf, "tests", "personalclaw.apps.quality",
                            ' --results "$RUNNER_TEMP/gate/bundles"', ""),
        "without --results"),
    "--results pointing elsewhere": (
        lambda wf: _replace(wf, "tests", "personalclaw.apps.quality",
                            "$RUNNER_TEMP/gate/bundles", "$RUNNER_TEMP/elsewhere"),
        "is not the bundle results the gate wrote"),
    "no quality step at all": (
        lambda wf: _job(wf, "tests")["steps"].remove(_verifier_step(wf)),
        "runs core's verifier 0 time(s)"),
    "the verifier run twice": (
        lambda wf: _add(wf, "tests", dict(_verifier_step(wf), name="planted")),
        "runs core's verifier 2 time(s)"),
    "the verifier before the gate": (_move_verifier_before_gate, "before the gate writes"),
    "the verifier only on a green gate": (
        lambda wf: _verifier_step(wf).pop("if"),
        "runs only when the gate passed"),
    "the step renamed": (
        lambda wf: _verifier_step(wf).__setitem__("name", "Quality"),
        "not 'quality-declarations'"),
    "split back into a job of its own": (_split_back_into_a_job, "in a job of its own"),
    "an artifact downloaded elsewhere": (
        lambda wf: _add(wf, "static", {"uses": "actions/download-artifact@x"}),
        "static: downloads an artifact"),
    "the tests job keeps no results": (
        lambda wf: _job(wf, "tests")["steps"].remove(
            _step(wf, "tests", "actions/upload-artifact@")),
        "never uploads"),
    "results uploaded only on a green tests job": (
        lambda wf: _step(wf, "tests", "actions/upload-artifact@").pop("if"),
        "only when the job passed"),
}


@pytest.mark.parametrize("plant", sorted(CI_PLANTS))
def test_a_planted_quality_job_violation_is_caught(plant):
    wf = copy.deepcopy(_load("ci.yml"))
    mutate, expected = CI_PLANTS[plant]
    mutate(wf)
    problems = ci_quality_problems(wf)
    assert any(expected in p for p in problems), problems


CANARY_PLANTS = {
    "the verifier without --results": (
        lambda wf: _replace(wf, "check", "personalclaw.apps.quality",
                            ' --results "$RUNNER_TEMP/gate/bundles"', ""),
        "without --results"),
    "--results pointing elsewhere": (
        lambda wf: _replace(wf, "check", "personalclaw.apps.quality",
                            "$RUNNER_TEMP/gate/bundles", "$RUNNER_TEMP/other"),
        "is not the gate's bundle results"),
    "the claimants' ranges installed again": (
        lambda wf: _add(wf, "check", {"name": "planted", "run": "uv pip install $deps"}),
        "installs the claimants' ranges"),
}


@pytest.mark.parametrize("plant", sorted(CANARY_PLANTS))
def test_a_planted_canary_quality_violation_is_caught(plant):
    wf = copy.deepcopy(_load("core-main-canary.yml"))
    mutate, expected = CANARY_PLANTS[plant]
    mutate(wf)
    problems = canary_quality_problems(wf)
    assert any(expected in p for p in problems), problems
