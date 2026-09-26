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

import hashlib
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


# ── internal references ─────────────────────────────────────────────────────────────────
#
# A public repository must not name a system that is not public: the name, host, identifier
# or document of an internal tool. Measured when this rule was added: 43 references on 28
# lines in 17 files, an internal guidance system cited by name, document id and numbered
# control id, and one bundle describing its CLI with an internal characterisation, an
# internal install path and an internal sign-in tool. Each was rewritten to the principle
# it stood for.
#
# ⚠️  THE DENYLIST IS SALTED SHA-256 DIGESTS, NEVER PLAINTEXT. A plaintext list would publish
#     the very names it keeps out, the self-defeat the home-path rule avoids by being an
#     allowlist. The text is folded into candidates (words, two-word phrases, host suffixes,
#     digit-shaped codes, id shapes), each distinct candidate is digested once, and digests are
#     compared. A compound kind is only checked in a file that carries one of its HEAD words.
#     These are the same salt and entries as core's ``publication-hygiene-baseline.json``
#     (``internal_reference_rule``): add a name there with
#     ``python3 scripts/check_publication_hygiene.py --digest KIND TEXT`` and copy the printed
#     lines here. Never write the plaintext into this repo, a commit message, a PR or an issue.

_INTERNAL_SALT = "personalclaw/publication-hygiene/internal-reference/v1"
_INTERNAL_DENIED: dict[str, frozenset[str]] = {
    "word": frozenset(),
    "phrase-head": frozenset(),
    "phrase": frozenset(),
    "host-head": frozenset(),
    "host": frozenset(),
    "code-head": frozenset(),
    "code": frozenset(),
    "id-head": frozenset(),
    "id": frozenset(),
}
_DIGITS = "0123456789"
#: A word: a maximal run of letters and digits, case-folded, plus the CamelCase parts of a
#: mixed-case run — so ``snake_case``, ``kebab-case``, ``dotted.names`` and ``FooService``
#: all fold into the words a reader sees.
_RUN = re.compile(r"[A-Za-z0-9]+")
_CAMEL_PART = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")
#: A dotted, host-like run, anchored at a label start. Every suffix of two or more labels is a
#: candidate, so a denied host also denies each of its subdomains.
_HOST_RUN = re.compile(r"(?<![a-z0-9-])[a-z0-9-]+(?:\.[a-z0-9-]+)+")
#: A catalogue code (letters, optional hyphen, 1-3 digits; not a ``pkg-1.6.0`` version),
#: digits folded to 9s so one entry covers a numbered family.
_CODE = re.compile(r"(?<![a-z0-9])([a-z]{2,6})(-?)([0-9]{1,3})(?![a-z0-9]|\.[0-9])")
#: A document-store id: a three-letter prefix, ``_``, then 14 mixed-case base62 characters or
#: 8 lowercase-and-digit ones. Folded to prefix and shape. Matched case-sensitively.
_ID = re.compile(
    r"(?<![A-Za-z0-9])(?P<prefix>[a-z]{3})_(?:"
    r"(?P<m14>(?=[a-z0-9]*[A-Z])(?=[A-Za-z]*[0-9])(?=[A-Z0-9]*[a-z])[A-Za-z0-9]{14})"
    r"|(?P<l8>(?=[a-z]*[0-9])(?=[0-9]*[a-z])[a-z0-9]{8})"
    r")(?![A-Za-z0-9])"
)
#: How far apart two words may sit and still read as one phrase (a wrapped comment included).
_PHRASE_GAP = 12

#: Each kind's CONTROL (whose digest ``_INTERNAL_DENIED`` carries) planted in ordinary text, and
#: a near miss that must stay green. Assembled at runtime for the reason ``_UNLISTED_OWNER`` is:
#: this script is inside its own input set, so a literal control would make it a published
#: match. Each piece is split so that no fragment is itself a candidate.
_INTERNAL_CONTROLS = (
    ("word", "per the " + "ZZ" + "HYGIENECONTROL" + "ZZ" + " guidance", "zz" + "hygienecontrol" + "zzish"),
    (
        "phrase",
        "sign in with " + "zzcontrol" + "head\n#   " + "zzcontrol" + "tail first",
        "zzcontrol" + "head unrelated",
    ),
    ("host", "https://docs." + "zzcontrol" + ".invalid/page", "not" + "zzcontrol" + ".invalid"),
    ("code", "(" + "ZZQ" + "-42 boundary)", "zzq" + "-1.6.0"),
    ("id", "cited as " + "zzq" + "_" + "Ab3dE5gH7jK9mN", "zzq" + "_processed1"),
)


def _internal_digest(kind: str, candidate: str) -> str:
    return hashlib.sha256(f"{_INTERNAL_SALT}\0{kind}\0{candidate}".encode("utf-8")).hexdigest()


def _words(text: str) -> set[str]:
    runs = set(_RUN.findall(text))
    words = {run.lower() for run in runs}
    for run in runs:
        tail = run[1:]
        if tail != tail.lower() and not run.isupper():
            words.update(part.lower() for part in _CAMEL_PART.findall(run))
    return words


def _phrase_pattern(heads: list[str]) -> re.Pattern[str]:
    alternation = "|".join(map(re.escape, heads))
    return re.compile(rf"(?<![a-z0-9])({alternation})(?=[^a-z0-9]{{1,{_PHRASE_GAP}}}([a-z0-9]+))")


def _wrapped_phrase(phrase: str) -> re.Pattern[str]:
    head, tail = (re.escape(part) for part in phrase.split(" ", 1))
    return re.compile(rf"(?<![a-z0-9]){head}(?=[^a-z0-9]{{1,{_PHRASE_GAP}}}{tail}(?![a-z0-9]))")


class _InternalMatcher:
    """Finds denylisted references while knowing the denylist only as digests; each distinct
    candidate is digested once per run and then checked with set arithmetic."""

    def __init__(self) -> None:
        self._seen: dict[str, set[str]] = {kind: set() for kind in _INTERNAL_DENIED}
        self._hits: dict[str, set[str]] = {kind: set() for kind in _INTERNAL_DENIED}

    def _denied_among(self, kind: str, candidates: set[str]) -> set[str]:
        seen = self._seen[kind]
        new = candidates - seen
        if new:
            denied = _INTERNAL_DENIED[kind]
            self._hits[kind].update(c for c in new if _internal_digest(kind, c) in denied)
            seen |= new
        return candidates & self._hits[kind]

    def references(self, text: str) -> list[tuple[str, str]]:
        """Every ``(kind, surface)`` denied reference in *text*, sorted, once each."""
        low = text.lower()
        words = _words(text)
        found = {("word", w) for w in self._denied_among("word", words)}
        heads = self._denied_among("phrase-head", words)
        if heads:
            phrases = {f"{m.group(1)} {m.group(2)}" for m in _phrase_pattern(sorted(heads)).finditer(low)}
            found.update(("phrase", p) for p in self._denied_among("phrase", phrases))
        if self._denied_among("host-head", words):
            for run in set(_HOST_RUN.findall(low)):
                labels = run.split(".")
                suffixes = {".".join(labels[start:]) for start in range(len(labels) - 1)}
                denied = self._denied_among("host", suffixes)
                if denied:
                    found.add(("host", max(denied, key=len)))
        letters = {w.rstrip(_DIGITS) for w in words if w[-1] in _DIGITS}
        if self._denied_among("code-head", words | letters):
            codes = {(f"{a}{h}{'9' * len(d)}", f"{a}{h}{d}") for a, h, d in set(_CODE.findall(low))}
            denied = self._denied_among("code", {shape for shape, _ in codes})
            found.update(("code", surface) for shape, surface in codes if shape in denied)
        if self._denied_among("id-head", words):
            ids = {
                (f"{m.group('prefix')}_{'m14' if m.group('m14') else 'l8'}", m.group(0))
                for m in _ID.finditer(text)
            }
            denied = self._denied_among("id", {shape for shape, _ in ids})
            found.update(("id", surface) for shape, surface in ids if shape in denied)
        return sorted(found)

    def located(self, text: str) -> list[tuple[int, str, str]]:
        """``(line, kind, surface)`` for each reference; a phrase wrapped across a line break is
        placed on its first word's line."""
        hits = self.references(text)
        if not hits:
            return []
        located = {
            (number, kind, surface)
            for number, line in enumerate(text.splitlines(), 1)
            for kind, surface in self.references(line)
        }
        placed = {(kind, surface) for _, kind, surface in located}
        low = text.lower()
        for kind, surface in hits:
            if kind == "phrase" and (kind, surface) not in placed:
                for match in _wrapped_phrase(surface).finditer(low):
                    located.add((low.count("\n", 0, match.start()) + 1, kind, surface))
        return sorted(located)


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
    matcher = _InternalMatcher()
    for kind, planted, near_miss in _INTERNAL_CONTROLS:
        if kind not in {found for found, _ in matcher.references(planted)}:
            failures.append(f"detector floor: the {kind} control no longer fires on {planted!r}")
        if matcher.references(near_miss):
            failures.append(f"detector floor: internal-reference falsely catches {near_miss!r}")
    for kind, entries in _INTERNAL_DENIED.items():
        if len(entries) < 2:
            failures.append(f"detector floor: the {kind} denylist holds nothing but its control")

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

    matcher = _InternalMatcher()
    for path in tracked:
        for kind, surface in matcher.references(path):
            violations.append(f"internal-reference: {path} (its PATH) names a denied {kind} ({surface!r})")

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
        text = blob.decode("utf-8", "replace")
        for name in set(_HOME_PATH.findall(text)):
            if name not in _PLACEHOLDER_HOMES:
                violations.append(f"real-home-path: {path} names home directory {name!r}")
        for line, kind, surface in matcher.located(text):
            violations.append(f"internal-reference: {path}:{line} names a denied {kind} ({surface!r})")

    if violations:
        print("publication-hygiene: FAIL")
        for line in sorted(violations):
            print(f"  {line}")
        if any(line.startswith("internal-reference:") for line in violations):
            print(
                "\nAn internal reference is fixed by stating the PRINCIPLE instead of naming its "
                "source (e.g. 'never trust an upload's declared type; cap decoded size'), and by "
                "renaming a fixture that borrows a non-public name. There is no exemption."
            )
        print(
            "\nDelete the path, or move it somewhere this repo publishes deliberately. Do not "
            "widen the allowlist to make CI green — every entry is reviewed as policy."
        )
        return 1

    print(f"OK: {len(tracked)} tracked paths, none unfit to publish")
    return 0


if __name__ == "__main__":
    sys.exit(main())
