"""Run every static check in this folder, report each on its own row, and let no red hide another.

A static check is a ``check_*.py`` beside this file. Each is found, never listed by hand, so a new
check cannot be left out of CI: it is a row the first time it exists. Each runs in a process of
its own, from the repository root, exactly as running it by hand does, and a check that fails or
crashes is recorded and the next one still runs. At the end a table names every check, its result
and how long it took; the run exits 1 when any row failed, and also when there was no check to run
(a runner that checked nothing must not read as a pass).

The table goes to stdout, and to the job summary in CI. In CI each check's output is folded into a
group of its own, and a failed check is an annotation that names it. ``--report FILE`` also writes
the rows as JSON, which the gate keeps in its receipt; ``--list`` prints each row's name and script
without running anything.

Rows keep the names the checks had as CI jobs of their own, because the docs cite them: a row is
the script's name without ``check_``, with hyphens, except where the job's name differed.

    python .github/scripts/run_checks.py
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]

#: Where a check's row name is not its script's name, because the job it replaces was named so.
ROW_NAMES = {
    "check_manifests.py": "manifest-validate",
    "check_settings_route_references.py": "settings-route-ratchet",
}


def row_name(script: Path) -> str:
    return ROW_NAMES.get(script.name) or script.stem.removeprefix("check_").replace("_", "-")


def discover() -> list[Path]:
    return sorted(p for p in HERE.glob("check_*.py") if p.is_file())


def _in_ci() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


def run_one(script: Path) -> dict:
    """Run one check; its output and exit code, whatever it does."""
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, str(script)],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        returncode, output = proc.returncode, proc.stdout
    except OSError as exc:  # the interpreter itself could not start
        returncode, output = 127, f"could not start {script.name}: {exc}\n"
    return {
        "row": row_name(script),
        "script": script.relative_to(ROOT).as_posix(),
        "returncode": returncode,
        "seconds": round(time.monotonic() - started, 2),
        "output": output,
    }


def table(rows: list[dict]) -> str:
    lines = ["| check | result | seconds |", "|---|---|---|"]
    for r in rows:
        result = "pass" if r["returncode"] == 0 else "FAIL"
        lines.append(f"| {r['row']} | {result} | {r['seconds']:.2f} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--report", type=Path, help="also write the rows as JSON to this file")
    parser.add_argument("--list", action="store_true", help="print each row and its script; run nothing")
    args = parser.parse_args(argv)

    scripts = discover()
    if args.list:
        for script in scripts:
            print(f"{row_name(script)} {script.relative_to(ROOT).as_posix()}")
        return 0 if scripts else 1
    if not scripts:
        print(f"error: no check_*.py in {HERE}: there is no check to run", file=sys.stderr)
        return 1

    ci = _in_ci()
    rows = []
    for script in scripts:
        row = run_one(script)
        rows.append(row)
        failed = row["returncode"] != 0
        if ci:
            print(f"::group::{row['row']} ({row['script']})", flush=True)
        else:
            print(f"--- {row['row']} ({row['script']})", flush=True)
        print(row["output"], end="" if row["output"].endswith("\n") or not row["output"] else "\n")
        if ci:
            print("::endgroup::")
            if failed:
                print(
                    f"::error title=static check {row['row']} failed::"
                    f"{row['script']} exited {row['returncode']}; its output is in the "
                    f"{row['row']} group above"
                )
        elif failed:
            print(f"--- {row['row']} FAILED (exit {row['returncode']})")
        sys.stdout.flush()

    failed_rows = [r["row"] for r in rows if r["returncode"] != 0]
    report = table(rows)
    print()
    print(report, end="")
    if failed_rows:
        print(f"\n{len(failed_rows)} of {len(rows)} static check(s) failed: {', '.join(failed_rows)}")
    else:
        print(f"\nOK: all {len(rows)} static check(s) passed")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if ci and summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("### Static checks\n\n" + report + "\n")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        slim = [{k: v for k, v in r.items() if k != "output"} for r in rows]
        args.report.write_text(json.dumps({"checks": slim}, indent=2) + "\n", encoding="utf-8")
    return 1 if failed_rows else 0


if __name__ == "__main__":
    sys.exit(main())
