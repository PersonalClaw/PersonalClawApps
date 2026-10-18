"""Every setup-uv step that keeps uv's cache between runs prunes it before saving.

setup-uv saves uv's cache (``UV_CACHE_DIR``) at the end of a job and restores it at the start of
the next one. Pruned for CI (``uv cache prune --ci``, which setup-uv runs when ``prune-cache`` is
true), the cache keeps only the wheels uv had to build from source, a few hundred MB here.
Unpruned, it keeps every wheel every bundle environment downloaded: 3.2 GB in this repository,
which each run then restores before anything else (35-62 s), to save downloads the bundle
environments are fast at anyway. The bundles part took 128-171 s with the pruned cache and
138-171 s with the full one.

setup-uv's own default changed under us: ``prune-cache`` defaulted to true up to v7 and defaults
to false from v10. So a version bump alone turned pruning off and changed the cache key, and the
next run saved the full cache. The rule does not lean on the default: every setup-uv step either
turns caching off (``enable-cache: false``) or sets ``prune-cache: true`` itself. ``enable-cache``
left out means ``auto``, which caches on GitHub's runners, so it is held to the rule too.

The planted workflows below show the rule fails on a caching step with no ``prune-cache``, with
``prune-cache: false`` and with ``enable-cache`` left to ``auto``, and passes on a pruned step and
on one that caches nothing.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


def _flag(value) -> str:
    """A ``with:`` input as setup-uv reads it: YAML's true is the string "true"."""
    return str(value).strip().lower()


def setup_uv_steps(workflow: dict) -> list[tuple[str, dict]]:
    """``(job id, step)`` for every setup-uv step in one parsed workflow."""
    found = []
    for job_id, job in (workflow.get("jobs") or {}).items():
        for step in (job or {}).get("steps") or []:
            if str(step.get("uses", "")).startswith("astral-sh/setup-uv@"):
                found.append((job_id, step))
    return found


def unpruned_caches(name: str, workflow: dict) -> list[str]:
    """One problem line for each setup-uv step that would save uv's cache unpruned."""
    problems = []
    for job_id, step in setup_uv_steps(workflow):
        given = step.get("with") or {}
        enabled = _flag(given.get("enable-cache", "auto"))
        if enabled == "false":
            continue
        if _flag(given.get("prune-cache", "")) != "true":
            where = f"{name} {job_id} {step.get('name') or step['uses'].split('@')[0]!r}"
            problems.append(f"{where}: keeps uv's cache (enable-cache: {enabled}) without "
                            "prune-cache: true, so it saves every downloaded wheel and every "
                            "run restores them")
    return problems


def _workflows() -> dict[str, dict]:
    return {p.name: yaml.safe_load(p.read_text(encoding="utf-8"))
            for p in sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])}


def test_the_rule_reads_every_setup_uv_step_the_workflows_have():
    # Vacuity floor: the static and tests jobs, the bump and the canary each install uv.
    steps = [s for wf in _workflows().values() for s in setup_uv_steps(wf)]
    assert len(steps) >= 4, f"only {len(steps)} setup-uv steps found — did the action move?"


def test_every_setup_uv_step_that_caches_prunes_first():
    problems = [line for name, wf in _workflows().items() for line in unpruned_caches(name, wf)]
    assert not problems, "\n".join(problems)


# -- planted workflows ------------------------------------------------------------------------

SETUP_UV = "astral-sh/setup-uv@1c37ad07a6a961277cf70c0d37d6f313000f5884"
PLANTED = {"jobs": {"build": {"steps": [{"name": "Install uv", "uses": SETUP_UV,
                                          "with": {"version": "0.13.1", "enable-cache": True,
                                                   "prune-cache": True}}]}}}


def _planted(**with_inputs) -> dict:
    workflow = copy.deepcopy(PLANTED)
    given = workflow["jobs"]["build"]["steps"][0]["with"]
    for key, value in with_inputs.items():
        if value is None:
            given.pop(key.replace("_", "-"), None)
        else:
            given[key.replace("_", "-")] = value
    return workflow


def test_the_planted_baseline_passes():
    assert unpruned_caches("planted.yml", _planted()) == []


@pytest.mark.parametrize(("inputs", "shown"), [
    ({"prune_cache": None}, "enable-cache: true"),
    ({"prune_cache": False}, "enable-cache: true"),
    ({"prune_cache": "false"}, "enable-cache: true"),
    ({"enable_cache": None, "prune_cache": None}, "enable-cache: auto"),
    ({"enable_cache": "auto", "prune_cache": None}, "enable-cache: auto"),
], ids=["no prune-cache", "prune-cache false", "prune-cache 'false'", "enable-cache left out",
        "enable-cache auto"])
def test_a_planted_step_that_saves_the_cache_unpruned_fails(inputs, shown):
    problems = unpruned_caches("planted.yml", _planted(**inputs))
    assert len(problems) == 1 and problems[0].startswith("planted.yml build 'Install uv'"), problems
    assert shown in problems[0] and "prune-cache: true" in problems[0]


@pytest.mark.parametrize("inputs", [
    {"enable_cache": False, "prune_cache": None},
    {"enable_cache": "false", "prune_cache": None},
    {"prune_cache": "true"},
], ids=["caching off", "caching off as a string", "prune-cache as a string"])
def test_a_planted_step_that_caches_nothing_or_prunes_passes(inputs):
    assert unpruned_caches("planted.yml", _planted(**inputs)) == []


def test_a_step_of_another_action_is_not_read():
    workflow = {"jobs": {"build": {"steps": [{"uses": "actions/setup-node@x",
                                               "with": {"cache": "npm"}}]}}}
    assert setup_uv_steps(workflow) == [] and unpruned_caches("planted.yml", workflow) == []
