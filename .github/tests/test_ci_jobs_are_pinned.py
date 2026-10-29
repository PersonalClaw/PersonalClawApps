"""``ci.yml``'s three jobs run exactly the commands this file names, and nothing can skip them.

The merge verdict reads an apps run by its jobs (core's #5516): ``static``, ``tests`` and
``audit``, each green on every attempt. A pull request runs its own ``ci.yml``, so a head that
changed the file could keep the job names and run nothing. A head that changes a CI definition is
therefore also judged by main's copy of these rails over its tree, and this file is the rail that
says what the jobs must run: a ``run: true`` in place of a gate step, a dropped or added step, a
step or job a condition can skip or whose failure is forgiven, or a changed command fails.

What is pinned, job by job and step by step, in order:

- every ``run:`` step, by its name, its command, its ``env`` and its ``if``;
- every ``uses:`` step, by its action (the ref is the supply-chain rail's), its name, its
  inputs and its ``if``;
- each job's keys (runner, environment), and the workflow's triggers, permissions and env;
- no ``continue-on-error``, ``needs`` or job-level ``if`` anywhere.

A version is not a command: a pin after ``==``, a ``--python X.Y.Z`` and setup-uv's ``version``
are read as ``<v>``, and so is a job's ``timeout-minutes``, so a version bump needs no edit here.
Any other change to the jobs changes this file in the same pull request, and lands in two: this
rail first, taking both shapes, then the change.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".github" / "workflows" / "ci.yml"

_VERSION = re.compile(r"(==|--python )\d[\w.]*")
_IDENTITY = (
    'echo "ImageOS=${ImageOS:-} ImageVersion=${ImageVersion:-} RUNNER_ARCH=${RUNNER_ARCH:-}"\n'
    "for v in ImageOS ImageVersion RUNNER_ARCH; do\n"
    '  if [ -z "${!v:-}" ]; then echo "::error::runner identity: $v is empty"; exit 1; fi\n'
    "done\n"
)
_READ_CORE_LOCK = {
    "name": "Read core.lock",
    "run": "uv run --no-project --python <v> python scripts/core_lock.py export",
}
_INSTALL_CORE = {
    "name": "Install core + pytest",
    "run": (
        "uv venv --python <v> .venv\n"
        'echo "$PWD/.venv/bin" >> "$GITHUB_PATH"\n'
        'echo "VIRTUAL_ENV=$PWD/.venv" >> "$GITHUB_ENV"\n'
        'uv pip install --python "$PWD/.venv/bin/python" "$CORE_SPEC" '
        "pytest==<v> pytest-asyncio==<v> pytest-xdist==<v>\n"
    ),
}
_NODE = {"uses": "actions/setup-node", "with": {"node-version-file": ".nvmrc"}}
_SKIP_BACKENDS = {"PERSONALCLAW_SKIP_APP_BACKENDS": "1"}
_NOT_CANCELLED = "${{ !cancelled() }}"

#: What each job runs, read as :func:`shape` reads the file.
PINNED: dict[str, dict[str, Any]] = {
    "static": {
        "job": {"runs-on": "ubuntu-24.04", "env": _SKIP_BACKENDS},
        "steps": [
            {"name": "Runner identity", "run": _IDENTITY},
            {"uses": "actions/checkout", "with": {"persist-credentials": False}},
            _NODE,
            {
                "uses": "astral-sh/setup-uv",
                "name": "Install uv",
                "with": {"version": "<v>", "enable-cache": True, "prune-cache": True},
            },
            _READ_CORE_LOCK,
            _INSTALL_CORE,
            {
                "name": "Every static check, one row each, then the repository's own tests",
                "run": 'python scripts/gate.py static rails --core "$CORE_SHA" --jobs 4'
                ' --results "$RUNNER_TEMP/gate"',
            },
            {
                "uses": "actions/upload-artifact",
                "name": "Keep the gate's receipt",
                "if": _NOT_CANCELLED,
                "with": {
                    "name": "gate-static",
                    "path": "${{ runner.temp }}/gate",
                    "if-no-files-found": "error",
                },
            },
        ],
    },
    "tests": {
        "job": {"runs-on": "ubuntu-24.04", "env": _SKIP_BACKENDS},
        "steps": [
            {"name": "Runner identity", "run": _IDENTITY},
            {"uses": "actions/checkout", "with": {"fetch-depth": 0, "persist-credentials": False}},
            _NODE,
            {
                "uses": "astral-sh/setup-uv",
                "name": "Install uv",
                "with": {
                    "version": "<v>",
                    "enable-cache": True,
                    "prune-cache": True,
                    "cache-dependency-glob": ".github/bundle-lock/*/uv.lock",
                },
            },
            _READ_CORE_LOCK,
            _INSTALL_CORE,
            {
                "name": "Each bundle's suite, then the quality claims",
                "env": {"BASE_SHA": "${{ github.event.pull_request.base.sha }}"},
                "run": 'python scripts/gate.py bundles quality --core "$CORE_SHA" --jobs 4'
                ' ${BASE_SHA:+--changed "$BASE_SHA"} --results "$RUNNER_TEMP/gate"',
            },
            {
                "name": "quality-declarations",
                "if": _NOT_CANCELLED,
                "run": 'python scripts/gate.py --verdict quality --core "$CORE_SHA"'
                ' --results "$RUNNER_TEMP/gate"',
            },
            {
                "uses": "actions/upload-artifact",
                "name": "Keep the gate's receipt and each bundle's results",
                "if": _NOT_CANCELLED,
                "with": {
                    "name": "gate-tests",
                    "path": "${{ runner.temp }}/gate",
                    "if-no-files-found": "error",
                },
            },
        ],
    },
    "audit": {
        "job": {"runs-on": "ubuntu-24.04"},
        "steps": [
            {"name": "Runner identity", "run": _IDENTITY},
            {"uses": "actions/checkout", "with": {"persist-credentials": False}},
            _NODE,
            {
                "uses": "astral-sh/setup-uv",
                "name": "Install uv",
                "with": {"version": "<v>", "enable-cache": False},
            },
            {
                "name": "pip-audit, in a venv of its own",
                "run": 'uv venv --python <v> "$RUNNER_TEMP/audit-venv"\n'
                'uv pip install --python "$RUNNER_TEMP/audit-venv/bin/python" pip-audit==<v>\n',
            },
            {
                "name": "Audit the locks the apps ship",
                "run": '"$RUNNER_TEMP/audit-venv/bin/python" scripts/audit_locks.py run'
                ' --pip-audit "$RUNNER_TEMP/audit-venv/bin/python" --work "$RUNNER_TEMP/audit"',
            },
        ],
    },
}
#: The step PersonalClaw/PersonalClawApps#166 adds after the static job's checkout: its
#: ``provider-ids`` row compares each app's providers with its manifest at the latest ``v*`` release,
#: and the shallow checkout has no tags. It fetches exactly one tag, at depth 1, and fails when
#: origin has none.
_FETCH_TAG = {
    "name": "Fetch the latest release tag",
    "run": (
        "tag=$(git ls-remote --tags --refs origin 'v*' | sed 's#.*refs/tags/##'"
        " | grep -E '^v[0-9]+(\\.[0-9]+)*$' | sort -V | tail -n 1)\n"
        'if [ -z "$tag" ]; then echo "::error::no v* release tag on origin"; exit 1; fi\n'
        'git fetch --depth 1 --no-tags origin "refs/tags/$tag:refs/tags/$tag"\n'
    ),
}

#: The other shapes a job may have while a change to it lands in two (the docstring): the rail
#: first, taking both, then the change, which drops the old one. The static job with #166's tag
#: fetch after its checkout.
ALSO: dict[str, list[list[dict[str, Any]]]] = {
    "static": [
        [*PINNED["static"]["steps"][:2], _FETCH_TAG, *PINNED["static"]["steps"][2:]],
    ],
}

WORKFLOW = {
    "on": {"pull_request": None, "push": {"branches": ["main"]}},
    "permissions": {"contents": "read"},
    "env": {"UV_PYTHON_PREFERENCE": "only-managed"},
}


def _versionless(value: Any) -> Any:
    if isinstance(value, str):
        return _VERSION.sub(lambda m: m.group(1) + "<v>", value)
    if isinstance(value, dict):
        return {k: _versionless(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_versionless(v) for v in value]
    return value


def _step(step: dict[str, Any]) -> dict[str, Any]:
    read = dict(step)
    if "uses" in read:
        read["uses"] = str(read["uses"]).split("@", 1)[0]
        if read["uses"] == "astral-sh/setup-uv" and "version" in (read.get("with") or {}):
            read["with"] = {**read["with"], "version": "<v>"}
    return _versionless(read)


def shape(workflow: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The jobs as they are pinned: steps in order, versions and timeouts read as nothing."""
    jobs = workflow.get("jobs") or {}
    return {
        job_id: {
            "job": {k: v for k, v in job.items() if k not in ("steps", "timeout-minutes")},
            "steps": [_step(s) for s in job.get("steps") or []],
        }
        for job_id, job in jobs.items()
    }


def violations(workflow: dict[str, Any]) -> list[str]:
    found: list[str] = []
    top = {"on": workflow.get("on", workflow.get(True)), **workflow}
    for key, want in WORKFLOW.items():
        if top.get(key) != want:
            found.append(f"ci.yml's {key} is {top.get(key)!r}, not {want!r}")
    jobs = shape(workflow)
    if set(jobs) != set(PINNED):
        found.append(f"ci.yml has jobs {sorted(jobs)}, not {sorted(PINNED)}")
    for job_id, want in PINNED.items():
        got = jobs.get(job_id)
        if got is None:
            continue
        if got["job"] != want["job"]:
            found.append(f"{job_id} is set {got['job']!r}, not {want['job']!r}")
        if got["steps"] in ALSO.get(job_id, []):
            continue
        if len(got["steps"]) != len(want["steps"]):
            found.append(f"{job_id} has {len(got['steps'])} steps, not {len(want['steps'])}")
        for n, (step, pinned) in enumerate(zip(got["steps"], want["steps"]), start=1):
            if step != pinned:
                found.append(f"{job_id} step {n} is {step!r}, not {pinned!r}")
    return found


def _load() -> dict[str, Any]:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))


def test_the_committed_ci_runs_exactly_the_pinned_jobs() -> None:
    assert violations(_load()) == []


def _job(wf: dict[str, Any], job: str) -> list[dict[str, Any]]:
    return wf["jobs"][job]["steps"]


def _named(wf: dict[str, Any], job: str, name: str) -> dict[str, Any]:
    return next(s for s in _job(wf, job) if s.get("name") == name)


_STATIC = "Every static check, one row each, then the repository's own tests"
_TESTS = "Each bundle's suite, then the quality claims"
_AUDIT = "Audit the locks the apps ship"

_PLANTS = {
    "the static gate replaced by run: true": (
        lambda wf: _named(wf, "static", _STATIC).update(run="true"),
        "static step 7",
    ),
    "the bundles gate replaced by run: true": (
        lambda wf: _named(wf, "tests", _TESTS).update(run="true"),
        "tests step 7",
    ),
    "the audit replaced by run: true": (
        lambda wf: _named(wf, "audit", _AUDIT).update(run="true"),
        "audit step 6",
    ),
    "the quality verdict dropped": (
        lambda wf: _job(wf, "tests").remove(_named(wf, "tests", "quality-declarations")),
        "tests has 8 steps, not 9",
    ),
    "a red forgiven with || true": (
        lambda wf: _named(wf, "static", _STATIC).update(
            run=_named(wf, "static", _STATIC)["run"] + " || true"
        ),
        "static step 7",
    ),
    "a gate step a condition can skip": (
        lambda wf: _named(wf, "tests", _TESTS).update({"if": "false"}),
        "tests step 7",
    ),
    "a gate step whose failure is forgiven": (
        lambda wf: _named(wf, "audit", _AUDIT).update({"continue-on-error": True}),
        "audit step 6",
    ),
    "a job a condition can skip": (
        lambda wf: wf["jobs"]["audit"].update({"if": "false"}),
        "audit is set",
    ),
    "a job whose failure is forgiven": (
        lambda wf: wf["jobs"]["static"].update({"continue-on-error": True}),
        "static is set",
    ),
    "a step that rewrites the gate before it runs": (
        lambda wf: _job(wf, "static").insert(
            6, {"name": "prepare", "run": "echo 'raise SystemExit(0)' > scripts/gate.py"}
        ),
        "static has 9 steps, not 8",
    ),
    "an install that no longer reads core.lock": (
        lambda wf: _named(wf, "tests", "Install core + pytest").update(
            run=_named(wf, "tests", "Install core + pytest")["run"].replace(
                '"$CORE_SPEC"',
                '"personalclaw @ git+https://github.com/PersonalClaw/PersonalClaw@main"',
            )
        ),
        "tests step 6",
    ),
    "a renamed job": (lambda wf: wf["jobs"].update(audit2=wf["jobs"].pop("audit")), "has jobs"),
    "a workflow that runs on a dispatch too": (
        lambda wf: wf.update({True: {**wf[True], "workflow_dispatch": None}}),
        "ci.yml's on",
    ),
    "a local action in a job": (
        lambda wf: _job(wf, "tests").insert(2, {"uses": "./.github/actions/setup"}),
        "tests has 10 steps, not 9",
    ),
}


@pytest.mark.parametrize("plant", list(_PLANTS), ids=list(_PLANTS))
def test_a_planted_edit_of_the_jobs_fails(plant: str) -> None:
    edit, says = _PLANTS[plant]
    wf = copy.deepcopy(_load())
    edit(wf)
    found = violations(wf)
    assert any(says in v for v in found), found


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("pytest==9.1.1", "pytest==9.2.0"),
        ("pip-audit==2.10.1", "pip-audit==2.11.0"),
        ('version: "0.13.1"', 'version: "0.14.0"'),
        ("--python 3.12.15", "--python 3.12.16"),
        ("timeout-minutes: 20", "timeout-minutes: 25"),
        (
            "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
            "actions/checkout@0000000000000000000000000000000000000000",
        ),
    ],
    ids=["pytest", "pip-audit", "uv", "python", "timeout", "action-ref"],
)
def test_a_version_bump_needs_no_edit_here(old: str, new: str) -> None:
    text = CI.read_text(encoding="utf-8")
    assert old in text, old
    assert violations(yaml.safe_load(text.replace(old, new))) == []


def _with_the_tag_fetch(wf: dict[str, Any]) -> dict[str, Any]:
    _job(wf, "static").insert(2, dict(_FETCH_TAG))
    return wf


def test_the_static_job_may_fetch_the_latest_release_tag() -> None:
    """#166's shape is taken while it lands: the step after the checkout, exactly as pinned."""
    assert violations(_with_the_tag_fetch(copy.deepcopy(_load()))) == []


@pytest.mark.parametrize(
    "edit",
    [
        lambda step: step.update(run=step["run"] + " || true"),
        lambda step: step.update(run=step["run"].replace("--depth 1 ", "")),
        lambda step: step.update({"if": "false"}),
        lambda step: step.update({"continue-on-error": True}),
    ],
    ids=["forgiven", "whole-history", "skippable", "failure-forgiven"],
)
def test_the_tag_fetch_is_held_to_its_pinned_form(edit) -> None:
    wf = _with_the_tag_fetch(copy.deepcopy(_load()))
    edit(_named(wf, "static", "Fetch the latest release tag"))
    assert violations(wf), "an edited tag fetch was taken"
