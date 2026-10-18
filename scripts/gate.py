"""The one apps gate: the static checks, the repository's own tests, then every bundle's suite.

CI and a local run call this same script. CI's ``static`` job runs the ``static`` part, its
``tests`` job the ``rails`` and ``bundles`` parts, and ``make gate`` (or this script with no part
named) runs all three, in that order. Each part runs even when an earlier one is red, and the gate
exits 1 when any part failed.

Parts:

- ``static``: ``.github/scripts/run_checks.py``, every static check on its own row.
- ``rails``: ``python -m pytest .github/tests -n N``, once, on ``--jobs`` workers (the bundle
  runner's default when ``--jobs`` is not given). They are a fixed cost in every pull request's
  run, so they get the same workers the bundles do; the receipt records how many.
- ``bundles``: ``scripts/test_bundles.py``, each bundle's suite in a locked environment holding
  only what it declares, ``--jobs`` at once; ``--changed BASE`` runs only the bundles a change
  since BASE can affect, plus every bundle claiming ``quality.tested`` (the quality check reads
  their recorded results), and ``--float`` (the weekly canary's mode, never merge evidence) resolves
  each bundle's declared ranges afresh instead of installing the bundle lock.

The gate never installs core. It records the core installed in the interpreter that runs the
parts, and ``--core <sha>`` refuses to run at all when that core was built from another commit.
CI passes ``--core`` the commit in ``core.lock``, so a CI run cannot test any other core. A local
run without ``--core`` compares the installed core with ``core.lock`` and warns when they differ,
then goes on, so work against a core that is not pushed yet stays possible.

Every run writes a receipt, ``receipt.json`` under ``--results``: the apps commit (and whether the
work tree had uncommitted changes), the core version and commit, what the install asked for, the
commit ``core.lock`` pins and whether the two match, the toolchain (Python, uv, Node,
npm and, in CI, the runner image), which bundles were selected and why, and each part's result.
The static rows and the bundle results sit beside it, with each bundle environment's receipt
(its key and the inputs the key is a digest of) under ``bundles/environments/``.

One results folder holds one run: a second gate started on the same folder refuses, and names
``--results`` as the way to run it elsewhere, so two validations side by side never clobber each
other's receipt. Each part it starts holds that folder's lock too, and so does every bundle's
pytest, so a part or a pytest left running by a gate that was stopped still keeps a second gate
out. The bundle environments are one per checkout: a second gate's bundles part in the same
checkout refuses, naming the run that holds them.

    python scripts/gate.py [static] [rails] [bundles] [--python PY] [--bundle NAME]...
                           [--jobs N] [--changed BASE] [--float] [--core SHA] [--results DIR]
"""

from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PARTS = ("static", "rails", "bundles")
DEFAULT_RESULTS = ROOT / ".local" / "scratch" / "gate"

#: What the parts' interpreter reports about itself and the core installed in it.
_PROBE = r"""
import importlib.metadata as md, json, sys
core = {"version": None, "commit": None, "source": None, "requested": None}
try:
    dist = md.distribution("personalclaw")
except md.PackageNotFoundError:
    pass
else:
    core["version"] = dist.version
    direct = json.loads(dist.read_text("direct_url.json") or "{}")
    vcs = direct.get("vcs_info") or {}
    core["commit"] = vcs.get("commit_id")
    core["requested"] = vcs.get("requested_revision")
    core["source"] = direct.get("url")
print(json.dumps({"python": ".".join(map(str, sys.version_info[:3])), "core": core}))
"""


def _out(argv: list[str], cwd: Path = ROOT) -> str | None:
    """A command's first line of output, or None when it cannot run or fails."""
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    lines = proc.stdout.strip().splitlines()
    return lines[0].strip() if lines else ""


def _apps() -> dict:
    """The apps commit, and whether the work tree differs from it (None: not a git checkout)."""
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
    except OSError:
        status = None
    dirty = None if status is None or status.returncode else bool(status.stdout.strip())
    return {"sha": _out(["git", "rev-parse", "HEAD"]), "dirty": dirty}


def _ci() -> dict | None:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return None
    keys = ("GITHUB_SHA", "GITHUB_REF", "GITHUB_EVENT_NAME", "GITHUB_RUN_ID",
            "GITHUB_RUN_ATTEMPT", "GITHUB_JOB")
    return {k.removeprefix("GITHUB_").lower(): os.environ.get(k) for k in keys}


def _runner() -> str:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        parts = (os.environ.get(k, "") for k in ("ImageOS", "ImageVersion", "RUNNER_ARCH"))
        return " ".join(parts).strip()
    return f"local {platform.system()} {platform.machine()}"


def _toolchain(probe: dict) -> dict:
    return {
        "python": probe["python"],
        "uv": _out(["uv", "--version"]),
        "node": _out(["node", "--version"]),
        "npm": _out(["npm", "--version"]),
        "runner": _runner(),
    }


def _load_bundle_runner():
    spec = importlib.util.spec_from_file_location("_gate_test_bundles", ROOT / "scripts" / "test_bundles.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # The runner's dataclasses look their module up in sys.modules while it is still loading.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _lock(core: dict) -> None:
    """Add the commit ``core.lock`` pins to *core*, and whether the installed core is it.

    ``scripts/core_lock.py`` is the one reader of the file's format. A missing or malformed lock
    is recorded as none and warned about; it is not this gate's to refuse (CI's own read step is).
    """
    spec = importlib.util.spec_from_file_location("_gate_core_lock", ROOT / "scripts" / "core_lock.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    try:
        lock = module.read_lock(ROOT / "core.lock")
    except ValueError as exc:
        core["lock"], core["matches_lock"] = None, None
        print(f"gate: warning: no usable core.lock ({exc}); the receipt records no lock",
              file=sys.stderr)
        return
    core["lock"] = lock
    core["matches_lock"] = core["commit"] == lock
    if not core["matches_lock"]:
        print(
            f"gate: warning: core.lock pins {lock}, but the installed core is commit "
            f"{core['commit'] or 'not recorded'} (version {core['version']}). CI tests the locked "
            f'core; to match it locally: uv pip install "{module.spec(lock)}"',
            file=sys.stderr,
        )


def _selection(named: list[str], changed: str | None) -> dict:
    if named:
        return {"bundles": sorted(named), "reason": "named on the command line"}
    runner = _load_bundle_runner()
    bundles, reason = runner.select((), changed, changed is not None, ROOT)
    return {"bundles": [b.relative_to(ROOT).as_posix() for b in bundles], "reason": reason}


def _runner_flags(args) -> list[str]:
    flags = []
    if args.jobs is not None:
        flags += ["--jobs", str(args.jobs)]
    if args.changed is not None:
        flags += ["--changed", args.changed]
    if args.floating:
        flags.append("--float")
    if args.core:
        flags += ["--core-commit", args.core]
    return flags


def _rails_workers(jobs: int | None) -> int:
    """The rails' pytest-xdist workers: ``--jobs`` when given, else the bundle runner's default."""
    return jobs if jobs is not None else _load_bundle_runner().default_jobs()


def _run_part(part: str, *, python: str, results: Path, bundles: list[str],
              flags: list[str], keep: tuple[int, ...] = (), workers: int = 2) -> int:
    if part == "static":
        argv = [python, ".github/scripts/run_checks.py", "--report", str(results / "static.json")]
    elif part == "rails":
        argv = [python, "-m", "pytest", ".github/tests", "-n", str(workers), "-q"]
    else:
        argv = [python, "scripts/test_bundles.py", "--results", str(results / "bundles"),
                *flags, *bundles]
    print(f"\n=== gate: {part}: {' '.join(argv)}", flush=True)
    try:
        # *keep*: the results folder's lock, so a part outlives a killed gate still holding it.
        env = {**os.environ, **({"GATE_RESULTS_LOCK_FD": str(keep[0])} if keep else {})}
        return subprocess.run(argv, cwd=ROOT, check=False, pass_fds=keep, env=env).returncode
    except OSError as exc:
        print(f"gate: could not start the {part} part: {exc}", file=sys.stderr)
        return 127


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/gate.py",
        description="Run the apps gate: static checks, the repository's tests, the bundle suites.",
    )
    parser.add_argument("parts", nargs="*", metavar="part",
                        help=f"which parts to run, of {', '.join(PARTS)} (default: all, in order)")
    parser.add_argument("--python", default=sys.executable,
                        help="interpreter whose environment holds core and pytest (default: this one)")
    parser.add_argument("--bundle", action="append", default=[], metavar="NAME",
                        help="run only this bundle's suite (repeatable; default: every tested bundle)")
    parser.add_argument("--jobs", type=int, metavar="N",
                        help="bundles to run at once, and the rails' pytest workers (default: "
                             "half the CPUs, at most 8)")
    parser.add_argument("--changed", metavar="BASE",
                        help="run only the bundles a change since BASE can affect")
    parser.add_argument("--float", action="store_true", dest="floating",
                        help="resolve each bundle's declared ranges afresh, not from the bundle "
                             "lock (the weekly canary's mode; never merge evidence)")
    parser.add_argument("--core", metavar="SHA",
                        help="refuse to run unless the installed core was built from this commit")
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS,
                        help="where the receipt and results go (default: .local/scratch/gate)")
    args = parser.parse_args(argv)
    unknown = [p for p in args.parts if p not in PARTS]
    if unknown:
        parser.error(f"unknown part(s): {', '.join(unknown)}; the parts are {', '.join(PARTS)}")
    parts = [p for p in PARTS if p in args.parts] if args.parts else list(PARTS)
    if args.bundle and args.changed is not None:
        parser.error("name bundles with --bundle or pass --changed, not both")

    results = args.results if args.results.is_absolute() else ROOT / args.results
    results.mkdir(parents=True, exist_ok=True)
    # One results folder, one run: held until this process and every part it starts have exited.
    held = open(results / ".gate.lock", "a")
    try:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(
            f"gate: another gate run is using {results}. Wait for it, or run this one with "
            "--results DIR to keep its receipt and results in another folder.",
            file=sys.stderr,
        )
        return 2

    raw = _out([args.python, "-c", _PROBE])
    if raw is None:
        print(f"gate: cannot run the interpreter {args.python}", file=sys.stderr)
        return 2
    probe = json.loads(raw)
    core = probe["core"]
    if args.core:
        want = args.core.strip().lower()
        have = (core["commit"] or "").lower()
        if len(want) < 7 or not have.startswith(want):
            print(
                f"gate: the core named, {args.core}, is not the core installed in {args.python} "
                f"(installed: version {core['version']}, commit {core['commit'] or 'not recorded'}). "
                "Install the named core into that environment, then run the gate again.",
                file=sys.stderr,
            )
            return 2
    # The tree's state before the gate loads anything: a loaded script's bytecode cache must
    # not read as an uncommitted change.
    apps = _apps()
    _lock(core)

    receipt = {
        "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "apps": apps,
        "core": core,
        "toolchain": _toolchain(probe),
        "ci": _ci(),
        "selection": _selection(args.bundle, args.changed) if "bundles" in parts else None,
        "parts": [],
    }
    for stale in ("static.json", "bundles", "receipt.json"):
        target = results / stale
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    results.mkdir(parents=True, exist_ok=True)

    workers = _rails_workers(args.jobs) if "rails" in parts else 2
    for part in parts:
        started = time.monotonic()
        rc = _run_part(part, python=args.python, results=results, bundles=args.bundle,
                       flags=_runner_flags(args), keep=(held.fileno(),), workers=workers)
        row = {
            "part": part,
            "result": "pass" if rc == 0 else "FAIL",
            "returncode": rc,
            "seconds": round(time.monotonic() - started, 1),
        }
        if part == "rails":
            row["workers"] = workers
        receipt["parts"].append(row)

    ran = results / "bundles" / "results.json"
    if ran.is_file():
        summary = json.loads(ran.read_text(encoding="utf-8"))
        receipt["selection"] = summary.get("selection") or receipt["selection"]
        receipt["bundles"] = {k: summary.get(k) for k in ("jobs", "resolution", "environments")}
    static = results / "static.json"
    if static.is_file():
        receipt["static_checks"] = json.loads(static.read_text(encoding="utf-8"))["checks"]
    receipt["verdict"] = "pass" if all(p["returncode"] == 0 for p in receipt["parts"]) else "FAIL"
    receipt["finished"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (results / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")

    rows = ["| part | result | seconds |", "|---|---|---|"]
    rows += [f"| {p['part']} | {p['result']} | {p['seconds']:.1f} |" for p in receipt["parts"]]
    table = "\n".join(rows) + "\n"
    print("\n" + table, end="")
    print(f"gate: {receipt['verdict']}; receipt in {results / 'receipt.json'}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if os.environ.get("GITHUB_ACTIONS") == "true" and summary:
        apps, tc = receipt["apps"], receipt["toolchain"]
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(
                "### Apps gate\n\n" + table + "\n"
                f"apps `{apps['sha']}` · core `{core['commit']}` ({core['version']}; "
                f"core.lock `{core['lock']}`, matches: {core['matches_lock']}) · "
                f"Python {tc['python']} · {tc['uv']} · Node {tc['node']} · npm {tc['npm']} · "
                f"{tc['runner']}\n\n"
            )
    return 0 if receipt["verdict"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
