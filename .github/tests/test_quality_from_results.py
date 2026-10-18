"""The quality check judges each ``tested`` claim from the bundle results the ``tests`` job wrote.

Core's verifier, ``python -m personalclaw.apps.quality TREE --results DIR``, reads
``DIR/results.json``'s ``apps`` list and starts no pytest: a claim with a ``fail`` result, or with
no result at all, is a violation. This file holds the apps half of that contract:

- the bundle runner's ``results.json`` carries that ``apps`` list (``pass`` only for a result of
  0, with each bundle's JUnit), and core's real verifier accepts it, run over a planted tree: a
  passing claimant passes; a failing one, and one the run never ran, each fail the verifier;
- ``--changed`` always runs every bundle that claims ``quality.tested`` and says why, so a pull
  request that touches none of them still records a result for each;
- ``ci.yml``: the ``tests`` job's one gate run runs the ``bundles`` and ``quality`` parts, so the
  gate's quality part judges the bundle results that same run wrote; its ``quality-declarations``
  step, after it and whatever its verdict, reads that verdict back with ``gate.py --verdict
  quality`` from the same folder under the locked core. Nothing in CI calls core's verifier
  except through the gate, no other job judges or downloads results to judge, and the tests job
  still uploads its results folder; the canary passes its own gate's results.

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


#: A gate run, and the parts it names (``--verdict`` runs none, so it is not one).
_GATE_RUN = re.compile(r"scripts/gate\.py(?P<parts>(?:\s+(?:static|rails|bundles|quality)\b)*)")
_VERDICT = re.compile(r"scripts/gate\.py\s+--verdict\s+quality\b")


def _gate_parts(step: dict) -> list[str] | None:
    """The parts a step's gate run names, or None for a step that starts no gate run."""
    run = _run(step)
    if "scripts/gate.py" not in run or "--verdict" in run:
        return None
    found = _GATE_RUN.search(run)
    return found["parts"].split() if found else []


def _always(step: dict) -> bool:
    return "!cancelled()" in str(step.get("if", "")) or "always()" in str(step.get("if", ""))


def ci_quality_problems(wf: dict) -> list[str]:
    """The tests job's one gate run runs the ``bundles`` and ``quality`` parts, so the claims are
    judged from the results that same run wrote; its ``quality-declarations`` step, after it and
    whatever its verdict, reads that verdict back from the run's receipt with ``--verdict
    quality`` under the locked core. Nothing in CI calls core's verifier but the gate, no other
    job judges, and no job downloads results to judge."""
    problems: list[str] = []
    jobs = wf.get("jobs") or {}
    tests = jobs.get("tests") or {}
    steps = _steps(tests)
    gate_at = [i for i, s in enumerate(steps) if _gate_parts(s) is not None]
    written = None
    if len(gate_at) != 1:
        problems.append(f"tests: {len(gate_at)} steps start a gate run, not one; a second run "
                        "clears the first's results before its quality part could judge them")
    if gate_at:
        parts = _gate_parts(steps[gate_at[0]]) or []
        if "bundles" not in parts:
            problems.append("tests: the gate step does not run the bundles part")
        if "quality" not in parts:
            problems.append("tests: the gate step does not run the quality part, so no claim is "
                            "judged from this run's bundle results")
        given = _RESULTS.search(_run(steps[gate_at[0]]))
        written = _folder(given.group(1)) if given else None
        if written is None:
            problems.append("tests: the gate writes no results folder")
    uploads = [s for s in steps
               if str(s.get("uses", "")).startswith("actions/upload-artifact@")
               and _folder((s.get("with") or {}).get("path", "")) == written]
    if not uploads:
        problems.append("tests: never uploads the gate's results folder")
    elif not _always(uploads[0]):
        problems.append("tests: uploads its results only when the job passed")

    verdict_at = [i for i, s in enumerate(steps) if _VERDICT.search(_run(s))]
    if len(verdict_at) != 1:
        problems.append(f"tests: reads the gate's quality verdict {len(verdict_at)} time(s), "
                        "not once")
    else:
        step = steps[verdict_at[0]]
        if gate_at and verdict_at[0] < gate_at[0]:
            problems.append("tests: reads the quality verdict before the gate step runs")
        given = _RESULTS.search(_run(step))
        read = _folder(given.group(1)) if given else None
        if written is not None and read != written:
            problems.append(f"tests: the verdict step reads --results {read}, not the folder the "
                            f"gate wrote, {written}")
        if '--core "$CORE_SHA"' not in _run(step):
            problems.append('tests: the verdict step reads the receipt without --core "$CORE_SHA", '
                            "so a receipt for another core would do")
        if step.get("name") != "quality-declarations":
            problems.append(f"tests: the verdict step is named {step.get('name')!r}, not "
                            "'quality-declarations', the name the docs cite")
        if not _always(step):
            problems.append("tests: the quality verdict is read only when the gate passed, so a "
                            "red bundle's claim goes unreported")
    for name, job in jobs.items():
        for step in _steps(job):
            if _runs_verifier(step):
                where = ("calls core's verifier directly, not through the gate's quality part"
                         if name == "tests" else
                         "runs core's verifier in a job of its own, a runner hop after tests")
                problems.append(f"{name}: {where}")
            if name != "tests" and str(step.get("uses", "")).startswith(
                    "actions/download-artifact@"):
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


def test_the_tests_job_judges_claims_in_its_gate_run_and_shows_the_verdict_on_its_own_step():
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


_GATE_STEP = "scripts/gate.py bundles"


def _verdict_step(wf):
    return next(s for s in _steps(_job(wf, "tests")) if _VERDICT.search(_run(s)))


def _move_verdict_before_gate(wf):
    steps = _job(wf, "tests")["steps"]
    step = _verdict_step(wf)
    steps.remove(step)
    steps.insert(next(i for i, s in enumerate(steps) if _GATE_STEP in _run(s)), step)


def _verdict_runs_the_part_again(wf):
    """A second gate run for the quality part: it clears the bundle results the first wrote."""
    step = _verdict_step(wf)
    step["run"] = 'python scripts/gate.py quality --core "$CORE_SHA" --results "$RUNNER_TEMP/gate"'


def _module_called_directly(wf):
    """The shape before the gate had a quality part: the step calls core's verifier itself."""
    _replace(wf, "tests", _GATE_STEP, " bundles quality ", " bundles ")
    _verdict_step(wf)["run"] = ('python -m personalclaw.apps.quality . --results '
                                '"$RUNNER_TEMP/gate/bundles"')


def _split_back_into_a_job(wf):
    step = _verdict_step(wf)
    _job(wf, "tests")["steps"].remove(step)
    wf["jobs"]["quality-declarations"] = {"needs": ["tests"], "steps": [
        {"uses": "actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333",
         "with": {"name": "gate-tests", "path": "${{ runner.temp }}/gate"}},
        {"run": 'python -m personalclaw.apps.quality . --results "$RUNNER_TEMP/gate/bundles"'}]}


CI_PLANTS = {
    "the gate step without the quality part": (
        lambda wf: _replace(wf, "tests", _GATE_STEP, " bundles quality ", " bundles "),
        "does not run the quality part"),
    "the gate step without the bundles part": (
        lambda wf: _replace(wf, "tests", _GATE_STEP, " bundles quality ", " quality "),
        "does not run the bundles part"),
    "the quality part run again by a second gate run": (
        _verdict_runs_the_part_again, "2 steps start a gate run"),
    "core's verifier called directly again": (
        _module_called_directly, "calls core's verifier directly"),
    "no verdict step at all": (
        lambda wf: _job(wf, "tests")["steps"].remove(_verdict_step(wf)),
        "reads the gate's quality verdict 0 time(s)"),
    "the verdict read twice": (
        lambda wf: _add(wf, "tests", dict(_verdict_step(wf), name="planted"), at=-2),
        "reads the gate's quality verdict 2 time(s)"),
    "the verdict before the gate": (_move_verdict_before_gate, "before the gate step runs"),
    "the verdict from another folder": (
        lambda wf: _replace(wf, "tests", "--verdict quality", '"$RUNNER_TEMP/gate"',
                            '"$RUNNER_TEMP/elsewhere"'),
        "not the folder the gate wrote"),
    "the verdict without --core": (
        lambda wf: _replace(wf, "tests", "--verdict quality", ' --core "$CORE_SHA"', ""),
        'without --core "$CORE_SHA"'),
    "the verdict only on a green gate": (
        lambda wf: _verdict_step(wf).pop("if"),
        "read only when the gate passed"),
    "the step renamed": (
        lambda wf: _verdict_step(wf).__setitem__("name", "Quality"),
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
