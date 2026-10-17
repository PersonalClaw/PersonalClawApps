"""Every job in every workflow carries a ``timeout-minutes`` cap.

A job with no cap can hold a runner for six hours: a wedged test worker or a hung network
call keeps it until GitHub's own limit ends it. With a cap, the job fails visibly at about
twice its normal time and the runner is freed.

The rule: every job under ``.github/workflows/`` declares ``timeout-minutes`` as a positive
whole number of minutes, or as an expression (``${{ ... }}``) that the workflow resolves.
A job that calls a reusable workflow (``uses:`` at job level) is exempt, because the jobs
of the workflow it calls carry their own caps.

Each cap is sized at about twice the job's measured p95 from the retained run history,
never below its longest observed run, rounded up to the next 5 minutes with a floor of 5.
The commit that set the caps keeps the measured table.

The planted workflows below show the rule fails on a job without a cap and on a cap that
is not a positive number, and passes on a capped job and on a reusable-workflow call.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


def missing_timeouts(name: str, text: str) -> list[str]:
    """Return one problem line for each job in one workflow that has no usable cap."""
    workflow = yaml.safe_load(text)
    if not isinstance(workflow, dict) or not isinstance(workflow.get("jobs"), dict):
        return [f"{name}: no jobs found"]
    problems = []
    for job_id, job in workflow["jobs"].items():
        where = f"{name} {job_id}"
        if not isinstance(job, dict):
            problems.append(f"{where}: a job must be a mapping")
            continue
        if "uses" in job:
            continue
        if "timeout-minutes" not in job:
            problems.append(f"{where}: no timeout-minutes")
            continue
        cap = job["timeout-minutes"]
        if isinstance(cap, str) and cap.strip().startswith("${{") and cap.strip().endswith("}}"):
            continue
        if isinstance(cap, bool) or not isinstance(cap, int) or cap <= 0:
            problems.append(f"{where}: timeout-minutes must be a positive whole number, "
                            f"not {cap!r}")
    return problems


def _workflow_files() -> list[Path]:
    return sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])


def test_the_repository_has_workflows_to_check():
    assert _workflow_files(), f"no workflow found under {WORKFLOWS}"


def test_every_job_has_a_timeout():
    problems = [line for path in _workflow_files()
                for line in missing_timeouts(path.name, path.read_text(encoding="utf-8"))]
    assert not problems, "jobs without a usable timeout-minutes:\n" + "\n".join(problems)


# -- planted workflows ------------------------------------------------------------------------

# Each planted workflow is the capped baseline plus one job, so a test fails only when the rule
# that job exercises is broken.
PLANTED = """\
on: pull_request
permissions: {}
jobs:
  capped:
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    steps:
      - run: echo capped
%s"""

UNCAPPED_JOB = """\
  uncapped:
    runs-on: ubuntu-24.04
    steps:
      - run: echo no cap
"""

REUSABLE_CALL = """\
  reusable:
    uses: ./.github/workflows/called.yml
"""

EXPRESSION_CAP = """\
  from-matrix:
    runs-on: ubuntu-24.04
    timeout-minutes: ${{ matrix.timeout }}
    steps:
      - run: echo from a matrix
"""


def test_the_planted_baseline_passes():
    assert missing_timeouts("planted.yml", PLANTED % "") == []


def test_a_planted_job_without_a_timeout_fails():
    assert missing_timeouts("planted.yml", PLANTED % UNCAPPED_JOB) == [
        "planted.yml uncapped: no timeout-minutes"]


@pytest.mark.parametrize("cap", ["0", "-5", "'ten'", "true", "2.5"])
def test_a_planted_cap_that_is_not_a_positive_whole_number_fails(cap):
    job = f"  bad-cap:\n    runs-on: ubuntu-24.04\n    timeout-minutes: {cap}\n" \
          "    steps:\n      - run: echo bad cap\n"
    problems = missing_timeouts("planted.yml", PLANTED % job)
    assert len(problems) == 1 and problems[0].startswith("planted.yml bad-cap: timeout-minutes"), \
        problems


def test_a_planted_reusable_workflow_call_is_exempt():
    assert missing_timeouts("planted.yml", PLANTED % REUSABLE_CALL) == []


def test_a_planted_cap_from_an_expression_is_accepted():
    assert missing_timeouts("planted.yml", PLANTED % EXPRESSION_CAP) == []
