"""``scripts/audit_locks.py``: the dependency audit of the locks the apps ship.

The command is driven end to end through ``audit_locks.main`` over a planted tree (both
platforms' bundle-lock projects and a UI bundle's npm lock), with a fake runner standing in for
``uv export``, ``pip-audit`` and ``npm audit``, so each test pins the wiring, not a helper. A few
read the committed tree: the graphs audited are the ones the lock's project declares, the export
is the bundle runner's own, the real export's format is one the audit reads, and both workflows
run the command unmasked.
"""

from __future__ import annotations

import datetime
import json
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml

import planted_envs as planted

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
TODAY = datetime.date(2026, 10, 18)
LINUX_LOCK = ".github/bundle-lock/linux-x86_64/uv.lock"
NPM_LOCK = "example-app/ui/package-lock.json"


def _module(root: Path = ROOT):
    return planted.load(root / "scripts" / "audit_locks.py", f"_audit_locks_{abs(hash(root))}")


class _Lazy:
    """The committed script, loaded on first use, so a tree without it fails each test on its
    own rather than the whole file at collection."""

    def __getattr__(self, name):
        return getattr(_module(), name)


audit = _Lazy()

#: The waiver fields, as the script names them (``test_the_waiver_fields_are_cores``).
FIELDS = ("file", "scope", "reference", "owner", "reason", "expires")

LINUX = "platform_machine == 'x86_64' and sys_platform == 'linux'"
DARWIN = "platform_machine == 'arm64' and sys_platform == 'darwin'"

#: Each environment's export, as ``uv export`` prints it (two lines per entry, hashed).
EXPORTS = {
    None: ["idna==3.6", "pytest==9.1.1"],
    "alpha-app": ["idna==3.6", "pytest==9.1.1", "requests==2.31.0"],
    "beta-app": ["idna==3.6", "pytest==9.1.1", "pillow==10.0.0"],
}

CLEAN_NPM = {
    "vulnerabilities": {},
    "metadata": {"vulnerabilities": {"moderate": 0, "high": 0, "critical": 0, "total": 0}},
}


def _npm(*advisories: tuple[str, str, str]) -> dict:
    """An ``npm audit --json`` report: each advisory as (package, GHSA id, severity), plus one
    package that only depends on the first, the way npm lists it."""
    vulns: dict = {}
    for package, ghsa, severity in advisories:
        vulns[package] = {"severity": severity, "via": [{
            "source": 1, "name": package, "url": f"https://github.com/advisories/{ghsa}",
            "severity": severity, "range": "<=1.0.0"}]}
    if advisories:
        vulns["a-dependent"] = {"severity": advisories[0][2], "via": [advisories[0][0]]}
    return {"vulnerabilities": vulns, "metadata": {"vulnerabilities": {"total": len(vulns)}}}


def _project(marker: str) -> str:
    return (
        "[project]\nname = \"apps-bundle-lock\"\nversion = \"0\"\n"
        "requires-python = \"==3.12.15\"\ndependencies = [\"pytest==9.1.1\"]\n\n"
        "[project.optional-dependencies]\nalpha-app = [\"requests>=2\"]\n"
        "beta-app = [\"pillow>=10\"]\n\n"
        f"[tool.uv]\npackage = false\nenvironments = [\"{marker}\"]\n\n"
        "[[tool.uv.index]]\nurl = \"https://pypi.org/simple\"\ndefault = true\n"
    )


def _export_text(extra: str | None, marker: str, more: Sequence[str] = ()) -> str:
    lines = []
    for pin in [*EXPORTS[extra], *more]:
        lines += [f"{pin} ; {marker} \\", f"    --hash=sha256:{'0' * 64}"]
    return "\n".join(lines) + "\n"


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A tree with this repository's scripts, both platforms' lock projects, two bundles that
    declare dependencies and one UI bundle with an npm lock."""
    root = tmp_path / "tree"
    (root / "scripts").mkdir(parents=True)
    for name in ("audit_locks.py", "bundle_lock.py", "test_bundles.py", "core_lock.py"):
        shutil.copy2(ROOT / "scripts" / name, root / "scripts" / name)
    for platform, marker in (("linux-x86_64", LINUX), ("darwin-arm64", DARWIN)):
        lock = root / ".github" / "bundle-lock" / platform
        lock.mkdir(parents=True)
        (lock / "pyproject.toml").write_text(_project(marker), encoding="utf-8")
        (lock / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    for name in ("alpha-app", "beta-app", "example-app"):
        (root / name).mkdir()
        (root / name / "app.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    (root / "example-app" / "ui" / "node_modules" / "dep").mkdir(parents=True)
    (root / "example-app" / "ui" / "package-lock.json").write_text("{}", encoding="utf-8")
    # A lock npm installed inside node_modules is not one the bundle ships.
    (root / "example-app" / "ui" / "node_modules" / "dep" / "package-lock.json").write_text(
        "{}", encoding="utf-8")
    return root


class FakeTools:
    """``uv export``, ``pip-audit`` and ``npm audit`` as the audit calls them."""

    def __init__(self, *, vulns: dict | None = None, pip: str | dict | None = None,
                 pip_exit: int | None = None, write_report: bool = True,
                 npm: dict | str | None = None, npm_exit: int | None = None,
                 export_exit: int = 0, more: Sequence[str] = (), drop: Sequence[str] = ()):
        self.vulns = vulns or {}  # {(name, version): [vuln, ...]}
        self.pip, self.pip_exit, self.write_report = pip, pip_exit, write_report
        self.npm = CLEAN_NPM if npm is None else npm
        self.npm_exit, self.export_exit = npm_exit, export_exit
        self.more, self.drop = list(more), set(drop)
        self.calls: list[list[str]] = []
        self.audited: list[dict[str, str]] = []

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        self.calls.append(argv)
        if argv[:2] == ["uv", "export"]:
            extra = argv[argv.index("--extra") + 1] if "--extra" in argv else None
            project = Path(argv[argv.index("--project") + 1]).name
            marker = LINUX if project == "linux-x86_64" else DARWIN
            text = _export_text(extra, marker, self.more) if self.export_exit == 0 else ""
            return subprocess.CompletedProcess(argv, self.export_exit, text, "lock is stale")
        if "pip_audit" in argv:
            req = Path(argv[argv.index("-r") + 1])
            given = dict(line.split("==") for line in req.read_text().split())
            self.audited.append(given)
            report = Path(argv[argv.index("-o") + 1])
            deps = [{"name": n, "version": v, "vulns": self.vulns.get((n, v), [])}
                    for n, v in sorted(given.items()) if n not in self.drop]
            if self.write_report:
                text = self.pip if isinstance(self.pip, str) else json.dumps(
                    self.pip if self.pip is not None else {"dependencies": deps, "fixes": []})
                report.write_text(text, encoding="utf-8")
            found = any(d["vulns"] for d in deps)
            code = self.pip_exit if self.pip_exit is not None else int(found)
            return subprocess.CompletedProcess(argv, code, "", "pip-audit crashed")
        if argv[:2] == ["npm", "audit"]:
            text = self.npm if isinstance(self.npm, str) else json.dumps(self.npm)
            found = isinstance(self.npm, dict) and bool(self.npm.get("vulnerabilities"))
            code = self.npm_exit if self.npm_exit is not None else int(found)
            return subprocess.CompletedProcess(argv, code, text, "")
        raise AssertionError(f"the audit ran {argv}")


IDNA = {("idna", "3.6"): [{"id": "PYSEC-2024-60", "fix_versions": ["3.7"],
                           "aliases": ["CVE-2024-3651", "GHSA-jjg7-2v4v-x38h"]}]}


def _waiver(reference: str, /, *, file: str = NPM_LOCK, scope: str = "npm-audit",
            days: int = 10, drop: Sequence[str] = ()) -> str:
    fields = {"file": file, "scope": scope, "reference": reference, "owner": "maintainers",
              "reason": "A recorded reason."}
    lines = ["[[waiver]]", *(f'{k} = "{v}"' for k, v in fields.items() if k not in drop)]
    if "expires" not in drop:
        lines.append(f"expires = {(TODAY + datetime.timedelta(days=days)).isoformat()}")
    return "\n".join(lines) + "\n"


def _run(tree: Path, tools: FakeTools, baseline: str = "") -> int:
    path = tree / "audit-baseline.toml"
    path.write_text(baseline, encoding="utf-8")
    module = _module(tree)
    argv = ["run", "--pip-audit", "py", "--work", str(tree / "work"), "--baseline", str(path)]
    return module.main(argv, run=tools, today=TODAY, root=tree)


@pytest.fixture(autouse=True)
def _summary(monkeypatch, tmp_path: Path) -> Path:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    return summary


# ── what is audited ───────────────────────────────────────────────────────────────────────────


def test_every_bundle_environment_of_both_platforms_is_exported_from_the_lock_and_audited(
        tree: Path) -> None:
    tools = FakeTools()
    assert _run(tree, tools) == 0
    exports = [c for c in tools.calls if c[:2] == ["uv", "export"]]
    seen = sorted((Path(c[c.index("--project") + 1]).name,
                   c[c.index("--extra") + 1] if "--extra" in c else "") for c in exports)
    assert seen == sorted((p, e) for p in ("darwin-arm64", "linux-x86_64")
                          for e in ("", "alpha-app", "beta-app")), seen
    # The lock's own pins, as the runner installs them: never a fresh resolution.
    assert all("--frozen" in c and "--no-emit-package" in c for c in exports), exports
    audits = [c for c in tools.calls if "pip_audit" in c]
    assert len(audits) == 2, "one pip-audit per lock, each package once"
    assert all("--no-deps" in c and "--disable-pip" in c for c in audits), audits
    assert tools.audited == [{"idna": "3.6", "pytest": "9.1.1", "requests": "2.31.0",
                              "pillow": "10.0.0"}] * 2
    npm = [c for c in tools.calls if c[:2] == ["npm", "audit"]]
    assert npm == [["npm", "audit", "--package-lock-only", "--json", "--prefix",
                    str(tree / "example-app" / "ui")]], "only the lock the bundle ships"


def test_the_export_is_the_bundle_runners_own(tree: Path, monkeypatch) -> None:
    """The audit exports with ``test_bundles.export_argv``, and the runner's own export runs
    that same argv, so what is audited is what a bundle's environment installs."""
    runner = planted.load(ROOT / "scripts" / "test_bundles.py", "_audit_test_runner")
    ran: list[list[str]] = []

    def fake(argv, **_):
        ran.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(runner.subprocess, "run", fake)
    lock = ROOT / ".github" / "bundle-lock" / "linux-x86_64"
    runner._export(lock, "rapidocr")
    assert ran == [runner.export_argv(lock, "rapidocr")]
    tools = FakeTools()
    _run(tree, tools)
    planted_runner = planted.load(tree / "scripts" / "test_bundles.py", "_audit_test_planted")
    linux = tree / ".github" / "bundle-lock" / "linux-x86_64"
    assert planted_runner.export_argv(linux, "alpha-app") in tools.calls


def test_the_graphs_are_every_extra_the_committed_locks_declare_plus_the_base() -> None:
    bundle_lock = planted.load(ROOT / "scripts" / "bundle_lock.py", "_audit_bundle_lock")
    graphs = audit.python_graphs(ROOT)
    assert set(graphs) == set(bundle_lock.PLATFORMS)
    for platform, extras in graphs.items():
        assert extras == [None, *sorted(bundle_lock.declared(ROOT))], platform


def test_every_npm_lock_a_bundle_ships_is_found() -> None:
    found = [p.relative_to(ROOT).as_posix() for p in audit.npm_locks(ROOT)]
    shipped = sorted(p.parent.relative_to(ROOT).as_posix() + "/package-lock.json"
                     for p in ROOT.glob("*/ui/package.json"))
    assert found == shipped and found, found


def test_the_committed_locks_export_in_a_form_the_audit_reads() -> None:
    """The real ``uv export`` of each platform's base environment, read offline from the lock:
    every entry is a pinned release whose marker holds on the lock's platform."""
    bundle_lock = planted.load(ROOT / "scripts" / "bundle_lock.py", "_audit_bundle_lock_real")
    runner = planted.load(ROOT / "scripts" / "test_bundles.py", "_audit_runner_real")
    for platform in bundle_lock.PLATFORMS:
        lock = ROOT / ".github" / "bundle-lock" / platform
        text = runner._export(lock, None)
        fixed = bundle_lock.lock_environment((lock / "pyproject.toml").read_text("utf-8"))
        held = audit.pins(text, fixed, bundle_lock, platform)
        entries = [ln for ln in text.splitlines() if ln and not ln[0].isspace()]
        assert len(held) == len(entries) and "pytest" in held, (platform, len(held))


def test_an_entry_whose_marker_fails_on_the_locks_platform_is_not_audited() -> None:
    bundle_lock = planted.load(ROOT / "scripts" / "bundle_lock.py", "_audit_bundle_lock_m")
    fixed = bundle_lock.lock_environment(_project(LINUX))
    text = (f"idna==3.6 ; {LINUX} \\\n    --hash=sha256:{'0' * 64}\n"
            "pywin32==306 ; sys_platform == 'win32' \\\n    --hash=sha256:" + "1" * 64 + "\n")
    assert audit.pins(text, fixed, bundle_lock, "linux") == {"idna": "3.6"}


# ── what fails the run ────────────────────────────────────────────────────────────────────────


def test_a_lock_pinned_to_a_vulnerable_release_fails_and_names_every_environment_it_reaches(
        tree: Path, _summary: Path) -> None:
    assert _run(tree, FakeTools(vulns=IDNA)) == 1
    text = _summary.read_text(encoding="utf-8")
    assert "GHSA-jjg7-2v4v-x38h" in text and "**fails the run**" in text
    assert f"| {LINUX_LOCK} | base, alpha-app, beta-app | idna | 3.6 |" in text, text


def test_a_python_finding_in_one_bundle_names_only_that_bundle(tree: Path, _summary) -> None:
    vulns = {("pillow", "10.0.0"): [{"id": "PYSEC-1", "aliases": ["GHSA-aaaa-bbbb-cccc"]}]}
    assert _run(tree, FakeTools(vulns=vulns)) == 1
    assert f"| {LINUX_LOCK} | beta-app | pillow |" in _summary.read_text(encoding="utf-8")


def test_a_new_high_npm_finding_fails_and_a_moderate_is_reported(tree: Path, _summary) -> None:
    moderate = FakeTools(npm=_npm(("left-pad", "GHSA-aaaa-bbbb-cccc", "moderate")))
    assert _run(tree, moderate) == 0
    assert "GHSA-aaaa-bbbb-cccc" in _summary.read_text(encoding="utf-8")
    assert _run(tree, FakeTools(npm=_npm(("left-pad", "GHSA-aaaa-bbbb-cccc", "high")))) == 1


def test_a_critical_npm_finding_fails(tree: Path) -> None:
    assert _run(tree, FakeTools(npm=_npm(("left-pad", "GHSA-aaaa-bbbb-cccc", "critical")))) == 1


def test_a_package_npm_lists_only_as_a_dependent_is_not_counted_again(tree, _summary) -> None:
    _run(tree, FakeTools(npm=_npm(("left-pad", "GHSA-aaaa-bbbb-cccc", "moderate"))))
    assert "a-dependent" not in _summary.read_text(encoding="utf-8")


def _summary_text() -> str:
    import os
    return Path(os.environ["GITHUB_STEP_SUMMARY"]).read_text(encoding="utf-8")


def test_a_live_waiver_lets_a_high_finding_pass_until_it_expires(tree: Path) -> None:
    high = FakeTools(npm=_npm(("left-pad", "GHSA-aaaa-bbbb-cccc", "high")))
    ref = "left-pad GHSA-aaaa-bbbb-cccc"
    assert _run(tree, high, _waiver(ref, days=10)) == 0
    assert _run(tree, high, _waiver(ref, days=0)) == 0, "the expiry day itself is live"
    assert _run(tree, high, _waiver(ref, days=-1)) == 1, "an expired waiver is no waiver"
    assert "expired on 2026-10-17" in _summary_text()


def test_a_waiver_on_a_python_finding_names_its_lock_and_pip_audit(tree: Path) -> None:
    ref = "idna GHSA-jjg7-2v4v-x38h"
    darwin = ".github/bundle-lock/darwin-arm64/uv.lock"
    both = _waiver(ref, file=LINUX_LOCK, scope="pip-audit") + _waiver(
        ref, file=darwin, scope="pip-audit")
    assert _run(tree, FakeTools(vulns=IDNA), both) == 0
    # One platform's waiver leaves the other platform's finding gating.
    assert _run(tree, FakeTools(vulns=IDNA), _waiver(ref, file=LINUX_LOCK,
                                                     scope="pip-audit")) == 1


@pytest.mark.parametrize("field", FIELDS)
def test_a_waiver_missing_a_field_is_refused(tree: Path, field: str) -> None:
    high = FakeTools(npm=_npm(("left-pad", "GHSA-aaaa-bbbb-cccc", "high")))
    assert _run(tree, high, _waiver("left-pad GHSA-aaaa-bbbb-cccc", drop=[field])) == 1
    assert f"missing {field}" in _summary_text()


def test_the_waiver_fields_are_cores() -> None:
    assert audit.FIELDS == FIELDS and audit.MAX_DAYS == 30


def test_a_waiver_more_than_thirty_days_out_is_refused(tree: Path) -> None:
    high = FakeTools(npm=_npm(("left-pad", "GHSA-aaaa-bbbb-cccc", "high")))
    ref = "left-pad GHSA-aaaa-bbbb-cccc"
    assert _run(tree, high, _waiver(ref, days=30)) == 0
    assert _run(tree, high, _waiver(ref, days=31)) == 1
    assert "more than 30 days out" in _summary_text()


def test_a_waiver_with_an_unknown_scope_or_an_undated_expiry_is_refused(tree: Path) -> None:
    high = FakeTools(npm=_npm(("left-pad", "GHSA-aaaa-bbbb-cccc", "high")))
    ref = "left-pad GHSA-aaaa-bbbb-cccc"
    assert _run(tree, high, _waiver(ref, scope="osv")) == 1
    assert "scope must be one of pip-audit, npm-audit" in _summary_text()
    undated = _waiver(ref, drop=["expires"]) + 'expires = "2026-10-20"\n'
    assert _run(tree, high, undated) == 1
    assert "expires must be a date" in _summary_text()


def test_a_waiver_that_matches_no_finding_is_refused_so_the_list_cannot_rot(tree: Path) -> None:
    assert _run(tree, FakeTools(), _waiver("left-pad GHSA-aaaa-bbbb-cccc")) == 1
    assert "matches no finding, so it is stale" in _summary_text()
    # The finding's own lock, not another's: a waiver for another file is stale.
    high = FakeTools(npm=_npm(("left-pad", "GHSA-aaaa-bbbb-cccc", "high")))
    assert _run(tree, high, _waiver("left-pad GHSA-aaaa-bbbb-cccc",
                                    file="other-app/ui/package-lock.json")) == 1


# ── a tool that did not run is never a clean audit ────────────────────────────────────────────


# Each case names the message its own rule prints, so a rule taken out cannot pass on another
# rule's failure further down.
@pytest.mark.parametrize("tools, message", [
    pytest.param(FakeTools(export_exit=2), "uv export for linux-x86_64 base failed",
                 id="the locked export fails"),
    pytest.param(FakeTools(more=["example @ https://example.com/example.whl"]),
                 "not a pinned release", id="the export holds an entry that is not a pin"),
    pytest.param(FakeTools(write_report=False, pip_exit=1), "wrote no report",
                 id="pip-audit writes no report"),
    pytest.param(FakeTools(pip_exit=2), "wrote no report", id="pip-audit crashes"),
    pytest.param(FakeTools(pip="not json"), "the report is not JSON",
                 id="pip-audit writes garbage"),
    pytest.param(FakeTools(pip={"dependencies": []}), "the report lists no dependencies",
                 id="pip-audit audits nothing"),
    pytest.param(FakeTools(drop=["requests"]), "not audited: requests",
                 id="pip-audit leaves a pin out of its report"),
    pytest.param(FakeTools(npm="", npm_exit=1), "the report is not JSON",
                 id="npm audit prints nothing"),
    pytest.param(FakeTools(npm={"error": {"code": "ENOLOCK"}}, npm_exit=1),
                 "it reported an error", id="npm errors"),
    pytest.param(FakeTools(npm=CLEAN_NPM, npm_exit=127), "exited 127", id="npm is missing"),
    pytest.param(FakeTools(npm={"vulnerabilities": {}}), "no vulnerabilities or metadata",
                 id="npm's report has no metadata"),
    pytest.param(FakeTools(npm={"vulnerabilities": {}, "metadata": {}}),
                 "no vulnerabilities or metadata", id="npm's metadata counts nothing"),
])
def test_a_tool_that_fails_to_run_fails_the_job(tree: Path, tools: FakeTools, message: str,
                                                 _summary) -> None:
    assert _run(tree, tools) == 2
    text = _summary.read_text(encoding="utf-8")
    assert "The audit did not run" in text and message in text, text


def test_an_empty_export_fails_the_job(tree: Path, monkeypatch, _summary) -> None:
    monkeypatch.setitem(EXPORTS, "beta-app", [])
    assert _run(tree, FakeTools()) == 2
    assert "linux-x86_64 beta-app holds no package to audit" in _summary.read_text("utf-8")


def test_an_unreadable_baseline_fails_the_job(tree: Path) -> None:
    assert _run(tree, FakeTools(), "[[waiver]\n") == 2


def test_a_tree_without_a_bundle_lock_fails_the_job(tree: Path, _summary) -> None:
    shutil.rmtree(tree / ".github" / "bundle-lock" / "darwin-arm64")
    assert _run(tree, FakeTools()) == 2
    assert "the darwin-arm64 bundle lock cannot be read" in _summary.read_text("utf-8")


# ── the tree's own baseline ───────────────────────────────────────────────────────────────────


def test_the_checked_in_baseline_is_well_formed_and_each_waiver_is_live() -> None:
    """Every waiver in the tree has all its fields and expires within 30 days of today; the
    audit itself, on the night, adds that each one still matches a finding."""
    waivers = audit.load_waivers(audit.BASELINE)
    today = datetime.datetime.now(datetime.UTC).date()
    for entry in waivers:
        assert set(audit.FIELDS) <= set(entry), entry
        assert entry["scope"] in audit.SCOPES, entry
        assert (ROOT / entry["file"]).is_file(), entry
        assert today <= entry["expires"] <= today + datetime.timedelta(days=audit.MAX_DAYS), entry


# ── the workflows run it, unmasked ────────────────────────────────────────────────────────────


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _on(wf: dict) -> dict:
    on = wf.get("on", wf.get(True))  # YAML 1.1 reads a bare `on` key as true
    return on if isinstance(on, dict) else {k: None for k in (on if isinstance(on, list) else [on])}


def audit_job_problems(job: dict) -> list[str]:
    problems: list[str] = []
    if "continue-on-error" in job:
        problems.append("the job continues on error")
    if job.get("if"):
        problems.append(f"the job runs only if {job['if']}")
    runs = [str(s.get("run", "")) for s in job.get("steps") or []]
    command = [r for r in runs if "scripts/audit_locks.py run" in r]
    if len(command) != 1:
        problems.append(f"{len(command)} steps run scripts/audit_locks.py run, not one")
    elif any(mask in command[0] for mask in ("|| true", "|| :", "set +e")):
        problems.append("the audit's exit status is masked")
    if any(s.get("continue-on-error") for s in job.get("steps") or []):
        problems.append("a step continues on error")
    if not any("pip-audit==2.10.1" in r for r in runs):
        problems.append("pip-audit is not installed at the one pinned release")
    if not any(str(s.get("uses", "")).startswith("actions/setup-node@")
               for s in job.get("steps") or []):
        problems.append("no Node is set up, so npm audit cannot run")
    if job.get("permissions") not in (None, {"contents": "read"}):
        problems.append(f"the job holds {job.get('permissions')!r}")
    return problems


def nightly_problems(wf: dict) -> list[str]:
    problems = audit_job_problems((wf.get("jobs") or {}).get("audit") or {})
    on = _on(wf)
    if set(on) != {"schedule", "workflow_dispatch"}:
        problems.append(f"the nightly audit runs on {sorted(map(str, on))}")
    crons = [str(e.get("cron", "")).split() for e in on.get("schedule") or []]
    if len(crons) != 1 or crons[0][2:] != ["*", "*", "*"]:
        problems.append(f"the nightly audit's schedule {crons} is not one daily cron")
    if wf.get("permissions") != {"contents": "read"}:
        problems.append(f"the workflow's permissions are {wf.get('permissions')!r}")
    jobs = wf.get("jobs") or {}
    report = jobs.get("report") or {}
    if report.get("needs") != "audit" or report.get("if") != "${{ !cancelled() }}":
        problems.append("report does not follow the audit job whatever its result")
    if report.get("permissions") != {"issues": "write"}:
        problems.append(f"report holds {report.get('permissions')!r}, not issues: write alone")
    if (report.get("env") or {}).get("TITLE") != "Dependency audit is red":
        problems.append("report's TITLE is not 'Dependency audit is red'")
    text = "\n".join(str(s.get("run", "")) for s in report.get("steps") or [])
    for verb in ("gh issue create", "gh issue comment", "gh issue close"):
        if verb not in text:
            problems.append(f"report never runs {verb}")
    if "select(.title == env.TITLE)" not in text:
        problems.append("report does not find its issue by the exact title")
    for name, job in jobs.items():
        # The audit job's own permissions are audit_job_problems'; report's are checked above.
        if name not in ("report", "audit") and job.get("permissions") != {"contents": "read"}:
            problems.append(f"{name} holds {job.get('permissions')!r}")
    return problems


def test_ci_runs_the_audit_on_every_run_unmasked_beside_the_other_jobs() -> None:
    job = _workflow("ci.yml")["jobs"]["audit"]
    assert audit_job_problems(job) == []
    assert "needs" not in job, "the audit waits on nothing, so it adds no wall time"


def test_the_nightly_audit_runs_the_same_command_and_reports_one_issue() -> None:
    assert nightly_problems(_workflow("audit.yml")) == []


def _masked(job: dict) -> dict:
    for step in job["steps"]:
        if "scripts/audit_locks.py run" in str(step.get("run", "")):
            step["run"] = step["run"] + " || true"
    return job


@pytest.mark.parametrize("plant, expected", [
    (lambda j: {**j, "continue-on-error": True}, "the job continues on error"),
    (lambda j: {**j, "if": "github.event_name == 'schedule'"}, "the job runs only if"),
    (lambda j: {**j, "steps": [s for s in j["steps"]
                               if "audit_locks" not in str(s.get("run", ""))]}, "0 steps run"),
    (_masked, "masked"),
    (lambda j: {**j, "steps": [{**s, "continue-on-error": True} for s in j["steps"]]},
     "a step continues on error"),
    (lambda j: {**j, "steps": [{**s, "run": s["run"].replace("pip-audit==2.10.1", "pip-audit")}
                               if "pip-audit==" in str(s.get("run", "")) else s
                               for s in j["steps"]]}, "pinned release"),
    (lambda j: {**j, "steps": [s for s in j["steps"]
                               if "setup-node" not in str(s.get("uses", ""))]}, "no Node"),
    (lambda j: {**j, "permissions": {"issues": "write"}}, "the job holds"),
])
def test_the_audit_job_rule_fails_on_a_planted(plant, expected) -> None:
    import copy
    job = plant(copy.deepcopy(_workflow("ci.yml")["jobs"]["audit"]))
    problems = audit_job_problems(job)
    assert len(problems) == 1 and expected in problems[0], problems


@pytest.mark.parametrize("plant, expected", [
    (lambda w: w.update({True: {"schedule": [{"cron": "47 4 * * *"}], "workflow_dispatch": {},
                                "pull_request": None}}), "runs on"),
    (lambda w: w.update({True: {"schedule": [{"cron": "47 4 * * 1"}],
                                "workflow_dispatch": {}}}), "not one daily cron"),
    (lambda w: w.update(permissions={"contents": "write"}), "the workflow's permissions"),
    (lambda w: w["jobs"]["report"].pop("if"), "whatever its result"),
    (lambda w: w["jobs"]["report"].update(permissions={"issues": "write",
                                                       "contents": "write"}), "report holds"),
    (lambda w: w["jobs"]["report"]["env"].update(TITLE="Audit"), "TITLE"),
    (lambda w: w["jobs"]["report"]["steps"].pop(1), "gh issue close"),
    (lambda w: w["jobs"]["audit"].update(permissions={"contents": "write"}), "the job holds"),
])
def test_the_nightly_rule_fails_on_a_planted(plant, expected) -> None:
    wf = _workflow("audit.yml")
    on = wf.pop("on", None) or wf.pop(True)
    wf[True] = on
    plant(wf)
    problems = nightly_problems(wf)
    assert problems and all(expected in p for p in problems), problems
