"""The review pipeline — pure functions over a unified diff, plus the local findings log.

Deliberately free of I/O beyond the findings file: everything here is testable from a
fixture diff, with no ``gh`` call, no network, and no model. ``provider.py`` owns the two
impure edges (the ``gh`` subprocess and the per-file model fan-out) and calls into this.

Three things live here:

1. ``parse_pr_ref`` / ``parse_diff`` — turn a PR reference and a unified diff into
   ``ChangedFile`` records.
2. ``blast_radius`` — the weight that orders and budgets the fan-out.
3. ``static_findings`` + ``FindingsLog`` — the deterministic finding pass and the
   append-only JSONL the findings are kept in, on this machine, forever.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from personalclaw.sdk.util import app_data_dir

APP_NAME = "code-review"

# ── PR reference ────────────────────────────────────────────────────────────
#
# One shape in, validated before it is ever handed to `gh` (validate at the boundary, not
# at the point of use). GitHub's own rules: owner/repo are
# alphanumerics + `.`/`-`/`_`, the number is digits. Anything else is refused with a
# message rather than passed through — the reference becomes argv AND a filename, so a
# loose regex here would be both a command-argument and a path-traversal hole.
_OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9_-])?"
_REPO = r"[A-Za-z0-9._-]{1,100}"
_PR_RE = re.compile(rf"^(?:https?://github\.com/)?(?P<owner>{_OWNER})/(?P<repo>{_REPO})"
                    rf"(?:#|/pull/)(?P<number>\d{{1,12}})/?$")


@dataclass(frozen=True)
class PrRef:
    """A validated PR coordinate. ``slug`` is the only thing that reaches the filesystem."""

    owner: str
    repo: str
    number: int

    @property
    def slug(self) -> str:
        return f"{self.owner}__{self.repo}__{self.number}"

    def __str__(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"


def parse_pr_ref(raw: str) -> PrRef:
    """Parse ``owner/repo#123`` or a ``github.com/owner/repo/pull/123`` URL.

    Raises ``ValueError`` on anything else. The strictness is the point: this value
    becomes both a ``gh`` argument and a path component.
    """
    m = _PR_RE.match((raw or "").strip())
    if not m:
        raise ValueError(
            f"not a PR reference: {raw!r} — use 'owner/repo#123' or "
            "'https://github.com/owner/repo/pull/123'"
        )
    return PrRef(m["owner"], m["repo"], int(m["number"]))


# ── Diff parsing ────────────────────────────────────────────────────────────

_DIFF_HEADER = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$")
# Git quotes a path holding a non-ASCII byte (``core.quotepath``, on by default), a `"`, a
# backslash or a control character: ``"a/caf\303\251.py"``, C escapes and octal bytes.
_QUOTED_PATH = r'"(?:[^"\\]|\\.)*"'
_QUOTED_HEADER = re.compile(
    rf'^diff --git (?P<a>{_QUOTED_PATH}|a/\S+) (?P<b>{_QUOTED_PATH}|b/\S+)$'
)
_C_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}
_BINARY = re.compile(r"^(GIT binary patch|Binary files .* differ)")
# A hunk header's line counts; a count left out is 1 (``@@ -3 +3 @@``).
_HUNK = re.compile(r"^@@ -\d+(?:,(?P<old>\d+))? \+\d+(?:,(?P<new>\d+))? @@")

# Paths whose contents carry no reviewable semantics — generated, vendored or locked.
# Matched on the whole path, case-insensitively.
_GENERATED = re.compile(
    r"(^|/)(node_modules|vendor|third_party|dist|build|\.min\.)|"
    r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|uv\.lock|poetry\.lock|"
    r"Cargo\.lock|go\.sum|composer\.lock)$",
    re.IGNORECASE,
)


def _unquote(token: str) -> str:
    """One path as git wrote it, quoted or not, without its ``a/`` or ``b/``."""
    if token.startswith('"'):
        raw = bytearray()
        body = token[1:-1]
        i = 0
        while i < len(body):
            ch = body[i]
            if ch != "\\":
                raw += ch.encode("utf-8")
                i += 1
            elif body[i + 1 : i + 4].isdigit() and len(body[i + 1 : i + 4]) == 3:
                raw.append(int(body[i + 1 : i + 4], 8) & 0xFF)
                i += 4
            else:
                raw.append(_C_ESCAPES.get(body[i + 1 : i + 2], ord("?")))
                i += 2
        token = raw.decode("utf-8", "replace")
    return token[2:]


def _header_paths(line: str) -> tuple[str, str] | None:
    """The (old, new) paths a ``diff --git`` line names, or ``None`` when it can't be read."""
    if '"' in line:
        quoted = _QUOTED_HEADER.match(line)
        if quoted:
            return _unquote(quoted["a"]), _unquote(quoted["b"])
        return None
    plain = _DIFF_HEADER.match(line)
    return (plain["a"], plain["b"]) if plain else None


@dataclass
class ChangedFile:
    """One file in the PR, with just enough of the diff to review it in isolation."""

    path: str
    old_path: str = ""
    status: str = "modified"  # added | modified | deleted | renamed
    added: int = 0
    removed: int = 0
    binary: bool = False
    generated: bool = False
    # Its `diff --git` line could not be read, so ``path`` is that line's own text.
    unreadable: bool = False
    hunks: list[str] = field(default_factory=list)
    # Filled in by blast_radius().
    weight: int = 0
    depth: str = "normal"  # deep | normal | skim
    weight_because: list[str] = field(default_factory=list)

    @property
    def churn(self) -> int:
        return self.added + self.removed

    @property
    def added_lines(self) -> list[str]:
        """Just the added lines, `+` stripped — what a finding cites as evidence."""
        out: list[str] = []
        for hunk in self.hunks:
            for line in hunk.splitlines():
                if line.startswith("+") and not line.startswith("+++"):
                    out.append(line[1:])
        return out

    def diff_text(self) -> str:
        return "\n".join(self.hunks)


def parse_diff(diff: str) -> list[ChangedFile]:
    """Split a unified diff into one ``ChangedFile`` per changed path.

    A hunk takes exactly the lines its header counts, so nothing after it is read as the
    file's change: in a series of patches (``git format-patch``, ``gh pr diff --patch``) the
    next commit's mail headers, message and diffstat sit between two files, and a message's
    ``- `` bullets would otherwise count as removed lines. A path changed in more than one
    stanza is one record, its hunks and counts together, so no file is weighed or reviewed
    twice.

    Tolerant by design: a stanza it cannot classify still yields a file record with its
    hunks, because a file silently dropped from the fan-out is the one failure mode that
    would make this app lie about coverage.
    """
    files: list[ChangedFile] = []
    by_path: dict[str, ChangedFile] = {}
    cur: ChangedFile | None = None
    old_left = new_left = 0  # lines the open hunk still holds, on each side
    stanza_hunks = 0  # hunks this `diff --git` stanza has opened
    for line in (diff or "").splitlines():
        if line.startswith("diff --git "):
            old_left = new_left = stanza_hunks = 0
            paths = _header_paths(line)
            # A header this can't read still opens a record, named by its own text and
            # never counted as reviewed, so its hunks can't land on the file before it.
            old, new = paths or ("", line[len("diff --git "):])
            cur = by_path.get(new)
            if cur is None:
                cur = ChangedFile(path=new, old_path=old, unreadable=paths is None)
                cur.generated = bool(_GENERATED.search(cur.path))
                by_path[cur.path] = cur
                files.append(cur)
            continue
        if cur is None:
            continue
        if old_left or new_left:
            cur.hunks[-1] += "\n" + line
            if line.startswith("+"):
                cur.added += 1
                new_left -= 1
            elif line.startswith("-"):
                cur.removed += 1
                old_left -= 1
            elif not line.startswith("\\"):  # context: on both sides
                old_left -= 1
                new_left -= 1
            old_left, new_left = max(0, old_left), max(0, new_left)
            continue
        hunk = _HUNK.match(line)
        if hunk:
            cur.hunks.append(line)
            stanza_hunks += 1
            old_left = int(hunk["old"] if hunk["old"] is not None else 1)
            new_left = int(hunk["new"] if hunk["new"] is not None else 1)
        elif line.startswith("\\") and stanza_hunks:
            cur.hunks[-1] += "\n" + line  # "\ No newline at end of file", after a hunk
        elif stanza_hunks:
            continue  # past the stanza's last hunk: not the file's change
        elif line.startswith("new file mode"):
            cur.status = "added"
        elif line.startswith("deleted file mode"):
            cur.status = "deleted"
        elif line.startswith("rename from"):
            cur.status = "renamed"
        elif _BINARY.match(line):
            cur.binary = True
    return files


# ── Blast radius ────────────────────────────────────────────────────────────
#
# Four cheap signals, no repo checkout, no language server. The whole point of a WEIGHT
# is that the fan-out spends its budget where a mistake propagates furthest — so every
# component below answers "how far does a bug here reach?", not "how big is the change?".

# Path fragments that raise the ceiling: the change reaches other code, other users, or
# the security posture. Deliberately short — a long list is a long list of guesses.
_CRITICAL_PATH = (
    ("auth", 22), ("crypt", 22), ("secret", 22), ("credential", 22), ("permission", 20),
    ("security", 20), ("token", 16), ("session", 14), ("payment", 22), ("billing", 18),
    ("migration", 18), ("schema", 14), ("config", 10), ("__init__.py", 10),
)
# Path fragments that lower it: the blast radius genuinely stops at the file.
_PERIPHERAL_PATH = (
    ("docs/", -22), (".md", -18), ("readme", -18), ("changelog", -20), ("license", -24),
    ("example", -14), ("fixture", -16), ("/test", -10), ("test_", -10), ("_test.", -10),
    ("spec.", -8), (".txt", -12), (".lock", -26),
)

# How a module is named in an import statement, per language family we can cheaply match.
_PY = re.compile(r"^(?:from|import)\s+([\w.]+)", re.MULTILINE)
_JS = re.compile(r"""(?:from\s+|require\()\s*['"]([^'"]+)['"]""")


def _module_names(path: str) -> set[str]:
    """The tokens an importer of ``path`` would write. Language-agnostic and generous."""
    p = path.rsplit("/", 1)[-1]
    stem = p.rsplit(".", 1)[0]
    names = {stem}
    if stem == "index" or stem == "__init__":
        parent = path.rsplit("/", 2)[-2] if "/" in path else ""
        if parent:
            names.add(parent)
    # dotted python path, best effort: strip a leading src/ and the extension
    dotted = re.sub(r"^(?:src|lib)/", "", path)
    dotted = re.sub(r"\.(py|ts|tsx|js|jsx|mjs)$", "", dotted)
    names.add(dotted.replace("/", "."))
    return {n for n in names if n and n not in {"index", "__init__", "mod", "main"}}


def _fan_in(target: ChangedFile, others: list[ChangedFile]) -> int:
    """How many OTHER changed files import ``target``.

    Scoped to the changed set on purpose — the app never clones the repo, so this is
    fan-in *within the PR*, which is a floor on real fan-in, not an estimate of it. Named
    honestly in ``weight_because`` so a reader is not misled about what was measured.
    """
    names = _module_names(target.path)
    if not names:
        return 0
    hits = 0
    for other in others:
        if other.path == target.path:
            continue
        body = other.diff_text()
        imported = set(_PY.findall(body)) | set(_JS.findall(body))
        flat = {tok.split(".")[-1].rsplit("/", 1)[-1] for tok in imported} | imported
        if names & flat:
            hits += 1
    return hits


def blast_radius(files: list[ChangedFile]) -> list[ChangedFile]:
    """Score every file 0..100, set ``depth``, and return them heaviest-first.

    Mutates and returns the same objects (the caller wants the annotated records, not a
    parallel table that can drift out of step with them).
    """
    for f in files:
        why: list[str] = []
        # 1. Churn, log-scaled: 40 lines and 4000 lines are both "big", and a linear term
        #    would let one vendored blob eat the whole budget.
        churn = min(35, int(12 * math.log10(1 + f.churn)))
        if churn:
            why.append(f"churn {f.churn} lines (+{churn})")
        score = 30 + churn

        # 2. Path criticality.
        low = f.path.lower()
        for frag, delta in _CRITICAL_PATH:
            if frag in low:
                score += delta
                why.append(f"path names {frag!r} ({delta:+d})")
                break
        for frag, delta in _PERIPHERAL_PATH:
            if frag in low:
                score += delta
                why.append(f"peripheral path {frag!r} ({delta:+d})")
                break

        # 3. Fan-in within the changed set.
        fan = _fan_in(f, files)
        if fan:
            score += min(20, 7 * fan)
            why.append(f"imported by {fan} other changed file(s) (+{min(20, 7 * fan)})")

        # 4. Status. A deletion of something others import is the highest-reach change in
        #    a PR and the easiest to wave through; an addition reaches nothing yet.
        if f.status == "deleted":
            score += 12 + (8 if fan else 0)
            why.append("deleted (+12)")
        elif f.status == "added":
            score -= 6
            why.append("newly added, nothing depends on it yet (-6)")
        elif f.status == "renamed":
            score += 4
            why.append("renamed (+4)")

        # 5. Nothing to review.
        if f.binary or f.generated:
            score = min(score, 5)
            why.append("binary/generated — no reviewable semantics (capped at 5)")

        f.weight = max(0, min(100, score))
        f.weight_because = why
        f.depth = "deep" if f.weight >= 65 else ("skim" if f.weight < 25 else "normal")
    files.sort(key=lambda f: (-f.weight, f.path))
    return files


# ── Per-file review briefs (the fan-out unit) ───────────────────────────────

# Chars of diff each depth is allowed to carry into its own review. The weight buys
# context: a deep file gets room for its whole story, a skim file gets a look.
DEPTH_BUDGET = {"deep": 24_000, "normal": 10_000, "skim": 2_500}

_BRIEF = """You are reviewing ONE file from a pull request, in isolation. You cannot see any
other file and must not speculate about them; if a judgment depends on a file you cannot
see, say so instead of guessing.

File: {path}
Change: {status}, +{added}/-{removed}
Blast radius: {weight}/100 ({depth} review) because {because}

Report only defects you can point at a line for. For each, emit one line of JSON:
{{"severity": "high|medium|low", "summary": "<one sentence>", "evidence": "<the line>"}}
Emit nothing at all if the file is clean. No prose, no preamble, no summary paragraph.

{diff}"""


@dataclass(frozen=True)
class ReviewBrief:
    """One isolated unit of the fan-out: everything a single-file review may see."""

    path: str
    weight: int
    depth: str
    prompt: str
    truncated: bool


def build_briefs(files: list[ChangedFile], *, fence: Any = None) -> list[ReviewBrief]:
    """One brief per reviewable file, each carrying ONLY that file's diff.

    ``fence`` is ``personalclaw.sdk.security.fence_untrusted``, injected so this module
    stays import-pure for tests. A PR diff is attacker-controlled text about to be read
    by a model, so it is fenced as data, never as instructions.
    """
    briefs: list[ReviewBrief] = []
    for f in files:
        if f.binary or f.generated or f.unreadable or not f.hunks:
            continue
        budget = DEPTH_BUDGET[f.depth]
        body = f.diff_text()
        truncated = len(body) > budget
        if truncated:
            body = body[:budget] + f"\n… [diff truncated at {budget} chars for a {f.depth} review]"
        if fence is not None:
            body = fence(
                body,
                source=f"github pull request diff: {f.path}",
                source_type="code_review_diff",
                source_id=f.path,
            )
        briefs.append(
            ReviewBrief(
                path=f.path,
                weight=f.weight,
                depth=f.depth,
                truncated=truncated,
                prompt=_BRIEF.format(
                    path=f.path,
                    status=f.status,
                    added=f.added,
                    removed=f.removed,
                    weight=f.weight,
                    depth=f.depth,
                    because="; ".join(f.weight_because) or "no signal fired",
                    diff=body,
                ),
            )
        )
    return briefs


# ── What became of each changed file ────────────────────────────────────────
#
# The report's coverage words are built from these, so it never says more was reviewed
# than was.

REVIEWED = "reviewed"
REVIEWED_TRUNCATED = "reviewed on a truncated diff"
STATIC_ONLY = "checked statically only"
PENDING = "pending"
FAILED = "not reviewed: its review call failed"
UNREADABLE = "not reviewed: its header could not be read"
SKIPPED_BINARY = "skipped: binary"
SKIPPED_GENERATED = "skipped: generated"
SKIPPED_EMPTY = "skipped: no text change"
# Files with nothing a review could read: they never count against full coverage.
NOT_REVIEWABLE = (SKIPPED_BINARY, SKIPPED_GENERATED, SKIPPED_EMPTY)


def skipped_by_cap(cap: int) -> str:
    return f"skipped: dropped by max_files={cap}"


def file_outcomes(files: list[ChangedFile], *, cap: int, briefs: list[ReviewBrief], ran: str,
                  failed: set[str] | frozenset[str] = frozenset()) -> dict[str, str]:
    """What became of each changed file, in the order given (heaviest first).

    ``files`` is every changed file, the ones past ``cap`` included. ``ran`` is how far the
    review went: ``"model"`` (each brief got its own model review), ``"static"`` (only the
    deterministic pass) or ``"plan"`` (the briefs were handed out, nothing reviewed yet).
    ``failed`` names the briefs whose model review call failed.
    """
    briefed = {b.path: b for b in briefs}
    out: dict[str, str] = {}
    for i, f in enumerate(files):
        if f.unreadable:
            out[f.path] = UNREADABLE
        elif f.binary:
            out[f.path] = SKIPPED_BINARY
        elif f.generated:
            out[f.path] = SKIPPED_GENERATED
        elif i >= cap:
            out[f.path] = skipped_by_cap(cap)
        elif f.path not in briefed:
            out[f.path] = SKIPPED_EMPTY
        elif ran == "plan":
            out[f.path] = PENDING
        elif ran != "model":
            out[f.path] = STATIC_ONLY
        elif f.path in failed:
            out[f.path] = FAILED
        elif briefed[f.path].truncated:
            out[f.path] = REVIEWED_TRUNCATED
        else:
            out[f.path] = REVIEWED
    return out


def coverage(outcomes: dict[str, str]) -> str:
    """The outcomes counted, most reviewed first: "3 reviewed, 1 skipped: binary"."""
    counts: dict[str, int] = {}
    for outcome in outcomes.values():
        counts[outcome] = counts.get(outcome, 0) + 1
    order = [REVIEWED, REVIEWED_TRUNCATED, STATIC_ONLY, PENDING, FAILED, UNREADABLE]
    order += sorted(k for k in counts if k not in order)
    return ", ".join(f"{counts[k]} {k}" for k in order if counts.get(k))


def fully_reviewed(outcomes: dict[str, str]) -> bool:
    """Whether every file a review could read got a full model review."""
    values = list(outcomes.values())
    return REVIEWED in values and all(v == REVIEWED or v in NOT_REVIEWABLE for v in values)


# ── The deterministic pass ──────────────────────────────────────────────────
#
# What a diff alone can prove, with no model in the loop. Every rule here is a
# line-anchored fact, not a judgement — which is why they are safe to run unattended and
# why they are what the test suite pins. Order is severity-descending.

_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("high", "Merge-conflict marker committed",
     re.compile(r"^(<{7}|={7}|>{7})(\s|$)")),
    ("high", "Private key material added in a diff",
     re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    # An identifier whose NAME ends in a secret word, assigned a long literal. Both halves
    # are needed: the name alone flags `api_key = os.environ[...]`, and a long literal
    # alone flags every URL in the diff. The 12-char floor and the `{`/path exclusions are
    # what keep `sort_key = "name"` and `path_key = "/tmp/x"` out.
    ("high", "Hard-coded credential literal added",
     re.compile(r"""(?i)(?:^|[^\w.])[A-Za-z_][A-Za-z0-9_]*"""
                r"""(?:secret|password|passwd|token|key|credential)\s*[:=]\s*"""
                r"""['"](?!https?:|/)[^'"\s{}]{12,}['"]""")),
    ("high", "AWS access key id added",
     re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("medium", "Bare or blanket exception swallow added",
     re.compile(r"^\s*except\s*(?:Exception\s*)?:\s*(?:#.*)?$|^\s*catch\s*\(\s*\)\s*\{\s*\}")),
    ("medium", "Debugger breakpoint left in",
     re.compile(r"\b(?:pdb\.set_trace|breakpoint)\s*\(|^\s*debugger\s*;?\s*$")),
    ("medium", "Suppression added with no reason beside it",
     re.compile(r"(?:#\s*type:\s*ignore|#\s*noqa|eslint-disable)(?!\S*\[)(?!.*(?:—|--|because|:))")),
    ("low", "Debug print left in",
     re.compile(r"^\s*(?:print\s*\(|console\.(?:log|debug)\s*\()")),
    ("low", "New TODO/FIXME with no owner or issue link",
     re.compile(r"(?i)\b(?:TODO|FIXME|XXX|HACK)\b(?!\s*[(\[:]?\s*(?:@|#\d|http))")),
)

# Above this, a single file's churn is itself the finding: nobody reviews it properly.
_UNREVIEWABLE_CHURN = 600


@dataclass(frozen=True)
class Finding:
    """One reviewable defect, as it is persisted and as it is rendered."""

    file: str
    severity: str
    summary: str
    evidence: str = ""
    weight: int = 0
    source: str = "static"

    def to_line(self) -> dict[str, Any]:
        d = asdict(self)
        d["ts"] = time.time()
        return d


SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def static_findings(files: list[ChangedFile]) -> list[Finding]:
    """The zero-model pass: line-anchored facts a unified diff can prove on its own."""
    found: list[Finding] = []
    for f in files:
        if f.binary or f.generated:
            continue
        if f.churn > _UNREVIEWABLE_CHURN and f.status != "deleted":
            found.append(Finding(
                file=f.path, severity="medium", weight=f.weight,
                summary=(f"{f.churn} changed lines in one file — too large to review as a "
                         "unit; ask for it to be split"),
                evidence=f"+{f.added}/-{f.removed}",
            ))
        for sev, summary, pattern in _RULES:
            for line in f.added_lines:
                if pattern.search(line):
                    found.append(Finding(
                        file=f.path, severity=sev, summary=summary, weight=f.weight,
                        evidence=_clean(line)[:200],
                    ))
                    break  # one finding per rule per file — a list of 40 identical
                           # TODO hits is noise, not coverage
    found.sort(key=lambda x: (SEVERITY_ORDER.get(x.severity, 9), -x.weight, x.file))
    return found


def _clean(text: str) -> str:
    """Strip control characters, CR and LF from a value bound for the findings log.

    Log injection: the evidence field is attacker-authored text landing in
    a line-delimited log. A newline in it would forge a second record; a carriage return
    would hide the rest of the line from a terminal reader. JSON escaping alone stops the
    first but not the second, so both go.
    """
    return "".join(ch for ch in (text or "") if ch.isprintable()).strip()


# ── The local findings log ──────────────────────────────────────────────────

class FindingsLog:
    """Append-only JSONL under this app's own data dir. One finding per line.

    "Findings kept locally" is load-bearing and literal: this class is the ONLY writer,
    the path is always inside ``app_data_dir("code-review")``, and there is no reader,
    uploader or reporter anywhere in this bundle that leaves the machine. Nothing is
    posted back to the PR.
    """

    def __init__(self, root: Path | None = None) -> None:
        self._root = Path(root) if root is not None else app_data_dir(APP_NAME) / "findings"
        self._root.mkdir(parents=True, exist_ok=True)

    def path_for(self, ref: PrRef) -> Path:
        # `ref.slug` is built from a regex-validated PrRef, so it cannot contain a
        # separator or a `..`. Asserted rather than assumed: this is the one place a bad
        # reference would become a write outside the app's own dir.
        name = f"{ref.slug}.jsonl"
        target = (self._root / name).resolve()
        if target.parent != self._root.resolve():
            raise ValueError(f"refusing to write outside the findings dir: {target}")
        return target

    def append(self, ref: PrRef, findings: list[Finding]) -> int:
        if not findings:
            return 0
        target = self.path_for(ref)
        with target.open("a", encoding="utf-8") as fh:
            for finding in findings:
                fh.write(json.dumps(finding.to_line(), ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        return len(findings)

    def read(self, ref: PrRef) -> list[dict[str, Any]]:
        target = self.path_for(ref)
        if not target.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in target.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue  # a torn last line never costs the reader the whole log
        return out

    def reviewed_prs(self) -> list[str]:
        return sorted(p.stem.replace("__", "/") for p in self._root.glob("*.jsonl"))


# ── Rendering ───────────────────────────────────────────────────────────────

def _md_path(path: str) -> str:
    """A path as a code span that keeps to its own table cell or list line.

    An author picks their paths, and git allows a backtick, a ``|`` and control characters in
    one: shown raw they would end the code span, split the cell or the row, and could draw
    an outcome the review never gave. Control characters are shown escaped, ``|`` is escaped
    for the table, and a path holding a backtick gets a double-backtick span.
    """
    shown = "".join(ch if ch.isprintable() else repr(ch)[1:-1] for ch in path)
    shown = shown.replace("|", "\\|")
    return f"`` {shown} ``" if "`" in shown else f"`{shown}`"


def render_report(ref: PrRef, files: list[ChangedFile], findings: list[Finding],
                  *, fanout: str, outcomes: dict[str, str], log_path: Path | None = None) -> str:
    """The model-facing summary. Weights first, so the reader sees where attention went.

    ``files`` is every changed file and ``outcomes`` what became of each
    (:func:`file_outcomes`): the table lists them all, and the report claims full coverage
    only when every file a review could read got a full model review.
    """
    lines = [f"# Review of {ref}", ""]
    lines.append(f"{len(files)} changed file(s), fan-out via {fanout}.")
    lines.append("")
    lines.append("## Blast radius")
    lines.append("")
    lines.append("| weight | depth | file | change | outcome |")
    lines.append("|---|---|---|---|---|")
    for f in files:
        lines.append(f"| {f.weight} | {f.depth} | {_md_path(f.path)} | {f.status} "
                     f"+{f.added}/-{f.removed} | {outcomes.get(f.path, 'not reviewed')} |")
    lines.append("")
    lines.append(f"Coverage: {coverage(outcomes)}.")
    lines.append("")
    if findings:
        lines.append(f"## Findings ({len(findings)})")
        lines.append("")
        for x in findings:
            lines.append(f"- **{x.severity}** {_md_path(x.file)} — {x.summary}")
            if x.evidence:
                lines.append(f"  - evidence: `{x.evidence}`")
    else:
        lines.append("## Findings")
        lines.append("")
        if fully_reviewed(outcomes):
            lines.append("None. Every reviewable file was inspected and nothing was raised.")
        elif PENDING in outcomes.values():
            lines.append("Nothing reviewed yet: run the briefs below.")
        else:
            lines.append("None raised, but not every changed file got a full review: see the "
                         "coverage above.")
    if log_path is not None:
        lines.extend(["", f"Kept locally at `{log_path}` — nothing was posted to the PR."])
    return "\n".join(lines)
