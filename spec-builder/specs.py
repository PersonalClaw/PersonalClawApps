"""The spec store: identifier and path grammars, the `git` seed edge, and the structural
readiness verdict.

Everything in here is synchronous and takes a root path, so the whole store is testable
against a temp dir with no provider, no gateway and no settings. The provider wraps each
call in ``asyncio.to_thread`` — ``git`` is a blocking child process and the gateway's event
loop must not wait on it.

Three invariants this module exists to hold:

1. **A spec id and a source path are validated before either is ever a path or an argv
   element.** One strict regex for an id, one per path segment, then the resolved target is
   re-checked to be inside its root (which also catches a symlink pointing out of it). A
   revision is a separate, narrower grammar. `git` is invoked with a fixed argv list, never
   a shell string, and the pathspec reaches it as `<rev>:./<path>` after the subcommand.
2. **Seeded text is data, never instruction.** Content read out of a source repository lands
   in the ``background`` section and is fenced on the way to any model. It is also the one
   section the compiler refuses to inline (see :mod:`workflow_spec`).
3. **This module decides nothing a model has to be asked about.** The readiness verdict is a
   structural count over the sections — which are empty, how many clauses and steps parsed,
   which steps have no instruction. A spec is judged on its shape here; whether the prose is
   any good is the reviewer's call, not this app's.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from personalclaw.sdk.util import app_data_dir, atomic_write

APP_NAME = "spec-builder"
SPEC_FILE = "spec.json"
STORE_VERSION = 1

GIT_MISSING = (
    "`git` is not on PATH. Install git — seeding a spec from a repository reads the file "
    "through `git show`, so the spec can be grounded in a named revision rather than in "
    "whatever happens to be in the working tree."
)
NO_SOURCE_REPO = (
    "No source repository is configured. Set 'Source repository' in Settings → Tools → "
    "Spec Builder to the repo this spec is about; there is deliberately no default, because "
    "an app that guesses which checkout to read is an app that reads the wrong one."
)

# A spec id becomes a directory name AND the compiled workflow definition's name, so it is
# held to the workflow name grammar: lowercase, digits, hyphens, starting alphanumeric.
# That makes `..`, `.git` and dotfiles unrepresentable rather than filtered.
_SPEC_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?$")

# A source path is a relative posix path inside the configured repository. Every segment
# must start with an alphanumeric, so `..`, `.git`, dotfiles and `-oProxyCommand=` cannot be
# spelled at all; no control character, quote, `~`, `$` or separator survives one either.
_SEGMENT = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9 ._-]{0,78}[A-Za-z0-9._-])?$")
MAX_PATH_SEGMENTS = 12
MAX_PATH_LEN = 240

# A revision reaches `git` as `<rev>:./<path>`, so its grammar is narrow on purpose: a commit
# sha, HEAD, or HEAD~<n>. A branch name, `@{upstream}`, a range, `--upload-pack=…` are all
# refused rather than passed through — this app only ever reads one committed file.
_REVISION = re.compile(r"^(?:HEAD|HEAD~[0-9]{1,3}|[0-9a-f]{7,40})$")

#: The section vocabulary. Closed on purpose: an open one turns into a second free-form
#: document store, which is what `notes` already is.
SECTIONS: tuple[str, ...] = (
    "problem",
    "outcome",
    "steps",
    "verification",
    "non_goals",
    "constraints",
    "background",
)

#: A spec cannot compile without these. `background` and `non_goals` are optional context;
#: the other four are what a workflow node is built out of.
REQUIRED_SECTIONS: tuple[str, ...] = ("problem", "outcome", "steps", "verification")

#: `background` is written by the seed path and is therefore the only section that can hold
#: text this app did not author. Tracked here so the fencing and the compiler's exclusion
#: both key off one name.
SEEDED_SECTION = "background"

WRITE_MODES = ("replace", "append")

MAX_TITLE_LEN = 120
MAX_INTENT_LEN = 400
MAX_SECTION_CHARS = 40_000
MAX_SEED_BYTES = 200_000
MAX_SPECS = 500
MAX_STEPS = 40
MAX_CLAUSES = 40
#: A step whose instruction is shorter than this compiles to a stage prompt that says
#: nothing, so readiness names it rather than letting the engine discover it at run time.
MIN_INSTRUCTION_CHARS = 16

_BULLET = re.compile(r"^\s*[-*]\s+(?P<body>.+?)\s*$")


class SpecRefError(ValueError):
    """A spec id, section name, source path or revision that must never become a path."""


class SpecMissing(LookupError):
    """No such spec in the store."""


class GitError(RuntimeError):
    """`git` was reachable but refused the operation."""


def parse_spec_id(raw: str) -> str:
    """Validate a spec id, or refuse it. See ``_SPEC_ID`` for why the grammar is this tight."""
    spec_id = (raw or "").strip().lower()
    if not spec_id:
        raise SpecRefError("a spec id is required, e.g. 'inbox-triage'")
    if not _SPEC_ID.match(spec_id):
        raise SpecRefError(
            f"spec id {raw!r} is not allowed — use lowercase letters, digits and hyphens, "
            "starting and ending with a letter or digit, up to 48 characters"
        )
    return spec_id


def slugify(title: str) -> str:
    """Derive a candidate spec id from a title. Refused by ``parse_spec_id`` if it cannot."""
    lowered = (title or "").strip().lower()
    hyphenated = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return hyphenated[:48].rstrip("-")


def parse_section(raw: str) -> str:
    """Validate a section name against the closed vocabulary."""
    name = (raw or "").strip().lower()
    if name not in SECTIONS:
        raise SpecRefError(
            f"section {raw!r} is not one of {', '.join(SECTIONS)}. The vocabulary is closed — "
            "a spec is a shape, not a free-form document."
        )
    return name


def parse_source_path(raw: str) -> PurePosixPath:
    """Validate a path inside the source repository, or refuse it.

    The result is used twice — as a filesystem path for the containment check, and as the
    `<rev>:./<path>` argument to `git show`. Validating once, here, is what keeps those two
    uses from disagreeing about what the caller asked for.
    """
    path = (raw or "").strip()
    if not path:
        raise SpecRefError("a source path is required, e.g. 'src/app/router.py'")
    if "\\" in path:
        raise SpecRefError(f"source path {path!r} contains a backslash — use '/' between folders")
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise SpecRefError(f"source path {path!r} must be relative to the repository, not absolute")
    if len(path) > MAX_PATH_LEN:
        raise SpecRefError(f"source path is longer than {MAX_PATH_LEN} characters")
    segments = path.split("/")
    if len(segments) > MAX_PATH_SEGMENTS:
        raise SpecRefError(f"source path is nested deeper than {MAX_PATH_SEGMENTS} folders")
    for segment in segments:
        if not segment:
            raise SpecRefError(f"source path {path!r} has an empty path segment")
        if not _SEGMENT.match(segment):
            raise SpecRefError(
                f"path segment {segment!r} is not allowed — a segment must start with a "
                "letter or digit and use only letters, digits, spaces, '.', '_' and '-'"
            )
    return PurePosixPath(*segments)


def parse_revision(raw: str) -> str:
    """Validate a revision. See ``_REVISION`` for why the grammar is this narrow."""
    rev = (raw or "").strip() or "HEAD"
    if not _REVISION.match(rev):
        raise SpecRefError(
            f"revision {raw!r} is not allowed — pass a commit sha (7-40 hex characters), "
            "'HEAD', or 'HEAD~<n>'. A branch name or a range is refused on purpose."
        )
    return rev


def one_line(text: str, limit: int) -> str:
    """Collapse text to one printable line and cap it.

    Titles and intents are echoed into logs, tables and the compiled definition's
    description. A newline in one would forge a row in every surface it is read through.
    """
    collapsed = " ".join((text or "").split())
    printable = "".join(ch for ch in collapsed if ch.isprintable())
    return printable[:limit].strip()


def parse_clauses(section_text: str) -> list[str]:
    """The `- ` bullets of an `outcome` section, in order — the done-when clauses."""
    out: list[str] = []
    for line in (section_text or "").splitlines():
        match = _BULLET.match(line)
        if match:
            clause = one_line(match.group("body"), 300)
            if clause:
                out.append(clause)
        if len(out) >= MAX_CLAUSES:
            break
    return out


@dataclass(frozen=True)
class Step:
    """One `- <label>: <instruction>` bullet of a `steps` section."""

    label: str
    instruction: str

    @property
    def ok(self) -> bool:
        return bool(self.label) and len(self.instruction) >= MIN_INSTRUCTION_CHARS

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "instruction": self.instruction, "ok": self.ok}


def parse_steps(section_text: str) -> list[Step]:
    """The `- <label>: <instruction>` bullets of a `steps` section, in order.

    A bullet with no colon keeps its whole body as the label and gets an empty instruction,
    which readiness then names — rather than this parser guessing which half was meant.
    """
    out: list[Step] = []
    for line in (section_text or "").splitlines():
        match = _BULLET.match(line)
        if not match:
            continue
        body = match.group("body")
        label, _, instruction = body.partition(":")
        out.append(
            Step(
                label=one_line(label, 120),
                instruction=one_line(instruction, 2_000),
            )
        )
        if len(out) >= MAX_STEPS:
            break
    return out


@dataclass
class Spec:
    """One spec: an intent, the closed section set, and its timestamps."""

    id: str
    title: str
    intent: str = ""
    sections: dict[str, str] = field(default_factory=dict)
    created: float = 0.0
    updated: float = 0.0

    def section(self, name: str) -> str:
        return self.sections.get(name, "")

    @property
    def clauses(self) -> list[str]:
        return parse_clauses(self.section("outcome"))

    @property
    def steps(self) -> list[Step]:
        return parse_steps(self.section("steps"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "store_version": STORE_VERSION,
            "id": self.id,
            "title": self.title,
            "intent": self.intent,
            "sections": {name: self.sections.get(name, "") for name in SECTIONS},
            "created": self.created,
            "updated": self.updated,
        }

    def summary(self) -> dict[str, Any]:
        """The listing row: shape, never bodies."""
        return {
            "id": self.id,
            "title": self.title,
            "clauses": len(self.clauses),
            "steps": len(self.steps),
            "filled": sorted(name for name in SECTIONS if self.sections.get(name, "").strip()),
            "updated": time.strftime("%Y-%m-%d %H:%M", time.localtime(self.updated)),
            "ready": self.readiness().ready,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Spec:
        """Tolerant load: an unknown section is dropped, a missing one reads as empty.

        Tolerant rather than strict because a spec is long-lived authoring state — refusing
        to open one because a later version of this app added a section would lose the user's
        writing, which is the only thing in here that cannot be recomputed.
        """
        raw_sections = raw.get("sections")
        sections = raw_sections if isinstance(raw_sections, dict) else {}
        return cls(
            id=parse_spec_id(str(raw.get("id") or "")),
            title=one_line(str(raw.get("title") or ""), MAX_TITLE_LEN),
            intent=one_line(str(raw.get("intent") or ""), MAX_INTENT_LEN),
            sections={name: str(sections.get(name) or "")[:MAX_SECTION_CHARS] for name in SECTIONS},
            created=float(raw.get("created") or 0.0),
            updated=float(raw.get("updated") or 0.0),
        )

    def readiness(self) -> Readiness:
        """The structural verdict — see :class:`Readiness`."""
        empty = [name for name in REQUIRED_SECTIONS if not self.section(name).strip()]
        clauses = self.clauses
        steps = self.steps
        problems: list[str] = []
        for name in empty:
            problems.append(f"section `{name}` is empty")
        if not empty and not clauses:
            problems.append(
                "`outcome` has no `- ` bullets — each done-when clause is one bullet, and the "
                "compiled workflow checks them one by one"
            )
        if "steps" not in empty and not steps:
            problems.append(
                "`steps` has no `- ` bullets — each step is `- <label>: <instruction>` and "
                "becomes one stage of the compiled workflow"
            )
        for index, step in enumerate(steps, start=1):
            if not step.label:
                problems.append(f"step {index} has no label before its ':'")
            elif len(step.instruction) < MIN_INSTRUCTION_CHARS:
                problems.append(
                    f"step {index} ({step.label!r}) has no instruction after its ':' — a stage "
                    f"prompt needs at least {MIN_INSTRUCTION_CHARS} characters to say anything"
                )
        return Readiness(
            spec_id=self.id,
            clauses=len(clauses),
            steps=len(steps),
            empty_sections=empty,
            problems=problems,
        )


@dataclass(frozen=True)
class Readiness:
    """Whether a spec has the SHAPE to compile. Never a judgement about the prose.

    Kept separate from the compiler so ``spec_review`` can answer without producing a
    definition, and so the compiler has exactly one gate to consult rather than re-deriving
    the same conditions in a second place that can drift.
    """

    spec_id: str
    clauses: int
    steps: int
    empty_sections: list[str]
    problems: list[str]

    @property
    def ready(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec": self.spec_id,
            "ready": self.ready,
            "clauses": self.clauses,
            "steps": self.steps,
            "empty_sections": list(self.empty_sections),
            "problems": list(self.problems),
        }


class SpecStore:
    """A directory of spec records, plus the read-only `git` edge that seeds them."""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        source_repo: str = "",
        timeout: int = 20,
    ) -> None:
        # Lazy on purpose: constructing the provider is what core does to READ its tool list
        # (Settings -> Tools, the manifest round-trip), and that must not mkdir under the
        # user's home. The store appears the first time a spec is actually written.
        self._root = Path(root) if root else app_data_dir(APP_NAME) / "specs"
        self._source_repo = str(source_repo or "").strip()
        self._timeout = max(5, int(timeout))

    @property
    def root(self) -> Path:
        return self._root

    @property
    def source_repo(self) -> str:
        return self._source_repo

    # ── Records ─────────────────────────────────────────────────────────────────

    def _dir(self, spec_id: str) -> Path:
        """The spec's directory, asserted to be inside the store on the RESOLVED path.

        The containment re-check is not redundant with ``parse_spec_id``: ``resolve()``
        follows symlinks, so this is what catches a link inside the store pointing out of it
        — the one case a name-only check cannot see.
        """
        valid = parse_spec_id(spec_id)
        base = self._root.resolve()
        target = (base / valid).resolve()
        if target != base and base not in target.parents:
            raise SpecRefError(f"refusing to touch a path outside the spec store: {spec_id!r}")
        return target

    def _path(self, spec_id: str) -> Path:
        return self._dir(spec_id) / SPEC_FILE

    def exists(self, spec_id: str) -> bool:
        return self._path(spec_id).is_file()

    def load(self, spec_id: str) -> Spec:
        path = self._path(spec_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise SpecMissing(
                f"no spec {parse_spec_id(spec_id)!r} — call spec_list to see what there is"
            ) from exc
        except ValueError as exc:
            raise SpecMissing(
                f"spec {parse_spec_id(spec_id)!r} is on disk but will not parse: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise SpecMissing(f"spec {parse_spec_id(spec_id)!r} is not a spec record")
        return Spec.from_dict({**raw, "id": parse_spec_id(spec_id)})

    def save(self, spec: Spec) -> None:
        spec.updated = time.time()
        target = self._path(spec.id)
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(target, json.dumps(spec.to_dict(), indent=2, sort_keys=True) + "\n")

    def open_spec(self, title: str, *, intent: str = "", spec_id: str = "") -> Spec:
        """Create a spec. Refuses to overwrite an existing one — that is what write is for."""
        clean_title = one_line(title, MAX_TITLE_LEN)
        if not clean_title:
            raise SpecRefError("a spec needs a title")
        candidate = parse_spec_id(spec_id or slugify(clean_title))
        if self.exists(candidate):
            raise SpecRefError(
                f"spec {candidate!r} already exists — write its sections with spec_write, or "
                "pass a different spec_id"
            )
        if len(self.list_ids()) >= MAX_SPECS:
            raise SpecRefError(f"the store already holds {MAX_SPECS} specs; delete one first")
        now = time.time()
        spec = Spec(
            id=candidate,
            title=clean_title,
            intent=one_line(intent, MAX_INTENT_LEN),
            sections={name: "" for name in SECTIONS},
            created=now,
            updated=now,
        )
        self.save(spec)
        return spec

    def write_section(
        self, spec_id: str, section: str, content: str, *, mode: str = "replace"
    ) -> dict[str, Any]:
        """Replace or append one section. Returns shape only — never the content back."""
        if mode not in WRITE_MODES:
            raise SpecRefError(f"mode must be one of {WRITE_MODES}, got {mode!r}")
        name = parse_section(section)
        spec = self.load(spec_id)
        previous = spec.section(name)
        addition = content or ""
        if mode == "append" and previous:
            merged = f"{previous.rstrip()}\n{addition.lstrip()}"
        else:
            merged = addition
        if len(merged) > MAX_SECTION_CHARS:
            raise SpecRefError(
                f"section `{name}` would be {len(merged)} characters; the cap is "
                f"{MAX_SECTION_CHARS}. A spec is a brief, not an archive."
            )
        spec.sections[name] = merged
        self.save(spec)
        return {
            "spec": spec.id,
            "section": name,
            "mode": mode,
            "chars": len(merged),
            "unchanged": merged == previous,
            "readiness": spec.readiness().to_dict(),
        }

    def set_meta(self, spec_id: str, *, title: str = "", intent: str = "") -> dict[str, Any]:
        spec = self.load(spec_id)
        if title:
            spec.title = one_line(title, MAX_TITLE_LEN)
        if intent:
            spec.intent = one_line(intent, MAX_INTENT_LEN)
        self.save(spec)
        return {"spec": spec.id, "title": spec.title, "intent": spec.intent}

    def list_ids(self) -> list[str]:
        if not self._root.is_dir():
            return []
        out: list[str] = []
        for child in sorted(self._root.iterdir()):
            if not child.is_dir() or not (child / SPEC_FILE).is_file():
                continue
            try:
                out.append(parse_spec_id(child.name))
            except SpecRefError:
                # A directory this app could not have created. Counted by `list_specs`, not
                # silently swept up into the listing as if it were a spec.
                continue
        return out

    def list_specs(self) -> tuple[list[Spec], int]:
        """Every readable spec, newest first, plus a count of the unreadable ones."""
        specs: list[Spec] = []
        unreadable = 0
        for spec_id in self.list_ids():
            try:
                specs.append(self.load(spec_id))
            except (SpecMissing, SpecRefError, OSError):
                unreadable += 1
        specs.sort(key=lambda s: s.updated, reverse=True)
        return specs, unreadable

    def delete(self, spec_id: str) -> dict[str, Any]:
        target = self._dir(spec_id)
        if not (target / SPEC_FILE).is_file():
            raise SpecMissing(f"no spec {parse_spec_id(spec_id)!r} to delete")
        # Only ever the record and the directory this app created — never a recursive tree
        # walk, so a symlink or a stray file under the spec dir cannot widen a delete.
        (target / SPEC_FILE).unlink()
        leftovers = sorted(p.name for p in target.iterdir()) if target.is_dir() else []
        if not leftovers:
            target.rmdir()
        return {
            "spec": parse_spec_id(spec_id),
            "removed": True,
            "leftover_files": leftovers,
        }

    # ── The `git` seed edge ─────────────────────────────────────────────────────

    def _repo(self) -> Path:
        if not self._source_repo:
            raise SpecRefError(NO_SOURCE_REPO)
        repo = Path(os.path.expandvars(os.path.expanduser(self._source_repo)))
        if not repo.is_dir():
            raise SpecRefError(
                f"the configured source repository {self._source_repo!r} is not a directory"
            )
        return repo

    def _git(self, repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
        """Run one read-only `git` command. Fixed argv, captured output, hard timeout."""
        argv = ["git", "-C", str(repo), *args]
        # Seeding must never block on a credential prompt: the configured repo may well have
        # a remote, and `git show` on a missing object can otherwise stall on one.
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        try:
            return subprocess.run(  # noqa: S603 — fixed argv, no shell, validated pathspec
                argv,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
                env=env,
            )
        except FileNotFoundError as exc:
            raise GitError(GIT_MISSING) from exc
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"`git {args[0]}` timed out after {self._timeout}s") from exc

    def repo_head(self) -> str | None:
        """The configured repo's short HEAD, or None when there is no repo to read."""
        try:
            repo = self._repo()
        except SpecRefError:
            return None
        proc = self._git(repo, ["rev-parse", "--short", "HEAD"])
        return proc.stdout.strip() or None if proc.returncode == 0 else None

    def read_source(self, path: str, *, revision: str = "") -> dict[str, Any]:
        """Read one file out of the configured repository at a named revision.

        Read through `git show` rather than off disk on purpose: a spec grounded in "whatever
        was in the working tree" cannot be re-derived later, and a dirty tree would make the
        seeded background disagree with the sha the spec cites.
        """
        repo = self._repo()
        rel = parse_source_path(path)
        rev = parse_revision(revision)
        posix = rel.as_posix()

        # Containment is asserted on the RESOLVED path before `git` is asked for anything, so
        # a symlinked directory inside the repo cannot be used to name a file outside it.
        base = repo.resolve()
        target = (base / Path(*rel.parts)).resolve()
        if target != base and base not in target.parents:
            raise SpecRefError(f"refusing to read a path outside the repository: {path!r}")

        proc = self._git(repo, ["show", f"{rev}:./{posix}"])
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:300]
            raise GitError(f"`git show {rev}:./{posix}` failed: {detail}")
        text = proc.stdout
        truncated = len(text.encode("utf-8")) > MAX_SEED_BYTES
        if truncated:
            text = text.encode("utf-8")[:MAX_SEED_BYTES].decode("utf-8", errors="ignore")
        sha = self._git(repo, ["rev-parse", "--short", rev])
        return {
            "path": posix,
            "revision": rev,
            "resolved": sha.stdout.strip() if sha.returncode == 0 else rev,
            "repo": str(repo),
            "text": text,
            "truncated": truncated,
            "chars": len(text),
        }
