#!/usr/bin/env python3
"""Fail when a tracked path is not fit to publish from a PUBLIC repository.

This is a repository-wide fact, so it cannot live in one bundle's test suite: CI runs
pytest separately per bundle, and the defects this catches are precisely the ones that
belong to no bundle. Scan Git's tracked-file list rather than a filesystem glob, so build
artifacts and local worktrees cannot affect the result.

``git ls-files`` is exactly what a clone receives, and ``git rm`` takes a path out of the
tree but never out of history — so the only cheap moment to refuse an unfit path is the PR
that adds it.

What this rail was built from, measured on ``origin/main``:

* ``docs/plans/OPENROUTER-MODELS.md`` — a 1,389-line internal implementation plan carrying
  **five hardcoded ``/Users/<maintainer>/...`` absolute paths**. Its own header read
  "Status: PLAN ONLY — no production code written yet" while ``openrouter-models/`` shipped
  as a complete bundle, so it was stale, false, unreferenced, and a leak of the author's
  machine layout at once. Removed; the core repo had already made the same call when it
  stopped publishing its internal roadmap tree.
* ``.worktrees/`` and ``.local/`` absent from ``.gitignore`` — a plain ``git add -A`` stages
  every linked worktree as an embedded git repository (a gitlink pointing at a local
  absolute path). Reproduced here before the fix by simply creating a worktree.

⚠️  THERE ARE NO EXEMPTIONS — not even for this file. The first version of this rail wrote
    its control home path as a LITERAL, which made the script itself a published real-home
    path, and then bought itself a blanket `(SELF, rule)` exemption to stay green. Six of
    those seven entries were dead weight (this path matches none of the six path rules) and
    the seventh existed only to hide the literal. Both are gone: the control name is now
    assembled at runtime, so nothing needs exempting. A rail that exempts its own file is
    precisely the shape that lets a real defect hide later, because the exempted file is the
    one nobody re-reads.

⚠️  AND THE DETECTOR FLOOR RUNS FIRST. A rail that matched nothing would print OK forever, so
    every pattern is proved against a sample it must catch AND one it must spare before a
    clean repository scan is believed — including a re-check of this script's own source.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SELF = Path(__file__).resolve().relative_to(ROOT).as_posix()

#: Read cap for the content rule. The longest real-home path worth seeing is a few hundred
#: bytes; reading whole multi-megabyte files to find one makes the rail slow enough to be
#: switched off.
_CONTENT_READ_CAP = 2_000_000

#: A tracked binary is permanent, unshrinkable weight in every clone (no Git LFS here).
#: Measured ceiling: the largest tracked binary in this repo is 40 KB. 512 KB leaves ample
#: room for a legitimate app icon while still refusing a committed screenshot dump.
_MAX_BINARY_BYTES = 512 * 1024

#: Path shapes that must never be published. Each is anchored on a full path SEGMENT so an
#: ordinary name that merely starts with a residue word stays green (`template.py` is not
#: `temp/`); `test_the_detector_floor` below pins both directions.
_PATH_RULES: tuple[tuple[str, str], ...] = (
    (
        "residue-name",
        r"(?:^|/)(?:temp|tmp|scratch|wip|old|bak|debug)(?:[-_.][^/]*)?(?:/|$)",
    ),
    ("residue-suffix", r"(?:\.(?:orig|rej|bak|tmp|swp|swo)|~)$"),
    (
        "build-output-or-cache",
        r"(?:^|/)(?:dist|build|node_modules|__pycache__|htmlcov|coverage|reports|"
        r"\.pytest_cache|\.mypy_cache|\.hypothesis|\.cache)(?:/|$)",
    ),
    (
        "embedded-repo-or-editor-state",
        r"(?:^|/)(?:\.worktrees|\.local|\.claude|\.idea|\.vscode)(?:/|$)",
    ),
    (
        "dev-home-or-secret-material",
        r"(?:^|/)(?:\.dev-home[^/]*|\.secrets\.env|\.local_secret|session_key|"
        r"sel_hmac\.key)(?:/|$)",
    ),
    ("dotenv-with-real-values", r"(?:^|/)\.env(?:\.local)?$"),
)

#: An absolute path into a home directory whose owner is not a recognised placeholder
#: publishes a real username and machine layout. This is an ALLOWLIST of placeholder names,
#: not a denylist of real ones, for two reasons: a denylist would have to write the
#: maintainer's username INTO the public repo to keep it out, and an allowlist reds an
#: unrecognised name the FIRST time it appears. The set below is the complete measured set
#: across the tracked tree — every entry a synthetic fixture. Adding a name here is a claim
#: that it is a placeholder, not a person.
_HOME_PATH = re.compile(r"/(?:Users|home)/([A-Za-z0-9._-]+)")
_PLACEHOLDER_HOMES = frozenset(
    {"alice", "alice.linux", "bob", "dev", "me", "runner", "someone", "u", "user", "you"}
)

#: A home-directory owner deliberately absent from the allowlist above, so the detector floor
#: proves the rule fires on a genuine-looking home path.
#:
#: ⚠️  ASSEMBLED AT RUNTIME, AND THAT IS THE WHOLE POINT. This script is itself a tracked file
#:     inside its own input set, so writing the name as a literal `/Users/<name>` would make
#:     this file a published real-home path — and the rule would be RIGHT to flag it. The
#:     first version of this rail carried that literal and bought itself a blanket
#:     `(SELF, rule)` exemption to stay green. That exemption is gone: six of its seven
#:     entries were dead weight (this path matches none of the six path rules), and the
#:     seventh only existed to hide this string. Interpolating instead means the bytes that
#:     reach the tree are `/Users/{` — and `{` is outside the owner class `[A-Za-z0-9._-]`,
#:     so the exemption is UNNECESSARY rather than merely unused.
#:
#:     THERE ARE NOW NO EXEMPTIONS AT ALL. A rail that exempts its own file is exactly the
#:     shape that lets a real defect hide later, because the exempted file is the one nobody
#:     re-reads. `_detector_floor` re-checks this script's own source on every run.
_UNLISTED_OWNER = "zz" + "notaplaceholder" + "zz"


def _tracked_files() -> list[str]:
    """Every tracked path as a repo-relative POSIX string. ``-z`` because a path may
    legitimately contain a space and line-splitting would silently drop its tail."""
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [
        name.decode("utf-8", errors="surrogateescape")
        for name in result.stdout.split(b"\0")
        if name
    ]


def _is_binary(blob: bytes) -> bool:
    """A NUL byte in the first 8 KB — the heuristic ``git diff`` uses to decide a file has
    no textual diff, and the right definition for the size rule's text exemption."""
    return b"\0" in blob[:8192]


def _detector_floor() -> list[str]:
    """Prove every pattern catches what it exists for, and spares what it must, BEFORE a
    clean scan is believed. A rail that matches nothing prints OK for the rest of its life."""
    must_match = {
        "residue-name": ("temp-screenshots/before.png", "scratch/notes.md", "docs/old.md"),
        "residue-suffix": ("provider.py.orig", "app.json.rej", "notes.md~"),
        "build-output-or-cache": ("slack-channel/ui/dist/index.js", "node_modules/x/i.js"),
        "embedded-repo-or-editor-state": (".worktrees/lane-x/README.md", ".local/state/gh/id"),
        "dev-home-or-secret-material": (".dev-home/.local_secret", ".secrets.env"),
        "dotenv-with-real-values": (".env", "slack-channel/.env.local"),
    }
    must_not_match = (
        "openrouter-models/template.py",
        "growth/bakeoff.py",
        ".env.example",
        "docs/oldest-first.md",
        "ops/tempo.py",
    )
    failures = []
    rules = dict(_PATH_RULES)
    for rule, samples in must_match.items():
        for sample in samples:
            if not re.search(rules[rule], sample):
                failures.append(f"detector floor: {rule} no longer catches {sample!r}")
    for sample in must_not_match:
        for rule, pattern in _PATH_RULES:
            if re.search(pattern, sample):
                failures.append(f"detector floor: {rule} falsely catches {sample!r}")
    if _HOME_PATH.findall(f"HOME = '/Users/{_UNLISTED_OWNER}'") != [_UNLISTED_OWNER]:
        failures.append("detector floor: the home-path pattern no longer extracts the owner")
    if _UNLISTED_OWNER in _PLACEHOLDER_HOMES:
        failures.append("detector floor: the control owner was added to the allowlist")
    if _HOME_PATH.findall("HOME = '/Users/me'") != ["me"] or "me" not in _PLACEHOLDER_HOMES:
        failures.append("detector floor: '/Users/me' is not treated as a placeholder")

    # This script polices itself — there are no exemptions. A literal `/Users/<name>` here
    # would make this file a published real-home path, so the guard is on the MECHANISM (an
    # inlined literal) rather than on waiting for the scan below to notice.
    own = Path(__file__).read_text(encoding="utf-8")
    inlined = sorted(o for o in set(_HOME_PATH.findall(own)) if o not in _PLACEHOLDER_HOMES)
    if inlined:
        failures.append(
            f"detector floor: {SELF} inlines {inlined} as a matchable home path — "
            f"interpolate _UNLISTED_OWNER instead of writing the name as a literal"
        )
    return failures


def main() -> int:
    """Print the report; exit ``0`` iff every tracked path is fit to publish."""
    violations = _detector_floor()
    if violations:
        print("publication-hygiene: FAIL (detector floor)")
        for line in violations:
            print(f"  {line}")
        return 1

    tracked = _tracked_files()
    for rule, pattern in _PATH_RULES:
        compiled = re.compile(pattern)
        for path in tracked:
            if compiled.search(path):
                violations.append(f"{rule}: {path}")

    for path in tracked:
        full = ROOT / path
        if not full.is_file():
            continue
        blob = full.read_bytes()[:_CONTENT_READ_CAP]
        if _is_binary(blob):
            size = full.stat().st_size
            if size > _MAX_BINARY_BYTES:
                violations.append(
                    f"oversized-binary: {path} is {size} bytes (ceiling {_MAX_BINARY_BYTES})"
                )
            continue
        for name in set(_HOME_PATH.findall(blob.decode("utf-8", "replace"))):
            if name not in _PLACEHOLDER_HOMES:
                violations.append(f"real-home-path: {path} names home directory {name!r}")

    if violations:
        print("publication-hygiene: FAIL")
        for line in sorted(violations):
            print(f"  {line}")
        print(
            "\nDelete the path, or move it somewhere this repo publishes deliberately. Do not "
            "widen the allowlist to make CI green — every entry is reviewed as policy."
        )
        return 1

    print(f"OK: {len(tracked)} tracked paths, none unfit to publish")
    return 0


if __name__ == "__main__":
    sys.exit(main())
