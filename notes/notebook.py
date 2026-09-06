"""The git-backed markdown notebook: reference validation, the `git` edge, and the
plain-text search that is deliberately NOT an index.

Everything in here is synchronous and takes a root path, so the whole notebook is
testable against a temp dir with no provider, no gateway and no settings. The provider
wraps each call in ``asyncio.to_thread`` — ``git`` is a blocking child process and the
gateway's event loop must not wait on it.

Two invariants this module exists to hold:

1. **A note reference is validated before it is ever a path or an argv element.** One
   strict regex per path segment, then the resolved target is re-checked to be inside the
   notebook root (which also catches a symlink pointing out of it). A revision is a
   separate, narrower grammar. `git` is invoked with a fixed argv list, never a shell
   string, and every pathspec is passed after ``--``.
2. **The notebook is a directory of markdown files and nothing else.** No database, no
   index, no sidecar metadata. Which is what makes it portable, diffable, editable in any
   editor, and recoverable without this app.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from personalclaw.sdk.util import app_data_dir, atomic_write

APP_NAME = "notes"
DEFAULT_BRANCH = "main"

# Commits are made under a fixed identity passed with `-c`, so the notebook never leans
# on — or writes into — the user's ambient git config.
COMMIT_NAME = "PersonalClaw Notes"
COMMIT_EMAIL = "notes@personalclaw.local"

GIT_MISSING = (
    "`git` is not on PATH. Install git — the notebook IS a git repository, which is how "
    "note history survives an app reinstall and stays readable without PersonalClaw."
)

# A note reference is a relative posix path of `.md` files. Every segment must start with
# an alphanumeric, which is what makes `..`, `.git`, and dotfiles unrepresentable rather
# than filtered; no control character, quote, `~`, `$` or separator can appear inside one.
_SEGMENT = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9 ._-]{0,78}[A-Za-z0-9._-])?$")
MAX_SEGMENTS = 8
MAX_REF_LEN = 200

# A revision reaches `git` as `<rev>:./<path>`, so its grammar is narrow on purpose: a
# commit sha, HEAD, or HEAD~<n>. Anything else — a branch name, `@{upstream}`, a range —
# is refused rather than passed through, because this app only ever reads a note's own
# past and a wider grammar would only widen the argv surface.
_REVISION = re.compile(r"^(?:HEAD|HEAD~[0-9]{1,3}|[0-9a-f]{7,40})$")

# A single note is prose, not a payload. The cap is what keeps an agent from committing a
# 50 MB blob into a repository the user is expected to clone.
MAX_NOTE_BYTES = 1_000_000
# Search reads at most this much of any one file, and lists at most this many notes, so a
# notebook that grew unexpectedly degrades instead of stalling a tool call.
SEARCH_MAX_FILE_BYTES = 400_000
LIST_MAX_NOTES = 2_000
TITLE_SCAN_BYTES = 4_096
MAX_QUERY_LEN = 200

WRITE_MODES = ("replace", "append")


class NoteRefError(ValueError):
    """A note reference or revision that must never become a path or an argv element."""


class NoteMissing(LookupError):
    """The note (or that revision of it) is not in the notebook."""


class GitError(RuntimeError):
    """`git` was reachable but refused the operation."""


def parse_note_ref(raw: str) -> PurePosixPath:
    """Validate a note reference into a relative posix path, or refuse it.

    `.md` is appended when absent so `note_write(ref="ideas/tempo")` does the obvious
    thing; everything else about the reference has to already be right.
    """
    ref = (raw or "").strip()
    if not ref:
        raise NoteRefError("a note reference is required, e.g. 'ideas/tempo.md'")
    if "\\" in ref:
        raise NoteRefError(
            f"note reference {ref!r} contains a backslash — use '/' between folders"
        )
    if ref.startswith("/") or re.match(r"^[A-Za-z]:", ref):
        raise NoteRefError(
            f"note reference {ref!r} must be relative to the notebook, not an absolute path"
        )
    if not ref.lower().endswith(".md"):
        ref = f"{ref}.md"
    if len(ref) > MAX_REF_LEN:
        raise NoteRefError(f"note reference is longer than {MAX_REF_LEN} characters")
    segments = ref.split("/")
    if len(segments) > MAX_SEGMENTS:
        raise NoteRefError(f"note reference is nested deeper than {MAX_SEGMENTS} folders")
    for segment in segments:
        if not segment:
            raise NoteRefError(f"note reference {ref!r} has an empty path segment")
        if not _SEGMENT.match(segment):
            raise NoteRefError(
                f"path segment {segment!r} is not allowed — a segment must start with a "
                "letter or digit and use only letters, digits, spaces, '.', '_' and '-'"
            )
    return PurePosixPath(*segments)


def parse_revision(raw: str) -> str:
    """Validate a revision. See ``_REVISION`` for why the grammar is this narrow."""
    rev = (raw or "").strip()
    if not _REVISION.match(rev):
        raise NoteRefError(
            f"revision {raw!r} is not allowed — pass a commit sha (7-40 hex characters), "
            "'HEAD', or 'HEAD~<n>'. Use note_history to get a sha."
        )
    return rev


def note_path(root: Path, ref: str) -> tuple[Path, PurePosixPath]:
    """Resolve a validated reference to an absolute path inside *root*.

    The containment re-check is not redundant with ``parse_note_ref``: ``resolve()``
    follows symlinks, so this is what catches a link inside the notebook that points out
    of it — the one case a name-only check cannot see.
    """
    rel = parse_note_ref(ref)
    base = root.resolve()
    target = (base / Path(*rel.parts)).resolve()
    if target != base and base not in target.parents:
        raise NoteRefError(f"refusing to touch a path outside the notebook: {ref!r}")
    return target, rel


def note_title(text: str) -> str:
    """The note's display title: its first markdown H1, else its first non-empty line."""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()[:120] or "(untitled)"
        return stripped[:120]
    return "(empty)"


@dataclass(frozen=True)
class NoteInfo:
    ref: str
    title: str
    bytes: int
    modified: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "title": self.title,
            "bytes": self.bytes,
            "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(self.modified)),
        }


@dataclass(frozen=True)
class Commit:
    sha: str
    when: str
    subject: str

    def to_dict(self) -> dict[str, Any]:
        return {"sha": self.sha, "when": self.when, "subject": self.subject}


@dataclass(frozen=True)
class Hit:
    ref: str
    line: int
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"ref": self.ref, "line": self.line, "text": self.text}


class Notebook:
    """A directory of markdown notes with a git repository under it."""

    def __init__(self, root: Path | str | None = None, *, timeout: int = 20) -> None:
        # Lazy on purpose: constructing the provider is what core does to READ its tool
        # list, and that must not mkdir or `git init` under the user's home. The notebook
        # appears the first time a note is actually written.
        self._root = (
            Path(os.path.expandvars(os.path.expanduser(str(root))))
            if root
            else app_data_dir(APP_NAME) / "notebook"
        )
        self._timeout = max(5, int(timeout))

    @property
    def root(self) -> Path:
        return self._root

    # ── The `git` edge ──────────────────────────────────────────────────────────

    def _run(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Run one `git` command in the notebook. Fixed argv, captured output, hard timeout."""
        argv = [
            "git",
            "-C", str(self._root),
            "-c", f"user.name={COMMIT_NAME}",
            "-c", f"user.email={COMMIT_EMAIL}",
            *args,
        ]
        # A notebook operation must never block on a credential prompt. There is no remote
        # here (syncing a notebook is `git-sync`'s job, not this app's), but a user-pointed
        # notebook_path may sit in a repo that has one.
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        try:
            proc = subprocess.run(  # noqa: S603 — fixed argv, no shell, validated pathspecs
                argv, capture_output=True, text=True, timeout=self._timeout,
                check=False, env=env,
            )
        except FileNotFoundError as exc:
            raise GitError(GIT_MISSING) from exc
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"`git {args[0]}` timed out after {self._timeout}s") from exc
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:400]
            raise GitError(f"`git {args[0]}` failed (exit {proc.returncode}): {detail}")
        return proc

    def toplevel(self) -> str | None:
        """The worktree root `git` reports for the notebook dir, or None if there is none."""
        if not self._root.is_dir():
            return None
        proc = self._run(["rev-parse", "--show-toplevel"], check=False)
        return proc.stdout.strip() or None if proc.returncode == 0 else None

    def _has_head(self) -> bool:
        return self._run(["rev-parse", "--verify", "-q", "HEAD"], check=False).returncode == 0

    def head(self) -> str | None:
        proc = self._run(["rev-parse", "--short", "HEAD"], check=False)
        return proc.stdout.strip() or None if proc.returncode == 0 else None

    def ensure(self) -> None:
        """Make the notebook exist and be inside a git worktree. Idempotent.

        A notebook path that already sits inside a repository (someone pointed it at a
        folder of their dotfiles repo) ADOPTS that repository rather than nesting a second
        one inside it — nested repos are how a user loses history they thought they had.
        Every commit is then made with an explicit pathspec, so adopting a repo can never
        sweep the user's unrelated staged changes into a note commit.
        """
        self._root.mkdir(parents=True, exist_ok=True)
        if self.toplevel() is not None:
            return
        # `init -b` needs git >= 2.28; older git gets the two-step equivalent.
        if self._run(["init", "-b", DEFAULT_BRANCH], check=False).returncode != 0:
            self._run(["init"])
            self._run(["checkout", "-B", DEFAULT_BRANCH], check=False)

    def _commit(self, message: str, pathspec: str) -> str | None:
        args = ["commit", "-m", message]
        if self._has_head():
            # Partial commit: ONLY this note, even if the adopted repo has other things
            # staged. An unborn HEAD has no tree to build a partial commit from, and on an
            # unborn HEAD the repo is one we just created, so a full commit is equivalent.
            args += ["--", pathspec]
        self._run(args)
        return self.head()

    def _staged(self, pathspec: str) -> bool:
        proc = self._run(["status", "--porcelain", "--", pathspec])
        return bool(proc.stdout.strip())

    # ── Notes ───────────────────────────────────────────────────────────────────

    def write(
        self, ref: str, content: str, *, mode: str = "replace", message: str = "",
    ) -> dict[str, Any]:
        """Create, replace or append to a note, then commit exactly that note."""
        if mode not in WRITE_MODES:
            raise NoteRefError(f"mode must be one of {WRITE_MODES}, got {mode!r}")
        target, rel = note_path(self._root, ref)
        posix = rel.as_posix()
        body = content or ""
        existed = target.is_file()
        if mode == "append" and existed:
            previous = target.read_text(encoding="utf-8", errors="replace")
            joiner = "" if not previous or previous.endswith("\n") else "\n"
            body = f"{previous}{joiner}{body}"
        if not body.endswith("\n"):
            body += "\n"
        if len(body.encode("utf-8")) > MAX_NOTE_BYTES:
            raise NoteRefError(
                f"{posix} would be larger than {MAX_NOTE_BYTES // 1000} kB — a note is "
                "prose, not a payload. Split it, or keep the blob outside the notebook."
            )
        self.ensure()
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(target, body)
        self._run(["add", "--", posix])
        if not self._staged(posix):
            return {
                "ref": posix, "path": str(target), "created": False, "unchanged": True,
                "commit": self.head(), "bytes": len(body.encode("utf-8")),
            }
        subject = message.strip() or f"{'Update' if existed else 'Add'} {posix}"
        sha = self._commit(_one_line(subject), posix)
        return {
            "ref": posix, "path": str(target), "created": not existed, "unchanged": False,
            "commit": sha, "bytes": len(body.encode("utf-8")),
        }

    def read(self, ref: str, *, revision: str = "") -> dict[str, Any]:
        """Read a note from the working tree, or from one past revision of it."""
        target, rel = note_path(self._root, ref)
        posix = rel.as_posix()
        if revision:
            rev = parse_revision(revision)
            # `<rev>:./<path>` is the cwd-relative form, so this stays correct when the
            # notebook is a subdirectory of an adopted repository.
            proc = self._run(["show", f"{rev}:./{posix}"], check=False)
            if proc.returncode != 0:
                raise NoteMissing(f"{posix} does not exist at revision {rev}")
            return {"ref": posix, "revision": rev, "content": proc.stdout}
        if not target.is_file():
            raise NoteMissing(f"{posix} is not in the notebook")
        return {
            "ref": posix,
            "revision": "working tree",
            "content": target.read_text(encoding="utf-8", errors="replace"),
        }

    def list_notes(self) -> tuple[list[NoteInfo], int]:
        """Every note in the notebook, plus a count of files this app cannot address.

        The skipped count is reported rather than hidden: a `.md` file whose name does not
        pass ``parse_note_ref`` is real and visible on disk, but no tool here can read or
        write it, and silently omitting it would look like data loss.
        """
        if not self._root.is_dir():
            return [], 0
        notes: list[NoteInfo] = []
        skipped = 0
        base = self._root.resolve()
        for path in sorted(self._root.rglob("*.md")):
            rel_parts = path.relative_to(self._root).parts
            if any(part.startswith(".") for part in rel_parts[:-1]):
                continue  # .git and any other dot-directory is not notebook content
            if not path.is_file():
                continue
            ref = "/".join(rel_parts)
            try:
                resolved, _ = note_path(base, ref)
            except NoteRefError:
                skipped += 1
                continue
            if resolved != path.resolve():
                skipped += 1  # a symlink out of the notebook is not notebook content
                continue
            stat = path.stat()
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                head = fh.read(TITLE_SCAN_BYTES)
            notes.append(NoteInfo(ref, note_title(head), stat.st_size, stat.st_mtime))
            if len(notes) >= LIST_MAX_NOTES:
                break
        notes.sort(key=lambda n: (-n.modified, n.ref))
        return notes, skipped

    def search(
        self, query: str, *, regex: bool = False, limit: int = 20,
    ) -> tuple[list[Hit], bool]:
        """Match lines across the notebook. A grep, deliberately — see the README.

        Literal and case-insensitive by default. ``regex=True`` is opt-in and the pattern
        is the caller's own; it runs in-process over the user's own files with no
        backtracking guard, which is why it is not the default.
        """
        q = (query or "").strip()
        if not q:
            raise NoteRefError("a search query is required")
        if len(q) > MAX_QUERY_LEN:
            raise NoteRefError(f"a search query is capped at {MAX_QUERY_LEN} characters")
        matcher: Any
        if regex:
            try:
                matcher = re.compile(q, re.IGNORECASE)
            except re.error as exc:
                raise NoteRefError(f"{q!r} is not a valid regular expression: {exc}") from exc
        else:
            matcher = None
            needle = q.lower()
        cap = max(1, min(200, int(limit)))
        hits: list[Hit] = []
        notes, _ = self.list_notes()
        for info in notes:
            path = self._root / Path(*info.ref.split("/"))
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                text = fh.read(SEARCH_MAX_FILE_BYTES)
            for number, line in enumerate(text.splitlines(), start=1):
                found = matcher.search(line) if matcher is not None else needle in line.lower()
                if not found:
                    continue
                hits.append(Hit(info.ref, number, line.strip()[:300]))
                if len(hits) >= cap:
                    return hits, True
        return hits, False

    def history(self, ref: str = "", *, limit: int = 10) -> list[Commit]:
        """Commits touching one note, or the whole notebook when *ref* is empty."""
        count = max(1, min(200, int(limit)))
        args = ["log", f"--max-count={count}", "--format=%H%x1f%aI%x1f%s"]
        if ref:
            _, rel = note_path(self._root, ref)
            args += ["--", f"./{rel.as_posix()}"]
        proc = self._run(args, check=False)
        if proc.returncode != 0:
            return []  # an unborn branch has no history yet; that is not an error
        out: list[Commit] = []
        for line in proc.stdout.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 3:
                out.append(Commit(parts[0][:12], parts[1], parts[2]))
        return out

    def restore(self, ref: str, revision: str) -> dict[str, Any]:
        """Bring a past revision of a note back as a NEW commit. Never rewrites history."""
        past = self.read(ref, revision=revision)
        result = self.write(
            ref, past["content"],
            message=f"Restore {past['ref']} from {past['revision']}",
        )
        result["restored_from"] = past["revision"]
        return result

    def delete(self, ref: str) -> dict[str, Any]:
        """Remove a note from the working tree. Its content stays in git history."""
        target, rel = note_path(self._root, ref)
        posix = rel.as_posix()
        if not target.is_file():
            raise NoteMissing(f"{posix} is not in the notebook")
        tracked = self._run(
            ["ls-files", "--error-unmatch", "--", posix], check=False
        ).returncode == 0
        if not tracked:
            # Never committed, so there is nothing to recover it from — say so plainly.
            target.unlink()
            return {"ref": posix, "commit": None, "recoverable": False}
        self._run(["rm", "--quiet", "--", posix])
        sha = self._commit(f"Delete {posix}", posix)
        return {"ref": posix, "commit": sha, "recoverable": True}

    def status(self) -> dict[str, Any]:
        """What `doctor` and `info` report: where the notebook is and what state it is in."""
        notes, skipped = self.list_notes()
        toplevel = self.toplevel() if self._root.is_dir() else None
        return {
            "root": str(self._root),
            "exists": self._root.is_dir(),
            "repo_root": toplevel,
            "adopted": bool(toplevel) and Path(toplevel).resolve() != self._root.resolve(),
            "notes": len(notes),
            "unaddressable_files": skipped,
            "head": self.head() if toplevel else None,
        }


def _one_line(text: str) -> str:
    """A commit subject is one line: newlines and control characters go.

    A commit message reaches `git` as an argv element, so it cannot inject a command — but
    a newline in it would still forge what looks like a second commit line in every log
    this notebook is read through.
    """
    return "".join(ch if ch.isprintable() else " " for ch in text).strip()[:120] or "Update note"
