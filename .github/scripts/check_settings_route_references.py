#!/usr/bin/env python3
"""Fail when a tracked file points users at the removed Settings/Tools route.

This is a repository-wide fact, so it cannot live in one bundle's test suite: CI runs
pytest separately per bundle. Scan Git's tracked-file list instead of a filesystem glob
so build artifacts and local worktrees cannot affect the result.

The two literal examples below are detector fixtures. Their allowlist is deliberately
exact (path, matched bytes, and count), so another occurrence in this file still fails.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
SELF = Path(__file__).resolve().relative_to(ROOT).as_posix()

_UNICODE_FIXTURE = "Settings → Tools".encode("utf-8")
_ASCII_FIXTURE = b"Settings -> Tools"
_REMOVED_ROUTE = re.compile(
    rb"settings"
    rb"(?:[^a-z\r\n]{0,4}|[ \t]*\r?\n[ \t]*)"
    rb"(?:\xe2\x86\x92|->)"
    rb"(?:[^a-z\r\n]{0,4}|[ \t]*\r?\n[ \t]*)"
    rb"tools",
    re.IGNORECASE,
)

_ALLOWED_MATCH_COUNTS = {
    (SELF, _UNICODE_FIXTURE): 1,
    (SELF, _ASCII_FIXTURE): 1,
}


def _tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [
        ROOT / name.decode("utf-8", errors="surrogateescape")
        for name in result.stdout.split(b"\0")
        if name
    ]


def main() -> int:
    # Detector floor: prove both arrow encodings and a line break after the arrow
    # match before trusting a clean repository scan.
    samples = (
        _UNICODE_FIXTURE,
        _ASCII_FIXTURE,
        b"Settings ->" + b"\n        Tools",
    )
    if any(_REMOVED_ROUTE.search(sample) is None for sample in samples):
        print("settings-route ratchet: FAIL (detector does not cover every required form)")
        return 1

    allowed_seen: Counter[tuple[str, bytes]] = Counter()
    violations: list[str] = []

    for path in _tracked_files():
        try:
            data = path.read_bytes()
        except OSError as exc:
            violations.append(f"{path.relative_to(ROOT)}: unreadable ({exc})")
            continue

        relative = path.relative_to(ROOT).as_posix()
        for match in _REMOVED_ROUTE.finditer(data):
            key = (relative, match.group(0))
            if key in _ALLOWED_MATCH_COUNTS and allowed_seen[key] < _ALLOWED_MATCH_COUNTS[key]:
                allowed_seen[key] += 1
                continue

            line = data.count(b"\n", 0, match.start()) + 1
            rendered = match.group(0).decode("utf-8", errors="replace").replace("\n", "\\n")
            violations.append(f"{relative}:{line}: {rendered}")

    for (path, matched), expected in _ALLOWED_MATCH_COUNTS.items():
        actual = allowed_seen[(path, matched)]
        if actual == expected:
            continue
        rendered = matched.decode("utf-8", errors="replace")
        violations.append(
            f"stale detector fixture allowlist: {path} expected {expected} match(es) "
            f"for {rendered!r}, saw {actual}"
        )

    if violations:
        print("settings-route ratchet: FAIL")
        for violation in violations:
            print(f"  {violation}")
        return 1

    print(
        "OK: no removed Settings/Tools route references outside the two explicit detector fixtures"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
