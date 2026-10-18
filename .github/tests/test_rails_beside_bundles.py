"""The rails run in CI's ``static`` job, beside the bundles, never ahead of them in ``tests``.

A pull request's verdict waits on its longest job, and that is ``tests``: it builds the bundle
environments and runs each bundle's suite. When its one gate run also ran the ``rails`` part,
the rails' fixed cost (the repository's own tests, about half a minute) came first in that
job, ahead of every bundle, on every pull request. ``static`` finishes well before ``tests``,
so the rails run there instead, in the same gate run as the static checks, and ``tests`` starts
its bundles at once.

The rule, over ``ci.yml``:

- exactly one job runs the ``rails`` part, in exactly one gate step, and it is not ``tests``;
- that job needs no other job, so the rails start with the run, not after a job ends;
- it installs pytest at the pins every bundle environment holds (``bundle_lock.BASE``), which
  the rails' xdist workers and asyncio tests need;
- it sets ``PERSONALCLAW_SKIP_APP_BACKENDS``, as ``tests`` did for the rails, so loading core
  starts no app backend;
- the ``tests`` job's gate runs ``bundles`` and ``quality`` and no ``rails``.

The real file passes it, and each planted violation below fails only its own line.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest
import yaml

import planted_envs as planted

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".github" / "workflows" / "ci.yml"

_GATE_RUN = re.compile(r"scripts/gate\.py(?P<parts>(?:\s+(?:static|rails|bundles|quality)\b)*)")


def _base_pins() -> list[str]:
    return planted.load(ROOT / "scripts" / "bundle_lock.py", "_rails_bundle_lock").BASE


def _steps(job: dict) -> list[dict]:
    return job.get("steps") or []


def _run(step: dict) -> str:
    return str(step.get("run") or "")


def _parts(step: dict) -> list[str] | None:
    """The parts a step's gate run names, or None for a step that starts no gate run."""
    run = _run(step)
    if "scripts/gate.py" not in run or "--verdict" in run:
        return None
    found = _GATE_RUN.search(run)
    return found["parts"].split() if found else []


def rails_problems(wf: dict) -> list[str]:
    problems: list[str] = []
    jobs = wf.get("jobs") or {}
    running = [(name, step) for name, job in jobs.items() for step in _steps(job)
               if "rails" in (_parts(step) or [])]
    if len(running) != 1:
        problems.append(f"{len(running)} gate steps run the rails part, not one")
    for name, step in running[:1]:
        job = jobs[name]
        if name == "tests":
            problems.append("tests runs the rails part, ahead of every bundle")
            continue
        if job.get("needs"):
            problems.append(f"{name}: needs {job['needs']!r}, so the rails wait for another job")
        installs = "\n".join(_run(s) for s in _steps(job))
        missing = [pin for pin in _base_pins() if pin not in installs.split()]
        if missing:
            problems.append(f"{name}: does not install {', '.join(missing)}, which the rails "
                            "need")
        env = {**(job.get("env") or {}), **(step.get("env") or {})}
        if str(env.get("PERSONALCLAW_SKIP_APP_BACKENDS")) != "1":
            problems.append(f"{name}: runs the rails without PERSONALCLAW_SKIP_APP_BACKENDS=1")
    gates = [_parts(s) for s in _steps(jobs.get("tests") or {}) if _parts(s) is not None]
    if not gates or not any({"bundles", "quality"} <= set(p) for p in gates):
        problems.append("tests: no gate step runs the bundles and quality parts")
    return problems


def _load() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))


def _gate(wf: dict, job: str) -> dict:
    return next(s for s in _steps(wf["jobs"][job]) if _parts(s) is not None)


def test_ci_runs_the_rails_once_beside_the_bundles():
    assert rails_problems(_load()) == []


def _rails_back_in_tests(wf):
    _gate(wf, "static")["run"] = _gate(wf, "static")["run"].replace(" static rails ", " static ")
    _gate(wf, "tests")["run"] = _gate(wf, "tests")["run"].replace(" bundles quality ",
                                                                  " rails bundles quality ")


def _rails_twice(wf):
    _gate(wf, "tests")["run"] = _gate(wf, "tests")["run"].replace(" bundles quality ",
                                                                  " rails bundles quality ")


def _rails_after_tests(wf):
    wf["jobs"]["static"]["needs"] = ["tests"]


def _no_xdist(wf):
    for step in _steps(wf["jobs"]["static"]):
        if "pytest-xdist==" in _run(step):
            step["run"] = re.sub(r"\s*pytest-xdist==\S+", "", step["run"])


def _backends_on(wf):
    wf["jobs"]["static"].get("env", {}).pop("PERSONALCLAW_SKIP_APP_BACKENDS", None)
    _gate(wf, "static").get("env", {}).pop("PERSONALCLAW_SKIP_APP_BACKENDS", None)


def _no_rails(wf):
    _gate(wf, "static")["run"] = _gate(wf, "static")["run"].replace(" static rails ", " static ")


def _tests_without_quality(wf):
    _gate(wf, "tests")["run"] = _gate(wf, "tests")["run"].replace(" bundles quality ",
                                                                  " bundles ")


PLANTS = {
    "the rails back in the tests job": (_rails_back_in_tests, "tests runs the rails part"),
    "the rails run in both jobs": (_rails_twice, "2 gate steps run the rails part"),
    "no job runs the rails": (_no_rails, "0 gate steps run the rails part"),
    "the rails job waits on tests": (_rails_after_tests, "so the rails wait for another job"),
    "the rails job without pytest-xdist": (_no_xdist, "does not install pytest-xdist=="),
    "the rails with app backends on": (_backends_on, "without PERSONALCLAW_SKIP_APP_BACKENDS"),
    "tests without the quality part": (_tests_without_quality, "tests: no gate step runs"),
}


@pytest.mark.parametrize("plant", sorted(PLANTS))
def test_a_planted_rails_placement_is_caught(plant):
    edit, expected = PLANTS[plant]
    wf = copy.deepcopy(_load())
    edit(wf)
    problems = rails_problems(wf)
    assert len(problems) == 1 and expected in problems[0], problems
