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

An OFFICE DOCUMENT is read as well as tracked: its metadata (custom properties, a sensitivity
label, the creator, last modifier, company and manager, each revision's and comment's author)
sits in compressed XML parts no text rule sees, so the office-document rule opens every one and
refuses it when that metadata names anyone. The two content rules read those parts too. Core's
``publication-hygiene-baseline.json`` holds the same rule as policy.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import subprocess
import sys
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

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
# ⚠️  THE VOCABULARY IS PRIVATE, AND NO FORM OF IT IS PUBLISHED. A list in this repository
#     would publish the very names it keeps out, and a list of digests does too: with the salt
#     beside it, anyone can hash a dictionary of likely names and confirm each entry offline. So
#     the names live in a plain-text file outside every repository, which
#     PERSONALCLAW_PRIVATE_DENYLIST names — the same file, format and matching as core's
#     ``scripts/check_publication_hygiene.py`` (one entry per line: ``word``, ``phrase``,
#     ``host``, ``code`` or ``id``, then its text). Without the variable (CI, a fork, a
#     contributor's clone) this one rule is SKIPPED with a one-line notice and every other rule
#     runs; the maintainer's landing runs it with the list. With it, the rule also refuses an
#     ENCODED entry: an MD5, SHA-1 or SHA-2 digest of one, in hex or base64, a digest salted
#     with a salt the list names (``salt`` lines: the salts a list of these digests was ever
#     published under), or its own base64 or hex — each is as readable as the name to anyone
#     holding a dictionary. A compound kind is only checked in a file that carries its HEAD
#     word. Never write a name, or a digest or encoding of one, into this repo, a commit
#     message, a PR or an issue.

_PRIVATE_DENYLIST_ENV = "PERSONALCLAW_PRIVATE_DENYLIST"
_INTERNAL_SKIPPED = (
    f"publication-hygiene: internal-reference rule skipped: {_PRIVATE_DENYLIST_ENV} is not set "
    "(its vocabulary is private; every other rule ran)"
)
#: The kinds a list line may name, and the kinds the matcher compares (each compound kind has a
#: ``-head`` twin — one word every match must contain — derived from the entries).
_ENTRY_KINDS = ("word", "phrase", "host", "code", "id")
_INTERNAL_KINDS = (
    "word", "phrase-head", "phrase", "host-head", "host", "code-head", "code", "id-head", "id",
)
_DIGITS = "0123456789"
#: A word: a maximal run of letters and digits, case-folded, plus the CamelCase parts of a
#: mixed-case run — so ``snake_case``, ``kebab-case``, ``dotted.names`` and ``FooService``
#: all fold into the words a reader sees.
_WORD = re.compile(r"[a-z0-9]+")
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
#: The digests tried on every spelling of every entry, in hex and in base64.
_DIGEST_ALGORITHMS = ("md5", "sha1", "sha224", "sha256", "sha384", "sha512")
#: A hex run (compared whole, case-folded) and a base64 run (compared whole, padding stripped).
_HEX_RUN = re.compile(r"[0-9A-Fa-f]{8,}")
_BASE64_RUN = re.compile(r"[A-Za-z0-9+/_-]{4,}")

#: A list of INVENTED words, one per kind, read through the same parser as the private list: the
#: detector floor proves every kind fires on it, planted and encoded, and stays green on a near
#: miss, whether or not the private list is set. None of these names anything, and none may ever
#: be replaced by a real name — this file is public. They are the floor's OWN words, distinct
#: from the ones the tests hand the rail: a seeded test repository carries a copy of this file,
#: so a word both used would red the copy on every test run.
_FLOOR_SALT = "zzfloor/publication-hygiene/v0"
_SYNTHETIC_LIST = (
    "word    zzgablewarden\n"
    "phrase  zzfernmoss thrent\n"
    "host    zzcindervale.invalid\n"
    "code    zzf-23\n"
    "id      zzf_Zr7kP2mW9qT4nB\n"
    f"salt    {_FLOOR_SALT}\n"
)
_INTERNAL_CONTROLS = (
    ("word", "per the ZZGABLEWARDEN guidance", "zzgablewardenish"),
    ("phrase", "sign in with zzfernmoss\n#   thrent first", "zzfernmoss unrelated"),
    ("host", "https://docs.zzcindervale.invalid/page", "notzzcindervale.invalid"),
    ("code", "(ZZF-23 boundary)", "zzf-1.6.0"),
    ("id", "cited as zzf_Zr7kP2mW9qT4nB", "zzf_processed1"),
)


class _PrivateDenylistError(Exception):
    """The private denylist is named but cannot be used. Never a skip: a typo in a path or a
    line must not turn "checked" into "silently passed"."""


def _words(text: str) -> set[str]:
    runs = set(_RUN.findall(text))
    words = {run.lower() for run in runs}
    for run in runs:
        tail = run[1:]
        if tail != tail.lower() and not run.isupper():
            words.update(part.lower() for part in _CAMEL_PART.findall(run))
    return words


def _entries(kind: str, text: str) -> list[tuple[str, str]]:
    """The canonical ``(kind, candidate)`` pairs one list line denies, head included. A refusal
    never repeats the text: it is a private entry, and the caller names its line instead."""
    low = text.strip().lower()
    words = _WORD.findall(low)
    if kind == "word":
        if words != [low]:
            raise ValueError("a word is one run of letters and digits")
        return [("word", low)]
    if kind == "phrase":
        if len(words) != 2:
            raise ValueError("a phrase is exactly two words")
        return [("phrase", " ".join(words)), ("phrase-head", words[0])]
    if kind == "host":
        labels = low.split(".")
        if not _HOST_RUN.fullmatch(low) or len(labels) < 2 or not _WORD.findall(labels[-2]):
            raise ValueError("a host is a dotted host name")
        return [("host", low), ("host-head", _WORD.findall(labels[-2])[0])]
    if kind == "code":
        code = _CODE.fullmatch(low)
        if not code:
            raise ValueError("a code is letters, an optional hyphen and 1-3 digits")
        letters, hyphen, digits = code.groups()
        return [("code", f"{letters}{hyphen}{'9' * len(digits)}"), ("code-head", letters)]
    if kind == "id":
        ident = _ID.fullmatch(text.strip())
        if not ident:
            raise ValueError("an id is abc_ and 14 mixed-case or 8 lowercase base62 characters")
        shape = "m14" if ident.group("m14") else "l8"
        return [("id", f"{ident.group('prefix')}_{shape}"), ("id-head", ident.group("prefix"))]
    raise ValueError(f"unknown kind {kind!r} ({', '.join(_ENTRY_KINDS)})")


def _salted(salt: str, kind: str, candidate: str) -> str:
    """The digest a salted denylist made of one candidate: never allowed in the tree."""
    return hashlib.sha256(f"{salt}\0{kind}\0{candidate}".encode("utf-8")).hexdigest()


def _parse_denylist(text: str, source: str) -> dict[str, frozenset[str]]:
    """One entry per line (KIND, whitespace, TEXT; blank and ``#`` lines ignored), folded as the
    matcher folds text; a ``salt`` line is taken as written. A malformed line or a list with no
    entry is refused."""
    denied: dict[str, set[str]] = {kind: set() for kind in (*_INTERNAL_KINDS, "salt")}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        try:
            if len(parts) != 2:
                raise ValueError("a line is KIND, whitespace, then the TEXT to deny")
            pairs = [tuple(parts)] if parts[0] == "salt" else _entries(*parts)
        except ValueError as exc:
            raise _PrivateDenylistError(f"{source}:{number}: {exc}") from None
        for kind, candidate in pairs:
            denied[kind].add(candidate)
    if not any(denied[kind] for kind in _ENTRY_KINDS):
        raise _PrivateDenylistError(f"{source} holds no entry")
    return {kind: frozenset(candidates) for kind, candidates in denied.items()}


def _private_denylist() -> dict[str, frozenset[str]] | None:
    """The list the environment names, or ``None`` when it names none (the rule is then skipped)."""
    value = os.environ.get(_PRIVATE_DENYLIST_ENV, "").strip()
    if not value:
        return None
    path = Path(value).expanduser()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _PrivateDenylistError(f"{path} cannot be read: {exc}") from None
    return _parse_denylist(text, str(path))


def _spellings(candidate: str) -> set[str]:
    """The canonical candidate, its upper, capitalised and title-cased twins, a phrase run
    together or joined by a hyphen or an underscore — each also with ``echo``'s newline."""
    joined = {candidate}
    if " " in candidate:
        joined |= {candidate.replace(" ", sep) for sep in ("", "-", "_")}
    cased = {c for text in joined for c in (text, text.upper(), text.capitalize(), text.title())}
    return cased | {text + "\n" for text in cased}


def _b64(raw: bytes) -> set[str]:
    return {
        base64.b64encode(raw).decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(raw).decode("ascii").rstrip("="),
    }


def _phrase_pattern(heads: list[str]) -> re.Pattern[str]:
    alternation = "|".join(map(re.escape, heads))
    return re.compile(rf"(?<![a-z0-9])({alternation})(?=[^a-z0-9]{{1,{_PHRASE_GAP}}}([a-z0-9]+))")


def _wrapped_phrase(phrase: str) -> re.Pattern[str]:
    head, tail = (re.escape(part) for part in phrase.split(" ", 1))
    return re.compile(rf"(?<![a-z0-9]){head}(?=[^a-z0-9]{{1,{_PHRASE_GAP}}}{tail}(?![a-z0-9]))")


class _InternalMatcher:
    """Finds a denylist's references in text, as written and encoded, with set arithmetic."""

    def __init__(self, denied: dict[str, frozenset[str]]) -> None:
        self._denied = {kind: frozenset(denied.get(kind, ())) for kind in _INTERNAL_KINDS}
        self._hex: dict[str, tuple[str, str]] = {}
        self._b64: dict[str, tuple[str, str]] = {}
        for kind in _INTERNAL_KINDS:
            for candidate in self._denied[kind]:
                for salt in denied.get("salt", ()):
                    self._hex.setdefault(_salted(salt, kind, candidate), ("salted sha256", kind))
                for spelling in _spellings(candidate):
                    raw = spelling.encode("utf-8")
                    self._hex.setdefault(raw.hex(), ("hex", kind))
                    for token in _b64(raw):
                        self._b64.setdefault(token, ("base64", kind))
                    for algorithm in _DIGEST_ALGORITHMS:
                        digest = hashlib.new(algorithm, raw).digest()
                        self._hex.setdefault(digest.hex(), (algorithm, kind))
                        for token in _b64(digest):
                            self._b64.setdefault(token, (f"base64 {algorithm}", kind))

    def _denied_among(self, kind: str, candidates: set[str]) -> set[str]:
        return candidates & self._denied[kind]

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

    def encodings(self, text: str) -> list[tuple[str, str, str]]:
        """Every ``(encoding, kind, token)`` in *text* that is a denied entry encoded."""
        found = set()
        for run in set(_HEX_RUN.findall(text)):
            if run.lower() in self._hex:
                found.add((*self._hex[run.lower()], run))
        for run in set(_BASE64_RUN.findall(text)):
            if run in self._b64:
                found.add((*self._b64[run], run))
        return sorted(found)

    def located_encodings(self, text: str) -> list[tuple[int, str, str, str]]:
        """``(line, encoding, kind, token)`` for each encoded entry; one pass for a clean text."""
        if not self.encodings(text):
            return []
        lines = enumerate(text.splitlines(), 1)
        return sorted({(number, *hit) for number, line in lines for hit in self.encodings(line)})


def _shown(token: str) -> str:
    return token if len(token) <= 20 else token[:20] + "…"


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


# ── office documents ────────────────────────────────────────────────────────────────────
#
# An office document carries more than its text: the software that saved it records who wrote
# it, who last changed it, their company and manager, the name, initials and account id of each
# revision's and comment's author, and whatever properties a document-management tool stamps on
# it, such as a sensitivity label. It all sits in compressed XML parts, so this rule opens every
# tracked office document (recognised by its CONTENT, so a renamed one is read too) and refuses
# one that carries custom properties, a sensitivity label, or a person or organisation in an
# identity field. A format the rule cannot read is refused rather than trusted.

#: Extensions of the office formats. A tracked file carrying one must be readable as an office
#: document or it is refused; extensions other files also use (``.dot``, ``.pot``, ``.key``) are
#: left to the content check.
_OFFICE_EXTENSIONS = frozenset(
    # Office Open XML documents, workbooks, presentations and drawings.
    ".docx .docm .dotx .dotm .xlsx .xlsm .xlsb .xltx .xltm .xlam".split()
    + ".pptx .pptm .potx .potm .ppsx .ppsm .ppam .sldx .sldm .thmx".split()
    + ".vsdx .vsdm .vssx .vssm .vstx .vstm".split()
    # OpenDocument, packaged and flat.
    + ".odt .ott .ods .ots .odp .otp .odg .otg .odf .odc .odb .fodt .fods .fodp .fodg".split()
    # The binary formats before those, rich text, and other word processors' packages.
    + ".doc .xls .xlt .ppt .pps .vsd .rtf .pages .numbers".split()
)
#: The values an identity field may hold, because each names no one. An ALLOWLIST for the reason
#: ``_PLACEHOLDER_HOMES`` is one: a denylist would publish the names it keeps out. The empty
#: string; ``Author``, which Word's remove-personal-information option writes in place of every
#: revision author; ``Unknown``, the first entry of every RTF revision table; and the creator
#: python-docx and openpyxl sign a new file with. python-pptx is deliberately absent: its
#: template records a real person as the last modifier.
_PLACEHOLDER_IDENTITY = frozenset({"", "Author", "Unknown", "openpyxl", "python-docx"})
_ZIP_MAGIC = b"PK\x03\x04"
#: The compound-document container of the binary office formats, of mail items and of embedded
#: objects, whose property sets this rule does not parse.
_COMPOUND_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")
_RTF_MAGIC = b"{\\rtf"
_ODF_MIMETYPE = b"application/vnd.oasis.opendocument."
_ODF_OFFICE_NS = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
_ODF_META_NS = "urn:oasis:names:tc:opendocument:xmlns:meta:1.0"
_OFFICE_XML_SUFFIXES = (".xml", ".rels", ".vml", ".rdf")
#: Caps on what one document can make the rule read: bytes per part, parts per package, and
#: packages nested inside packages. Each one fails CLOSED.
_OFFICE_PART_CAP = 8_000_000
_OFFICE_MAX_PARTS = 2_000
_OFFICE_MAX_DEPTH = 3
_ZIP_ERRORS = (
    zipfile.BadZipFile,
    zipfile.LargeZipFile,
    zlib.error,
    OSError,
    EOFError,
    RuntimeError,
    NotImplementedError,
    ValueError,
)
#: Elements whose TEXT names a person or an organisation (a package's creator, last-modified-by,
#: company and manager, a spreadsheet comment's author, an OpenDocument's creators and
#: printed-by); attributes that name a person wherever they sit (a revision's or comment's author
#: and initials, a signed-in author's account id, a shared workbook's user); and elements that
#: describe a person, whose ``name`` or ``displayName`` is that person's name.
_IDENTITY_ELEMENTS = frozenset(
    {
        "creator",
        "lastModifiedBy",
        "Company",
        "Manager",
        "author",
        "initial-creator",
        "creator-initials",
        "sender-initials",
        "printed-by",
    }
)
_IDENTITY_ATTRIBUTES = frozenset({"author", "initials", "userId", "userName"})
_PERSON_ELEMENTS = frozenset({"cmAuthor", "author", "person", "userInfo"})
_PERSON_NAME_ATTRIBUTES = frozenset({"name", "displayName"})
#: Root namespaces of a part holding CUSTOM properties, and the relationship types that attach
#: custom properties or a sensitivity label to a package.
_CUSTOM_PROPERTY_NAMESPACES = frozenset(
    {
        "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties",
        "http://purl.oclc.org/ooxml/officeDocument/customProperties",
        "http://schemas.microsoft.com/office/2006/metadata/properties",
    }
)
_CUSTOM_PROPERTY_RELATIONSHIPS = ("/custom-properties", "/customProperties")
_LABEL_RELATIONSHIPS = ("/classificationlabels",)
#: A sensitivity label is written as custom properties whose names start with this marker, or
#: into a label part of its own.
_LABEL_MARKER = "msip_label"
_LABEL_PART = "docmetadata/labelinfo.xml"
_RTF_IDENTITY = re.compile(r"\{\\(?:\*\\)?(author|operator|company|manager)(?![a-z])\s?([^{}]*)\}")
_RTF_CUSTOM = re.compile(r"\{\\\*\\userprops(?![a-z])")
_RTF_REVISION_TABLE = re.compile(r"\{\\\*\\revtbl(?![a-z])((?:\s*\{[^{}]*\})*)")
_RTF_REVISION_ENTRY = re.compile(r"\{([^{}]*)\}")
_OFFICE_HOW_TO_FIX = (
    "An office-document finding is fixed in the document: clear the named field (Word's "
    "remove-personal-information option or its document inspector does it on save), delete a "
    "custom-properties or label part together with its content-type override and its package "
    "relationship, or convert a legacy binary file to its XML format. Do not add a value to "
    "_PLACEHOLDER_IDENTITY unless it names no person and no organisation."
)


def _local(name: str) -> str:
    return name.rsplit("}", 1)[-1]


def _namespace(name: str) -> str:
    return name[1:].split("}", 1)[0] if name.startswith("{") else ""


def _office_kind(blob: bytes) -> str | None:
    """``package``, ``flat``, ``rtf`` or ``compound``, judged by CONTENT; ``None`` for anything
    else, a zip that is not an office package included."""
    if blob.startswith(_COMPOUND_MAGIC):
        return "compound"
    if blob.startswith(_RTF_MAGIC):
        return "rtf"
    if blob.startswith(_ZIP_MAGIC):
        try:
            with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                names = set(archive.namelist())
                if "[Content_Types].xml" in names:
                    return "package"
                if "mimetype" in names:
                    with archive.open("mimetype") as fh:
                        if fh.read(len(_ODF_MIMETYPE)) == _ODF_MIMETYPE:
                            return "package"
        except _ZIP_ERRORS:
            return None
        return None
    head = blob[:4096]
    if head.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<") and _ODF_OFFICE_NS.encode() in head:
        try:
            for _, element in ElementTree.iterparse(io.BytesIO(blob), events=("start",)):
                return "flat" if _namespace(element.tag) == _ODF_OFFICE_NS else None
        except ElementTree.ParseError:
            return None
    return None


def _read_part(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes | None:
    """A part's bytes, or ``None`` when it cannot be read within the cap. The size is what the
    read yields, never what the member's header claims."""
    try:
        with archive.open(info) as fh:
            data = fh.read(_OFFICE_PART_CAP + 1)
    except _ZIP_ERRORS:
        return None
    return data if len(data) <= _OFFICE_PART_CAP else None


def _xml_problems(part: str, data: bytes) -> list[str]:
    """One XML part's problems. Fields are judged by NAME, never by searching for a person."""
    where = f" ({part})" if part else ""
    found = []
    if _LABEL_MARKER in data.decode("utf-8", "replace").lower():
        found.append(f"carries a sensitivity label{where}")
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        return [*found, f"has a part that is not well-formed XML{where}"]
    if _namespace(root.tag) in _CUSTOM_PROPERTY_NAMESPACES:
        found.append(f"carries custom properties{where}")
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        tag = _local(element.tag)
        if tag == "user-defined" and _namespace(element.tag) == _ODF_META_NS:
            found.append(f"carries custom properties{where}")
        if tag == "Relationship":
            relationship = element.get("Type", "")
            if relationship.endswith(_CUSTOM_PROPERTY_RELATIONSHIPS):
                found.append(f"attaches custom properties{where}")
            if relationship.endswith(_LABEL_RELATIONSHIPS):
                found.append(f"attaches a sensitivity label{where}")
        text = "".join(element.itertext()).strip()
        if tag in _IDENTITY_ELEMENTS and text not in _PLACEHOLDER_IDENTITY:
            found.append(f"names someone in <{tag}>{where}")
        for key, value in element.attrib.items():
            attribute = _local(key)
            named = attribute in _IDENTITY_ATTRIBUTES or (
                tag in _PERSON_ELEMENTS and attribute in _PERSON_NAME_ATTRIBUTES
            )
            if named and value.strip() not in _PLACEHOLDER_IDENTITY:
                found.append(f"names someone in <{tag} {attribute}>{where}")
    return found


def _package_problems(blob: bytes, depth: int) -> list[str]:
    """The problems in every part of one package, and of every package embedded in it."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
        infos = [info for info in archive.infolist() if not info.is_dir()]
    except _ZIP_ERRORS:
        return ["is not a readable package"]
    if len(infos) > _OFFICE_MAX_PARTS:
        return [f"has more parts than this rule reads ({len(infos)} > {_OFFICE_MAX_PARTS})"]
    found = []
    for info in infos:
        part = info.filename
        data = _read_part(archive, info)
        if data is None:
            found.append(f"has a part this rule cannot read ({part})")
            continue
        if part.lower() == _LABEL_PART:
            found.append(f"carries a sensitivity label ({part})")
        if part.lower().endswith(_OFFICE_XML_SUFFIXES):
            found += _xml_problems(part, data)
        elif data.startswith(_COMPOUND_MAGIC):
            found.append(f"embeds a compound document this rule cannot read ({part})")
        elif data.startswith(_ZIP_MAGIC) and _office_kind(data) == "package":
            if depth >= _OFFICE_MAX_DEPTH:
                found.append(f"nests documents deeper than this rule reads ({part})")
            else:
                found += [f"{problem} inside {part}" for problem in _package_problems(data, depth + 1)]
    return found


def _rtf_problems(text: str) -> list[str]:
    """An RTF document's information group, custom properties and revision table."""
    found = [
        f"names someone in \\{field}"
        for field, value in _RTF_IDENTITY.findall(text)
        if value.strip() not in _PLACEHOLDER_IDENTITY
    ]
    if _RTF_CUSTOM.search(text):
        found.append("carries custom properties (\\userprops)")
    for table in _RTF_REVISION_TABLE.findall(text):
        entries = (entry.strip().rstrip(";").strip() for entry in _RTF_REVISION_ENTRY.findall(table))
        if any(entry not in _PLACEHOLDER_IDENTITY for entry in entries):
            found.append("names someone in its revision table")
    if _LABEL_MARKER in text.lower():
        found.append("carries a sensitivity label")
    return found


def _office_problems(blob: bytes, *, claimed: bool = False) -> list[str]:
    """Why *blob* is unfit to publish as an office document, one phrase each, sorted; empty when
    it is fit, and when it is no office document at all unless *claimed* (its extension names an
    office format). A phrase names the field or part, never its value: the report is published."""
    kind = _office_kind(blob)
    if kind is None:
        found = ["is not readable as the office document its extension names"] if claimed else []
    elif kind == "compound":
        found = ["is a compound document, whose author and custom properties this rule cannot read"]
    elif kind == "rtf":
        found = _rtf_problems(blob.decode("latin-1"))
    elif kind == "flat":
        found = _xml_problems("", blob)
    else:
        found = _package_problems(blob, depth=0)
    return sorted(set(found))


def _office_part_texts(blob: bytes, depth: int = 0) -> list[tuple[str, str]]:
    """``(part, text)`` for every XML part of an office package and of the packages embedded in
    it (as ``<part>!<inner part>``); empty for any other blob."""
    if _office_kind(blob) != "package":
        return []
    archive = zipfile.ZipFile(io.BytesIO(blob))
    texts = []
    for info in [info for info in archive.infolist() if not info.is_dir()][:_OFFICE_MAX_PARTS]:
        data = _read_part(archive, info)
        if data is None:
            continue
        if info.filename.lower().endswith(_OFFICE_XML_SUFFIXES):
            texts.append((info.filename, data.decode("utf-8", "replace")))
        elif depth < _OFFICE_MAX_DEPTH and data.startswith(_ZIP_MAGIC):
            texts += [(f"{info.filename}!{inner}", t) for inner, t in _office_part_texts(data, depth + 1)]
    return texts


def _may_be_office(head: bytes) -> bool:
    if head.startswith((_ZIP_MAGIC, _COMPOUND_MAGIC, _RTF_MAGIC)):
        return True
    stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    return stripped.startswith(b"<") and _ODF_OFFICE_NS.encode() in head


def _office_sample(parts: dict[str, str | bytes]) -> bytes:
    """A small package built at run time for the detector floor: nothing binary is committed."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        for name, data in parts.items():
            archive.writestr(name, data)
    return out.getvalue()


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
    matcher = _InternalMatcher(_parse_denylist(_SYNTHETIC_LIST, "the synthetic list"))
    for kind, planted, near_miss in _INTERNAL_CONTROLS:
        if kind not in {found for found, _ in matcher.references(planted)}:
            failures.append(f"detector floor: the {kind} control no longer fires on {planted!r}")
        if matcher.references(near_miss):
            failures.append(f"detector floor: internal-reference falsely catches {near_miss!r}")
    word = "zzgablewarden".encode("utf-8")
    for encoding, token in (
        ("sha256", hashlib.sha256(word).hexdigest()),
        ("base64", base64.b64encode(word).decode("ascii")),
        ("salted sha256", _salted(_FLOOR_SALT, "word", word.decode("utf-8"))),
    ):
        fired = {(found, kind) for found, kind, _ in matcher.encodings(f"x = '{token}'")}
        if (encoding, "word") not in fired:
            failures.append(f"detector floor: the {encoding} of a denied word no longer fires")
    if matcher.encodings("x = '" + hashlib.sha256(b"zzsomethingelse").hexdigest() + "'"):
        failures.append("detector floor: the encoded check falsely catches an unlisted digest")

    dc = 'xmlns:dc="http://purl.org/dc/elements/1.1/"'
    signed = _office_sample({"docProps/core.xml": f"<p {dc}><dc:creator>python-docx</dc:creator></p>"})
    if _office_problems(signed):
        failures.append("detector floor: office-document falsely catches a package naming no one")
    planted = "zz-planted-person"
    office_samples = {
        "a creator": _office_sample(
            {"docProps/core.xml": f"<p {dc}><dc:creator>{planted}</dc:creator></p>"}
        ),
        "a revision author": _office_sample(
            {"word/document.xml": f'<d xmlns:w="urn:w"><w:ins w:author="{planted}"/></d>'}
        ),
        "custom properties": _office_sample(
            {
                "docProps/custom.xml": '<Properties xmlns="http://schemas.openxmlformats.org/'
                'officeDocument/2006/custom-properties"/>'
            }
        ),
        "a sensitivity label": _office_sample({"docProps/custom.xml": "<p name='MSIP_Label_x'/>"}),
        "a compound document": _COMPOUND_MAGIC + b"\0" * 504,
        "an unreadable claim": b"not a document",
    }
    for label, sample in office_samples.items():
        if not _office_problems(sample, claimed=label == "an unreadable claim"):
            failures.append(f"detector floor: office-document no longer catches {label}")
    if planted in _PLACEHOLDER_IDENTITY:
        failures.append("detector floor: the planted identity was added to the allowlist")

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
    try:
        denied = _private_denylist()
    except _PrivateDenylistError as exc:
        print(f"publication-hygiene: FAIL (the private denylist is unusable: {exc})")
        return 1
    if denied is None:
        print(_INTERNAL_SKIPPED)
    matcher = None if denied is None else _InternalMatcher(denied)

    tracked = _tracked_files()
    for rule, pattern in _PATH_RULES:
        compiled = re.compile(pattern)
        for path in tracked:
            if compiled.search(path):
                violations.append(f"{rule}: {path}")

    if matcher is not None:
        for path in tracked:
            for kind, surface in matcher.references(path):
                violations.append(
                    f"internal-reference: {path} (its PATH) names a denied {kind} ({surface!r})"
                )
            for encoding, kind, token in matcher.encodings(path):
                violations.append(
                    f"internal-reference: {path} (its PATH) carries the {encoding} of a denied {kind} "
                    f"({_shown(token)!r})"
                )

    for path in tracked:
        full = ROOT / path
        if not full.is_file():
            continue
        blob = full.read_bytes()[:_CONTENT_READ_CAP]
        claimed = PurePosixPath(path).suffix.lower() in _OFFICE_EXTENSIONS
        if claimed or _may_be_office(blob[:4096]):
            for problem in _office_problems(full.read_bytes(), claimed=claimed):
                violations.append(f"office-document: {path} {problem}")
        if _is_binary(blob):
            size = full.stat().st_size
            if size > _MAX_BINARY_BYTES:
                violations.append(
                    f"oversized-binary: {path} is {size} bytes (ceiling {_MAX_BINARY_BYTES})"
                )
            # An office document's XML parts are text every reader of it gets: hold them to
            # the content rules below, located as `<path>!<part>`.
            texts = [(f"{path}!{part}", text) for part, text in _office_part_texts(full.read_bytes())]
        else:
            texts = [(path, blob.decode("utf-8", "replace"))]
        for shown, text in texts:
            for name in set(_HOME_PATH.findall(text)):
                if name not in _PLACEHOLDER_HOMES:
                    violations.append(f"real-home-path: {shown} names home directory {name!r}")
            if matcher is None:
                continue
            for line, kind, surface in matcher.located(text):
                violations.append(
                    f"internal-reference: {shown}:{line} names a denied {kind} ({surface!r})"
                )
            for line, encoding, kind, token in matcher.located_encodings(text):
                violations.append(
                    f"internal-reference: {shown}:{line} carries the {encoding} of a denied {kind} "
                    f"({_shown(token)!r})"
                )

    if violations:
        print("publication-hygiene: FAIL")
        for line in sorted(violations):
            print(f"  {line}")
        if any(line.startswith("office-document:") for line in violations):
            print("\n" + _OFFICE_HOW_TO_FIX)
        if any(line.startswith("internal-reference:") for line in violations):
            print(
                "\nAn internal reference is fixed by stating the PRINCIPLE instead of naming its "
                "source (e.g. 'never trust an upload's declared type; cap decoded size'), and by "
                "renaming a fixture that borrows a non-public name; an encoded one (a digest, "
                "base64 or hex of a denied name) is deleted. There is no exemption. A new name "
                f"goes in the private list {_PRIVATE_DENYLIST_ENV} names, never into this repo."
            )
        if any(not line.startswith(("office-document:", "internal-reference:")) for line in violations):
            print(
                "\nDelete the path, or move it somewhere this repo publishes deliberately. Do not "
                "widen the allowlist to make CI green — every entry is reviewed as policy."
            )
        return 1

    checked = ""
    if denied is not None:
        entries = sum(len(denied[kind]) for kind in _ENTRY_KINDS)
        checked = f" ({entries} private internal-reference entries checked)"
    print(f"OK: {len(tracked)} tracked paths, none unfit to publish{checked}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
