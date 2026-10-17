"""``core.lock`` pins the core every apps CI run tests, and the two scheduled workflows beside it
keep that pin honest without ever writing it.

- ``core.lock`` is one line: a 40-character lowercase hex core commit, then a newline.
- ``scripts/core_lock.py`` is its one reader: ``read``, ``export`` (CI's ``CORE_SHA`` and
  ``CORE_SPEC``), ``resolve`` (the canary's core), ``verify`` (the installed core is the locked one)
  and ``bump`` (the daily comparison with core ``main``, report-only until the pin-refresh App
  exists).
- ``ci.yml``: every job that installs core reads ``core.lock`` first, installs ``"$CORE_SPEC"``
  and then checks the installed core against it; nothing names ``@main``.
- ``core-main-canary.yml``: weekly and on dispatch only, so it never stands for a pull request's
  verdict; only its ``report`` job writes, and only issues, to one issue found by one exact title.
- ``core-lock-bump.yml``: daily and on dispatch, no write permission, nothing that pushes or opens
  a pull request.

Each workflow rule runs over the real file and over a planted violation of it, so a rule that
matched nothing could not pass.
"""

from __future__ import annotations

import copy
import importlib.metadata
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "core_lock.py"
WORKFLOWS = ROOT / ".github" / "workflows"
CORE_URL = "https://github.com/PersonalClaw/PersonalClaw"
TITLE = "Apps fail against core main"
SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER = "fedcba9876543210fedcba9876543210fedcba98"


def _module():
    spec = importlib.util.spec_from_file_location("_core_lock_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _cli(*args: str, env: dict[str, str] | None = None, python: str = sys.executable):
    import os

    full = {k: v for k, v in os.environ.items() if k not in ("GITHUB_ENV", "GITHUB_OUTPUT",
                                                          "GITHUB_STEP_SUMMARY")}
    full.update(env or {})
    return subprocess.run([python, str(SCRIPT), *args], cwd=ROOT, capture_output=True, text=True,
                          env=full)


def _lock(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "core.lock"
    path.write_text(text, encoding="utf-8", newline="")
    return path


def _core_repo(tmp_path: Path) -> tuple[Path, str]:
    """A local repository standing in for core, with one commit on ``main``."""
    repo = tmp_path / "core"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True,  # noqa: E731
                                    text=True).stdout.strip()
    run("init", "-q", "-b", "main")
    (repo / "README").write_text("core\n", encoding="utf-8")
    run("add", "-A")
    run("-c", "user.name=Core Test", "-c", "user.email=core@example.com", "commit", "-q", "-m", "c")
    return repo, run("rev-parse", "HEAD")


def _installed_core_commit() -> str | None:
    try:
        dist = importlib.metadata.distribution("personalclaw")
    except importlib.metadata.PackageNotFoundError:
        return None
    direct = json.loads(dist.read_text("direct_url.json") or "{}")
    return (direct.get("vcs_info") or {}).get("commit_id")


# -- core.lock's format ---------------------------------------------------------------------------

def test_the_checked_in_core_lock_is_one_full_commit():
    text = (ROOT / "core.lock").read_bytes().decode("ascii")
    assert re.fullmatch(r"[0-9a-f]{40}\n", text), repr(text)
    assert _module().parse(text) == text.strip()


@pytest.mark.parametrize("text, why", [
    ("main\n", "a branch name"),
    ("v0.2.1\n", "a tag"),
    (SHA[:7] + "\n", "a short SHA"),
    (SHA.upper() + "\n", "uppercase hex"),
    (SHA, "no trailing newline"),
    (SHA + "\n" + OTHER + "\n", "two lines"),
    (" " + SHA + "\n", "a leading space"),
    (SHA + " \n", "a trailing space"),
    (SHA + "\r\n", "a CRLF line end"),
    ("", "an empty file"),
])
def test_a_planted_bad_lock_is_refused(text, why):
    with pytest.raises(ValueError):
        _module().parse(text)


def test_read_prints_the_locked_commit_and_refuses_a_bad_file(tmp_path):
    good = _cli("read", "--file", str(_lock(tmp_path, SHA + "\n")))
    assert (good.returncode, good.stdout) == (0, SHA + "\n")
    bad = _cli("read", "--file", str(_lock(tmp_path, "main\n")))
    assert bad.returncode == 1 and "core.lock" in bad.stderr
    missing = _cli("read", "--file", str(tmp_path / "absent.lock"))
    assert missing.returncode == 1 and "absent.lock" in missing.stderr


# -- the reader's commands ------------------------------------------------------------------------

def test_export_hands_ci_the_locked_commit_and_prints_it(tmp_path):
    env_file, out_file = tmp_path / "env", tmp_path / "out"
    run = _cli("export", "--file", str(_lock(tmp_path, SHA + "\n")),
               env={"GITHUB_ENV": str(env_file), "GITHUB_OUTPUT": str(out_file)})
    assert run.returncode == 0, run.stderr
    assert SHA in run.stdout
    assert env_file.read_text().splitlines() == [
        f"CORE_SHA={SHA}", f"CORE_SPEC=personalclaw @ git+{CORE_URL}@{SHA}",
    ]
    assert out_file.read_text().splitlines() == [f"sha={SHA}"]


def test_resolve_turns_a_branch_into_one_commit_and_refuses_what_it_cannot_resolve(tmp_path):
    repo, head = _core_repo(tmp_path)
    branch = _cli("resolve", "main", "--repo", str(repo))
    assert (branch.returncode, branch.stdout) == (0, head + "\n")
    full = _cli("resolve", OTHER, "--repo", str(repo))
    assert (full.returncode, full.stdout) == (0, OTHER + "\n")
    for ref in ("no-such-branch", head[:7]):
        refused = _cli("resolve", ref, "--repo", str(repo))
        assert refused.returncode == 1, ref
        assert ref in refused.stderr


def test_resolve_exports_what_it_resolved(tmp_path):
    repo, head = _core_repo(tmp_path)
    env_file = tmp_path / "env"
    run = _cli("resolve", "main", "--repo", str(repo), "--export",
               env={"GITHUB_ENV": str(env_file)})
    assert run.returncode == 0, run.stderr
    assert f"CORE_SHA={head}" in env_file.read_text().splitlines()


def test_verify_refuses_an_installed_core_other_than_the_locked_one(tmp_path):
    run = _cli("verify", "--file", str(_lock(tmp_path, "0" * 40 + "\n")))
    assert run.returncode == 1
    assert "0" * 40 in run.stderr


def test_verify_accepts_the_installed_core_when_it_is_the_locked_one(tmp_path):
    commit = _installed_core_commit()
    if not commit:
        pytest.skip("the installed core does not record the commit it was built from")
    run = _cli("verify", "--file", str(_lock(tmp_path, commit + "\n")))
    assert run.returncode == 0, run.stderr
    assert commit in run.stdout


def _bump(tmp_path: Path, *args: str):
    summary = tmp_path / "summary.md"
    lock = _lock(tmp_path, SHA + "\n")
    run = _cli("bump", "--file", str(lock), *args, env={"GITHUB_STEP_SUMMARY": str(summary)})
    assert lock.read_text() == SHA + "\n", "the bump wrote core.lock"
    return run, summary.read_text() if summary.exists() else ""


def test_bump_without_the_app_reports_both_commits_and_opens_nothing(tmp_path):
    run, summary = _bump(tmp_path, "--core-main", OTHER, "--app-credentials", "false")
    assert run.returncode == 0, run.stderr
    assert SHA in summary and OTHER in summary
    assert "behind" in summary
    assert "opened no pull request" in summary and "pushed nothing" in summary
    assert "::error::" not in run.stdout


def test_bump_says_current_when_core_main_is_the_pin(tmp_path):
    run, summary = _bump(tmp_path, "--core-main", SHA, "--app-credentials", "false")
    assert run.returncode == 0, run.stderr
    assert "current" in summary and "behind" not in summary


def test_bump_with_the_app_configured_fails_loudly_until_the_pull_request_half_exists(tmp_path):
    run, summary = _bump(tmp_path, "--core-main", OTHER, "--app-credentials", "true")
    assert run.returncode == 1
    assert "::error::" in run.stdout
    assert SHA in summary and OTHER in summary


def test_bump_reads_core_main_itself_when_not_told(tmp_path):
    repo, head = _core_repo(tmp_path)
    run, summary = _bump(tmp_path, "--repo", str(repo), "--app-credentials", "false")
    assert run.returncode == 0, run.stderr
    assert head in summary


# -- the workflows --------------------------------------------------------------------------------

def _load(name: str) -> tuple[dict, str]:
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    return yaml.safe_load(text), text


def _on(wf: dict) -> dict:
    on = wf.get("on", wf.get(True))  # YAML 1.1 reads a bare `on` key as true
    return on if isinstance(on, dict) else {k: None for k in (on if isinstance(on, list) else [on])}


def _cron_fields(wf: dict) -> list[list[str]]:
    return [str(entry.get("cron", "")).split() for entry in (_on(wf).get("schedule") or [])]


def _daily(fields: list[str]) -> bool:
    return len(fields) == 5 and fields[0].isdigit() and fields[1].isdigit() and fields[2:] == ["*"] * 3


def _weekly(fields: list[str]) -> bool:
    return (len(fields) == 5 and fields[0].isdigit() and fields[1].isdigit()
            and fields[2:4] == ["*", "*"] and fields[4].isdigit())


def _steps(job: dict) -> list[dict]:
    return job.get("steps") or []


def _run(step: dict) -> str:
    return step.get("run") or ""


def _writes(permissions) -> set[str]:
    if isinstance(permissions, str):
        return {"*"} if permissions != "read-all" else set()
    return {k for k, v in (permissions or {}).items() if v == "write"}


def _checkout_problems(name: str, job: dict) -> list[str]:
    return [
        f"{name}: a checkout keeps its credentials (set persist-credentials: false)"
        for step in _steps(job)
        if str(step.get("uses", "")).startswith("actions/checkout@")
        and (step.get("with") or {}).get("persist-credentials") is not False
    ]


_PIP_INSTALL = re.compile(r"pip install\b(?P<rest>.*)")


def _core_installs(run: str) -> list[str]:
    lines = []
    for line in run.splitlines():
        found = _PIP_INSTALL.search(line)
        if found and ("CORE_SPEC" in found["rest"]
                      or re.search(r"\bpersonalclaw\b(?![._-])", found["rest"])):
            lines.append(line.strip())
    return lines


def ci_problems(wf: dict, text: str) -> tuple[list[str], list[str]]:
    """What ``ci.yml`` breaks of the core.lock rules, and which jobs install core."""
    problems: list[str] = []
    if "@main" in text:
        problems.append("ci.yml names @main")
    if "CORE_SPEC" in (wf.get("env") or {}):
        problems.append("a workflow-level CORE_SPEC: core comes from core.lock, per job")
    if wf.get("permissions") != {"contents": "read"}:
        problems.append(f"the workflow's permissions are {wf.get('permissions')!r}, not contents: read")
    installing: list[str] = []
    for name, job in (wf.get("jobs") or {}).items():
        if _writes(job.get("permissions")):
            problems.append(f"{name}: holds a write permission")
        steps = _steps(job)
        export_at = next((i for i, s in enumerate(steps)
                          if "scripts/core_lock.py export" in _run(s)), None)
        install_at = []
        for i, step in enumerate(steps):
            lines = _core_installs(_run(step))
            for line in lines:
                if '"$CORE_SPEC"' not in line:
                    problems.append(f"{name}: installs core from a literal spec: {line}")
            if lines:
                install_at.append(i)
        for step in steps:
            if "scripts/gate.py" in _run(step) and '--core "$CORE_SHA"' not in _run(step):
                problems.append(f'{name}: runs the gate without --core "$CORE_SHA"')
        if not install_at:
            continue
        installing.append(name)
        if export_at is None or export_at > min(install_at):
            problems.append(f"{name}: installs core without first reading core.lock")
        after = [_run(s) for s in steps[max(install_at) + 1:]]
        if not any("scripts/core_lock.py verify" in r
                   or ("scripts/gate.py" in r and '--core "$CORE_SHA"' in r) for r in after):
            problems.append(f"{name}: never checks that the installed core is the locked one")
    return problems, installing


def canary_problems(wf: dict) -> list[str]:
    problems: list[str] = []
    on = _on(wf)
    if set(on) != {"schedule", "workflow_dispatch"}:
        problems.append(f"the canary runs on {sorted(map(str, on))}: only schedule and "
                        "workflow_dispatch, so it never stands for a pull request's verdict")
    crons = _cron_fields(wf)
    if len(crons) != 1 or not _weekly(crons[0]):
        problems.append(f"the canary's schedule {crons} is not one weekly cron")
    if wf.get("permissions") != {"contents": "read"}:
        problems.append(f"the canary's workflow permissions are {wf.get('permissions')!r}")
    jobs = wf.get("jobs") or {}
    for name, job in jobs.items():
        if job.get("permissions") is None:
            problems.append(f"{name}: declares no permissions of its own")
        if name == "report":
            if job.get("permissions") != {"issues": "write"}:
                problems.append(f"report: holds {job.get('permissions')!r}, not issues: write alone")
        elif _writes(job.get("permissions")):
            problems.append(f"{name}: holds a write permission; only the report job writes")
        problems += _checkout_problems(name, job)
    check = "\n".join(_run(s) for s in _steps(jobs.get("check") or {}))
    if "scripts/core_lock.py resolve" not in check:
        problems.append("check: does not resolve the core it tests to one commit")
    if not re.search(r'scripts/gate\.py static rails bundles --core "\$CORE_SHA"', check):
        problems.append("check: does not run every gate part against the resolved core")
    if "python -m personalclaw.apps.quality ." not in check:
        problems.append("check: does not verify the quality declarations")
    report = jobs.get("report") or {}
    if report.get("needs") != "check":
        problems.append("report: does not follow the check job")
    if (report.get("env") or {}).get("TITLE") != TITLE:
        problems.append(f"report: its TITLE is not {TITLE!r}")
    issue_steps = [s for s in _steps(report) if "gh issue" in _run(s)]
    for step in issue_steps:
        if "TITLE" in (step.get("env") or {}):
            problems.append(f"report: step {step.get('name')!r} sets a title of its own")
        if "gh issue create" in _run(step) and '--title "$TITLE"' not in _run(step):
            problems.append("report: opens its issue under another title")
    text = "\n".join(_run(s) for s in issue_steps)
    lists = [s for s in issue_steps if "gh issue list" in _run(s)]
    if not lists:
        problems.append("report: never lists its issue")
    for step in lists:
        if "select(.title == env.TITLE)" not in _run(step):
            problems.append(f"report: step {step.get('name')!r} does not find its issue by the "
                            "exact title")
    for verb in ("gh issue create", "gh issue comment", "gh issue close"):
        if verb not in text:
            problems.append(f"report: never runs {verb}")
    return problems


_NO_WRITES = re.compile(r"\bgit\s+(?:push|commit)\b|\bgh\s+(?:pr|api|issue)\b")


def bump_problems(wf: dict, text: str) -> list[str]:
    problems: list[str] = []
    on = _on(wf)
    if set(on) != {"schedule", "workflow_dispatch"}:
        problems.append(f"the bump runs on {sorted(map(str, on))}, not schedule and dispatch")
    crons = _cron_fields(wf)
    if len(crons) != 1 or not _daily(crons[0]):
        problems.append(f"the bump's schedule {crons} is not one daily cron")
    if wf.get("permissions") != {}:
        problems.append(f"the bump's workflow permissions are {wf.get('permissions')!r}, not {{}}")
    for name, job in (wf.get("jobs") or {}).items():
        if job.get("permissions") is None:
            problems.append(f"{name}: declares no permissions of its own")
        if _writes(job.get("permissions")):
            problems.append(f"{name}: holds a write permission")
        problems += _checkout_problems(name, job)
        for step in _steps(job):
            if _NO_WRITES.search(_run(step)):
                problems.append(f"{name}: step {step.get('name')!r} writes to the repository")
    steps = [s for job in (wf.get("jobs") or {}).values() for s in _steps(job)]
    bump = [s for s in steps if "scripts/core_lock.py bump" in _run(s)]
    if len(bump) != 1 or '--app-credentials "$APP_CREDENTIALS"' not in _run(bump[0]):
        problems.append('no single step runs core_lock.py bump --app-credentials "$APP_CREDENTIALS"')
    else:
        source = str((bump[0].get("env") or {}).get("APP_CREDENTIALS", ""))
        for name in ("vars.PIN_REFRESH_APP_ID", "secrets.PIN_REFRESH_APP_PRIVATE_KEY"):
            if name not in source:
                problems.append(f"APP_CREDENTIALS does not read {name}")
    return problems


def test_ci_installs_core_only_from_core_lock():
    problems, installing = ci_problems(*_load("ci.yml"))
    assert problems == []
    # The vacuity floor: the rules above saw the three jobs that install core.
    assert installing == ["static", "tests", "quality-declarations"]


def test_the_canary_keeps_its_rules():
    assert canary_problems(_load("core-main-canary.yml")[0]) == []


def test_the_bump_keeps_its_rules():
    assert bump_problems(*_load("core-lock-bump.yml")) == []


def _job_step(wf: dict, job: str, needle: str) -> tuple[list[dict], int]:
    steps = wf["jobs"][job]["steps"]
    return steps, next(i for i, s in enumerate(steps) if needle in _run(s))


def _drop(wf, job, needle):
    steps, i = _job_step(wf, job, needle)
    del steps[i]


def _move_export_after_install(wf):
    steps, i = _job_step(wf, "static", "scripts/core_lock.py export")
    step = steps.pop(i)
    _, j = _job_step(wf, "static", '"$CORE_SPEC"')
    steps.insert(j + 1, step)


def _edit(wf, job, needle, old, new):
    steps, i = _job_step(wf, job, needle)
    assert old in steps[i]["run"]
    steps[i]["run"] = steps[i]["run"].replace(old, new)


CI_PLANTS = {
    "a job installs core without the read step": (
        lambda wf, t: (_drop(wf, "tests", "scripts/core_lock.py export"), t)[1],
        "tests: installs core without first reading core.lock"),
    "the read step comes after the install": (
        lambda wf, t: (_move_export_after_install(wf), t)[1],
        "static: installs core without first reading core.lock"),
    "@main is named again": (lambda wf, t: t + "\n# personalclaw @ git+x@main\n", "names @main"),
    "a workflow-level CORE_SPEC is back": (
        lambda wf, t: (wf["env"].__setitem__("CORE_SPEC", "personalclaw"), t)[1],
        "workflow-level CORE_SPEC"),
    "core is installed from a literal spec": (
        lambda wf, t: (_edit(wf, "quality-declarations", '"$CORE_SPEC"', '"$CORE_SPEC"',
                             f'"personalclaw @ git+{CORE_URL}@{SHA}"'), t)[1],
        "installs core from a literal spec"),
    "a new job installs core without the step": (
        lambda wf, t: (wf["jobs"].__setitem__("extra", {"steps": [
            {"run": 'uv pip install "$CORE_SPEC"'}, {"run": "python scripts/core_lock.py verify"},
        ]}), t)[1],
        "extra: installs core without first reading core.lock"),
    "the gate runs without --core": (
        lambda wf, t: (_edit(wf, "static", "scripts/gate.py", ' --core "$CORE_SHA"', ""), t)[1],
        'static: runs the gate without --core "$CORE_SHA"'),
    "the quality job never verifies its core": (
        lambda wf, t: (_drop(wf, "quality-declarations", "scripts/core_lock.py verify"), t)[1],
        "quality-declarations: never checks"),
    "a write permission at the top": (
        lambda wf, t: (wf.__setitem__("permissions", {"contents": "write"}), t)[1],
        "not contents: read"),
    "a job holds a write permission": (
        lambda wf, t: (wf["jobs"]["tests"].__setitem__("permissions", {"issues": "write"}), t)[1],
        "tests: holds a write permission"),
}


@pytest.mark.parametrize("plant", sorted(CI_PLANTS))
def test_a_planted_ci_violation_is_caught(plant):
    wf, text = _load("ci.yml")
    wf = copy.deepcopy(wf)
    mutate, expected = CI_PLANTS[plant]
    text = mutate(wf, text)
    problems, _ = ci_problems(wf, text)
    assert any(expected in p for p in problems), problems


def _set_step_env(wf, job, needle, key, value):
    steps, i = _job_step(wf, job, needle)
    steps[i].setdefault("env", {})[key] = value


def _checkout(wf, job):
    return next(s for s in wf["jobs"][job]["steps"]
                if str(s.get("uses", "")).startswith("actions/checkout@"))


CANARY_PLANTS = {
    "a pull_request trigger": (lambda wf: _on(wf).__setitem__("pull_request", None),
                               "never stands for a pull request's verdict"),
    "issues: write at the top": (
        lambda wf: wf.__setitem__("permissions", {"contents": "read", "issues": "write"}),
        "workflow permissions"),
    "issues: write on the check job": (
        lambda wf: wf["jobs"]["check"].__setitem__("permissions", {"contents": "read",
                                                                   "issues": "write"}),
        "check: holds a write permission"),
    "a second title": (
        lambda wf: _set_step_env(wf, "report", "gh issue create", "TITLE", "Core main canary"),
        "sets a title of its own"),
    "the gate without the resolved core": (
        lambda wf: _edit(wf, "check", "scripts/gate.py", ' --core "$CORE_SHA"', ""),
        "every gate part against the resolved core"),
    "no quality declarations": (
        lambda wf: _drop(wf, "check", "python -m personalclaw.apps.quality"),
        "quality declarations"),
    "a daily schedule": (
        lambda wf: _on(wf).__setitem__("schedule", [{"cron": "23 6 * * *"}]),
        "not one weekly cron"),
    "a checkout that keeps its credentials": (
        lambda wf: _checkout(wf, "check").pop("with", None), "keeps its credentials"),
    "a substring title match": (
        lambda wf: _edit(wf, "report", "select(.title == env.TITLE)",
                         "[.[] | select(.title == env.TITLE)][0].number", ".[0].number"),
        "exact title"),
}


@pytest.mark.parametrize("plant", sorted(CANARY_PLANTS))
def test_a_planted_canary_violation_is_caught(plant):
    wf = copy.deepcopy(_load("core-main-canary.yml")[0])
    mutate, expected = CANARY_PLANTS[plant]
    mutate(wf)
    problems = canary_problems(wf)
    assert any(expected in p for p in problems), problems


def _add_step(wf, run):
    wf["jobs"]["bump"]["steps"].append({"name": "planted", "run": run})


BUMP_PLANTS = {
    "no schedule": (lambda wf, t: (_on(wf).pop("schedule"), t)[1], "not schedule and dispatch"),
    "a weekly schedule": (
        lambda wf, t: (_on(wf).__setitem__("schedule", [{"cron": "41 5 * * 1"}]), t)[1],
        "not one daily cron"),
    "contents: write on the job": (
        lambda wf, t: (wf["jobs"]["bump"].__setitem__("permissions", {"contents": "write"}), t)[1],
        "bump: holds a write permission"),
    "pull-requests: write on the job": (
        lambda wf, t: (wf["jobs"]["bump"]["permissions"].__setitem__("pull-requests", "write"),
                       t)[1],
        "bump: holds a write permission"),
    "a permission at the top": (
        lambda wf, t: (wf.__setitem__("permissions", {"contents": "read"}), t)[1],
        "workflow permissions"),
    "a git push": (lambda wf, t: (_add_step(wf, "git push origin HEAD:core-lock-bump"), t)[1],
                   "writes to the repository"),
    "a gh pr create": (lambda wf, t: (_add_step(wf, "gh pr create --fill"), t)[1],
                       "writes to the repository"),
    "a checkout that keeps its credentials": (
        lambda wf, t: (_checkout(wf, "bump").pop("with", None), t)[1], "keeps its credentials"),
    "the bump without the credentials flag": (
        lambda wf, t: (_edit(wf, "bump", "scripts/core_lock.py bump",
                             ' --app-credentials "$APP_CREDENTIALS"', ""), t)[1],
        "no single step runs core_lock.py bump"),
}


@pytest.mark.parametrize("plant", sorted(BUMP_PLANTS))
def test_a_planted_bump_violation_is_caught(plant):
    wf, text = _load("core-lock-bump.yml")
    wf = copy.deepcopy(wf)
    mutate, expected = BUMP_PLANTS[plant]
    text = mutate(wf, text)
    problems = bump_problems(wf, text)
    assert any(expected in p for p in problems), problems
