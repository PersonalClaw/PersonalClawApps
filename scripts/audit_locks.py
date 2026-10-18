"""The dependency audit of the locks the apps ship: every bundle environment's Python graph, and
every npm lock a bundle ships.

What is audited is what is installed, read from the committed locks and never from a fresh
resolution:

- each bundle environment's Python graph, for each platform's bundle lock
  (``.github/bundle-lock/<platform>/uv.lock``): the base environment (core's dependencies and the
  pytest pins, which a bundle that declares nothing runs in) and one per extra, the bundle's
  folder, exported with the bundle runner's own ``uv export`` argv
  (``scripts/test_bundles.py``'s ``export_argv``), so it is the graph the runner installs. The
  lock resolves for one platform, so every exported entry whose marker holds there is audited
  whatever machine runs the audit. ``pip-audit`` reads the pins with ``--no-deps --disable-pip``,
  so a lock pinned to an older vulnerable release is reported even when a fresh resolution would
  be clean. A package is audited once per lock, at the one version the lock holds, and a finding
  names every environment that installs it;
- each ``package-lock.json`` a bundle ships (``<bundle>/**/package-lock.json``, the UI bundles'
  build graphs), with ``npm audit --package-lock-only``.

Core's own graph is core's audit's; core is never exported here (``--no-emit-package
personalclaw``), but the dependencies it brings into every environment are.

The verdict, as core's (``tools/ci/audit.py``):

- a tool that fails to run (a non-zero exit other than "found something", no report, a report
  that cannot be read, or one that audited other packages than it was given) fails the job,
  exit 2: a missing audit is never a clean one;
- a high or critical finding that no live waiver covers fails it, exit 1. ``pip-audit`` reports
  no severity, so a Python finding is ``unrated`` and gates as if it were high;
- a waiver that is expired, more than ``MAX_DAYS`` out, missing a field, or matching no finding
  fails it too, exit 1, so the list cannot rot;
- every other finding (moderate, low) is reported in the job summary and gates nothing.

Waivers are ``[[waiver]]`` entries in ``.github/audit-baseline.toml``: ``file`` (the lock, from
the repository root), ``scope`` (``pip-audit`` or ``npm-audit``), ``reference``
("<package> <advisory>"), ``owner``, ``reason`` and ``expires``.

    python scripts/audit_locks.py run --pip-audit PYTHON --work DIR [--baseline FILE]

``--pip-audit`` names the Python pip-audit is installed in, which also runs this script in CI
(it brings ``packaging``, which reading the export's markers needs). It needs the network: CI's
``audit`` job runs it on every pull request and the nightly ``audit.yml`` on main; no gate part
does.
"""

from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

#: The severities a finding fails the run at, when no live waiver covers it.
GATING = frozenset({"high", "critical"})

#: A Python finding's severity: pip-audit reports none, so it gates as a high one would.
UNRATED = "unrated"

#: A waiver expires at most this many days after it is written or renewed.
MAX_DAYS = 30

FIELDS = ("file", "scope", "reference", "owner", "reason", "expires")
SCOPES = ("pip-audit", "npm-audit")

BASELINE = ROOT / ".github" / "audit-baseline.toml"

#: The base environment's name in a graph label: the one a bundle that declares nothing runs in.
BASE = "base"

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]

_PIN = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==([^\s;\\]+)")


class ToolFailure(Exception):
    """An audit tool did not run to a readable report."""


@dataclass(frozen=True)
class Finding:
    """One advisory against one package of one lock, and the environments that install it."""

    file: str
    graphs: tuple[str, ...]
    package: str
    version: str
    advisory: str
    severity: str

    @property
    def scope(self) -> str:
        return "pip-audit" if self.file.endswith("uv.lock") else "npm-audit"

    @property
    def reference(self) -> str:
        return f"{self.package} {self.advisory}"

    @property
    def gates(self) -> bool:
        return self.severity in GATING or self.severity == UNRATED


@dataclass(frozen=True)
class Verdict:
    findings: tuple[Finding, ...]
    gating: tuple[Finding, ...]
    waived: tuple[Finding, ...]
    problems: tuple[str, ...]

    @property
    def failed(self) -> bool:
        return bool(self.gating or self.problems)


def _load(name: str, root: Path = ROOT):
    spec = importlib.util.spec_from_file_location(f"_audit_locks_{name}",
                                                  root / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module  # a dataclass looks its module up while it loads
    spec.loader.exec_module(module)
    return module


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


# ── what is audited ───────────────────────────────────────────────────────────────────────────


def python_graphs(root: Path = ROOT) -> dict[str, list[str | None]]:
    """Each platform's bundle lock, and the environments the runner installs from it: the base
    one (None) and one per extra the lock's project declares, which is one per bundle that
    declares ``pythonDependencies`` (the static bundle-lock row holds the project to them)."""
    bundle_lock = _load("bundle_lock", root)
    out: dict[str, list[str | None]] = {}
    for platform in bundle_lock.PLATFORMS:
        project = root / ".github" / "bundle-lock" / platform / "pyproject.toml"
        try:
            data = tomllib.loads(project.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as err:
            raise ToolFailure(f"the {platform} bundle lock cannot be read: {err}") from None
        extras = sorted(((data.get("project") or {}).get("optional-dependencies") or {}))
        out[platform] = [None, *extras]
    return out


def npm_locks(root: Path = ROOT) -> list[Path]:
    """Every ``package-lock.json`` a bundle ships, outside any ``node_modules``."""
    found = []
    for manifest in sorted(root.glob("*/app.json")):
        for lock in sorted(manifest.parent.rglob("package-lock.json")):
            if "node_modules" not in lock.relative_to(root).parts:
                found.append(lock)
    return found


def pins(export: str, fixed: Mapping[str, str], bundle_lock, graph: str) -> dict[str, str]:
    """The ``name -> version`` pins of one export whose marker holds where the lock resolves.
    An entry that is not a pinned release cannot be audited, so it fails the run."""
    held: dict[str, str] = {}
    for line in export.splitlines():
        if not line.strip() or line[0].isspace() or line.lstrip().startswith(("#", "--")):
            continue
        requirement = line.rstrip().removesuffix("\\").strip()
        found = _PIN.match(requirement)
        if not found:
            raise ToolFailure(f"{graph}: cannot audit {requirement!r}: not a pinned release")
        if not bundle_lock.applies(requirement, dict(fixed)):
            continue
        held[bundle_lock.canonical(found.group(1))] = found.group(2)
    return held


# ── reading the tools' reports ────────────────────────────────────────────────────────────────


def _json(text: str, what: str) -> Any:
    try:
        return json.loads(text)
    except ValueError as err:
        raise ToolFailure(f"{what}: the report is not JSON ({err})") from None


def pip_report(what: str, text: str, given: Mapping[str, str],
               canonical: Callable[[str], str]) -> tuple[list[tuple[str, str, str]], list[str]]:
    """The ``(package, version, advisory)`` findings of a ``pip-audit -f json`` report, and the
    packages it could not audit. It must account for exactly the pins it was given."""
    doc = _json(text, f"pip-audit on {what}")
    deps = doc.get("dependencies") if isinstance(doc, dict) else None
    if not isinstance(deps, list) or not deps:
        raise ToolFailure(f"pip-audit on {what}: the report lists no dependencies")
    found: list[tuple[str, str, str]] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for dep in deps:
        if not isinstance(dep, dict) or not isinstance(dep.get("name"), str):
            raise ToolFailure(f"pip-audit on {what}: a dependency entry has no name")
        seen.add(canonical(dep["name"]))
        if "skip_reason" in dep:
            skipped.append(f"{what}: {dep['skip_reason']}")
            continue
        for vuln in dep.get("vulns") or []:
            aliases = [a for a in vuln.get("aliases") or [] if str(a).startswith("GHSA-")]
            advisory = str(aliases[0] if aliases else vuln.get("id", "?"))
            found.append((canonical(dep["name"]), str(dep.get("version", "?")), advisory))
    if seen != set(given):
        missing = sorted(set(given) - seen)
        extra = sorted(seen - set(given))
        raise ToolFailure(f"pip-audit on {what}: the report does not account for the pins it was "
                          f"given (not audited: {', '.join(missing) or 'none'}; not given: "
                          f"{', '.join(extra) or 'none'})")
    return found, skipped


def npm_findings(file: str, text: str) -> list[Finding]:
    """The advisories in an ``npm audit --json`` report, once each.

    npm lists a package that only depends on a vulnerable one as vulnerable too; only the
    entries ``via`` names as an advisory are findings, so each advisory is counted once."""
    doc = _json(text, f"npm audit of {file}")
    if not isinstance(doc, dict) or "error" in doc:
        detail = doc.get("error") if isinstance(doc, dict) else doc
        raise ToolFailure(f"npm audit of {file}: it reported an error, not an audit ({detail})")
    vulns, meta = doc.get("vulnerabilities"), doc.get("metadata")
    if not isinstance(vulns, dict) or not isinstance(meta, dict) or "vulnerabilities" not in meta:
        raise ToolFailure(f"npm audit of {file}: the report has no vulnerabilities or metadata")
    found: dict[tuple[str, str], Finding] = {}
    for name, vuln in vulns.items():
        for via in vuln.get("via") or [] if isinstance(vuln, dict) else []:
            if not isinstance(via, dict):
                continue
            url = str(via.get("url", ""))
            advisory = url.rsplit("/", 1)[-1] if url else str(via.get("source", "?"))
            package = str(via.get("name", name))
            found[(package, advisory)] = Finding(
                file, ("npm",), package, str(via.get("range", "?")), advisory,
                str(via.get("severity", UNRATED)),
            )
    return sorted(found.values(), key=lambda f: (f.package, f.advisory))


# ── the waivers ───────────────────────────────────────────────────────────────────────────────


def load_waivers(path: Path) -> list[dict[str, Any]]:
    entries = tomllib.loads(path.read_text(encoding="utf-8")).get("waiver", [])
    if not isinstance(entries, list):
        raise ToolFailure(f"{path.name}: `waiver` is not a list of tables")
    return entries


def judge(findings: Sequence[Finding], waivers: Sequence[Mapping[str, Any]],
          today: datetime.date) -> Verdict:
    problems: list[str] = []
    waived: set[tuple[str, str, str]] = set()
    for i, entry in enumerate(waivers, 1):
        where = f"waiver {i} ({entry.get('reference', '?')})"
        missing = [
            f for f in FIELDS
            if f not in entry
            or (f != "expires" and not (isinstance(entry[f], str) and entry[f].strip()))
        ]
        if missing:
            problems.append(f"{where}: missing {', '.join(missing)}")
            continue
        expires = entry["expires"]
        if type(expires) is not datetime.date:
            problems.append(f"{where}: expires must be a date, such as {today.isoformat()}")
            continue
        if entry["scope"] not in SCOPES:
            problems.append(f"{where}: scope must be one of {', '.join(SCOPES)}")
            continue
        if expires < today:
            problems.append(f"{where}: expired on {expires.isoformat()}: fix it, or renew it")
            continue
        if (expires - today).days > MAX_DAYS:
            problems.append(f"{where}: expires {expires.isoformat()}, more than {MAX_DAYS} days "
                            "out")
            continue
        key = (entry["scope"], entry["file"], entry["reference"])
        if not any((f.scope, f.file, f.reference) == key for f in findings):
            problems.append(f"{where}: matches no finding, so it is stale: delete it")
            continue
        waived.add(key)
    covered = [f for f in findings if (f.scope, f.file, f.reference) in waived]
    gating = [f for f in findings if f.gates and f not in covered]
    return Verdict(tuple(findings), tuple(gating), tuple(covered), tuple(problems))


# ── running the tools ─────────────────────────────────────────────────────────────────────────


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    # The bundle runner's uv environment: no index, preference or config setting from the
    # caller's environment can change what the export holds.
    env = _load("test_bundles")._uv_env()
    return subprocess.run(list(argv), capture_output=True, text=True, check=False, env=env)


def _tail(proc: subprocess.CompletedProcess[str]) -> str:
    return (proc.stderr or proc.stdout or "").strip()[-600:]


def _batches(graphs: Mapping[str, Mapping[str, str]]) -> list[dict[str, str]]:
    """The pins of every graph of one lock, in as few requirement files as hold each package
    once: one, while the lock holds one version of each package."""
    batches: list[dict[str, str]] = []
    for held in graphs.values():
        for name, version in sorted(held.items()):
            if any(b.get(name) == version for b in batches):
                continue
            target = next((b for b in batches if name not in b), None)
            if target is None:
                target = {}
                batches.append(target)
            target[name] = version
    return batches


def audit_python(pip_audit_python: str, work: Path, run: Runner,
                 root: Path = ROOT) -> tuple[list[Finding], list[str], int]:
    """Every platform's bundle environments, audited. Returns the findings, the packages
    pip-audit could not audit, and how many environments were exported."""
    bundle_lock = _load("bundle_lock", root)
    test_bundles = _load("test_bundles", root)
    findings: list[Finding] = []
    skipped: list[str] = []
    exported = 0
    for platform, extras in python_graphs(root).items():
        lock_dir = root / ".github" / "bundle-lock" / platform
        file = _rel(lock_dir / "uv.lock", root)
        fixed = bundle_lock.lock_environment(
            (lock_dir / "pyproject.toml").read_text(encoding="utf-8"))
        graphs: dict[str, dict[str, str]] = {}
        for extra in extras:
            label = f"{platform} {extra or BASE}"
            proc = run(test_bundles.export_argv(lock_dir, extra))
            if proc.returncode != 0:
                raise ToolFailure(f"uv export for {label} failed: {_tail(proc)}")
            held = pins(proc.stdout, fixed, bundle_lock, label)
            if not held:
                raise ToolFailure(f"uv export for {label} holds no package to audit")
            graphs[extra or BASE] = held
            exported += 1
        for n, batch in enumerate(_batches(graphs), 1):
            req, report = work / f"{platform}-{n}.txt", work / f"{platform}-{n}.json"
            req.write_text("".join(f"{k}=={v}\n" for k, v in sorted(batch.items())),
                           encoding="utf-8")
            report.unlink(missing_ok=True)
            proc = run([pip_audit_python, "-m", "pip_audit", "-r", str(req), "--no-deps",
                        "--disable-pip", "-f", "json", "-o", str(report)])
            # 1 is "found something" only when the report was written; anything else failed.
            if proc.returncode not in (0, 1) or not report.is_file():
                raise ToolFailure(f"pip-audit on {file} wrote no report: {_tail(proc)}")
            got, skips = pip_report(file, report.read_text(encoding="utf-8"), batch,
                                    bundle_lock.canonical)
            skipped += skips
            for package, version, advisory in got:
                users = tuple(g for g, held in graphs.items() if held.get(package) == version)
                findings.append(Finding(file, users, package, version, advisory, UNRATED))
    return findings, skipped, exported


def audit_npm(run: Runner, root: Path = ROOT) -> tuple[list[Finding], int]:
    findings: list[Finding] = []
    locks = npm_locks(root)
    for lock in locks:
        file = _rel(lock, root)
        proc = run(["npm", "audit", "--package-lock-only", "--json", "--prefix",
                    str(lock.parent)])
        if proc.returncode not in (0, 1):
            raise ToolFailure(f"npm audit of {file} exited {proc.returncode}: {_tail(proc)}")
        findings += npm_findings(file, proc.stdout)
    return findings, len(locks)


# ── the report ────────────────────────────────────────────────────────────────────────────────


def summary(verdict: Verdict, skipped: Sequence[str], graphs: int, locks: int) -> str:
    lines = ["### Dependency audit", ""]
    lines.append(
        f"{graphs} bundle environment graph(s) and {locks} npm lock(s) audited. "
        f"{len(verdict.findings)} finding(s): {len(verdict.gating)} that fail the run, "
        f"{len(verdict.waived)} under a live waiver."
    )
    if verdict.findings:
        lines += [
            "",
            "| Lock | Environments | Package | Version | Advisory | Severity | Status |",
            "|---|---|---|---|---|---|---|",
        ]
        for f in verdict.findings:
            status = ("**fails the run**" if f in verdict.gating
                      else "waived" if f in verdict.waived else "reported")
            cells = (f.file, ", ".join(f.graphs), f.package, f.version, f.advisory, f.severity,
                     status)
            lines.append("| " + " | ".join(cells) + " |")
    if verdict.problems:
        lines += ["", "**Waivers that fail the run:**", *(f"- {p}" for p in verdict.problems)]
    if skipped:
        lines += ["", "Not audited (not on PyPI):", *(f"- {s}" for s in skipped)]
    return "\n".join(lines) + "\n"


def _publish(text: str) -> None:
    print(text, end="")
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)


def main(argv: Sequence[str], run: Runner = _run, today: datetime.date | None = None,
         root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(prog="python scripts/audit_locks.py")
    sub = parser.add_subparsers(dest="command", required=True)
    go = sub.add_parser("run", help="audit every lock the apps ship and judge the findings")
    go.add_argument("--pip-audit", required=True, help="the Python pip-audit is installed in")
    go.add_argument("--work", type=Path, required=True, help="a folder for the exports")
    go.add_argument("--baseline", type=Path, default=root / ".github" / "audit-baseline.toml")
    args = parser.parse_args(list(argv))
    day = today or datetime.datetime.now(datetime.UTC).date()
    try:
        args.work.mkdir(parents=True, exist_ok=True)
        waivers = load_waivers(args.baseline)
        python, skipped, graphs = audit_python(args.pip_audit, args.work, run, root)
        npm, locks = audit_npm(run, root)
        verdict = judge([*python, *npm], waivers, day)
    except (ToolFailure, OSError, tomllib.TOMLDecodeError) as err:
        print(f"::error title=the dependency audit did not run::{err}")
        _publish(f"### Dependency audit\n\n**The audit did not run:** {err}\n")
        return 2
    _publish(summary(verdict, skipped, graphs, locks))
    for f in verdict.gating:
        print(f"::error title=unwaived {f.severity} finding::{f.file} "
              f"({', '.join(f.graphs)}): {f.reference}")
    for p in verdict.problems:
        print(f"::error title=waiver::{p}")
    return 1 if verdict.failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
