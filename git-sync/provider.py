"""Git-sync transport — carries durability shard objects through a git remote you own.

Point two machines' git-sync at the same git remote you control and the durability layer
converges through it. The transport keeps a **local working clone** and moves one object
per shard key as a file it commits and pushes; ``git log -p`` over those shards is the
human-diffable audit history of what the assistant knows — the whole point of this
transport. Because that readable history is the value, git-sync does **not** encrypt (the
merge, the machine-seq registry, and the outbox all live above it in core; secrets are
excluded upstream and never reach any transport).

Every method is insert-only and idempotent on the object key: a retried push of an object
already present is a no-op (skipped, never overwritten), so the sync cycle can retry
freely after a lost race. The registry compare-and-swap rides git's own push rejection —
if the remote moved under us the push is rejected and we report the lost race, cleaner
than a hand-rolled lock. The service (never an agent) invokes ``git`` via ``subprocess``;
no subprocess error is ever allowed to raise out of a contract method.
"""

import errno
import hashlib
import os
import subprocess
from typing import Any

from personalclaw.sdk.net import sentence_with_detail
from personalclaw.sdk.sync import (
    ConnectionResult,
    PushResult,
    RemoteRef,
    SyncObject,
    SyncTransportProvider,
)

# The single shared registry object every machine compare-and-swaps.
_REGISTRY_KEY = "registry.json"

# ``list_remote`` skips any file whose basename starts with this — git-sync writes objects
# in place and never leaves such files, but a stray one is never advertised as a real ref.
_TMP_PREFIX = ".tmp-"

# Ceiling for any single git invocation. A clone/pull that blows past this is treated as a
# transient failure by the caller, never a hang.
_GIT_TIMEOUT = 120

# Deterministic committer identity for the transport's automated commits. It never depends
# on ambient git config and names no real person — a sync commit is the machine's, not a
# contributor's.
_COMMIT_NAME = "PersonalClaw Sync"
_COMMIT_EMAIL = "sync@personalclaw.local"

# ── What a failure says ──────────────────────────────────────────────────────────────
#
# git's own words ("fatal: Could not read from remote repository.", a Python
# ``CalledProcessError`` naming the argv) say neither what is wrong in the user's setup nor
# what to do. Each failure below is said as that, and git's words follow as the detail.

#: Where this transport's own settings (Git remote URL, Local working clone, Branch) are set.
_ON_CARD = "on the Git Sync card in Settings → Providers"
#: Said after a failure the sync cycle retries (a ``transient`` outcome).
_RETRIES = "Sync tries again on its next run."

# What git — and the ssh it runs — prints for each kind of trouble with the remote, matched
# lower-cased, in the order ``_remote_trouble`` checks them. The credential needles are ssh's
# and https's own refusals: a bare "permission denied" is also how a LOCAL folder refuses a
# write, which is the working clone's trouble, not the remote's (``_CLONE_DENIED``).
_HOST_KEY_CHANGED = ("remote host identification has changed",)
_HOST_KEY = ("host key verification failed",)
_CREDENTIALS = (
    "permission denied (",
    "permission denied, please try again",
    "authentication failed",
    "could not read username",
    "could not read password",
    "invalid username or password",
    "access denied",
    "error: 401",
    "error: 403",
)
_UNREACHABLE = (
    "could not resolve host",
    "connection timed out",
    "connection refused",
    "network is unreachable",
    "no route to host",
    "failed to connect",
    "operation timed out",
)
_NO_GIT_THERE = (
    "upload-pack: command not found",
    "receive-pack: command not found",
    "upload-pack: not found",
    "receive-pack: not found",
)
_NO_REPOSITORY = (
    "repository not found",
    "does not appear to be a git repository",
    "does not exist",
    "not found",
)
_RULES = ("hook declined", "protected branch", "not allowed to push")
# What git prints when the working clone's own folder refuses it.
_CLONE_DENIED = ("permission denied", "insufficient permission", "read-only file system")


def _words(failure: object) -> str:
    """What git itself said about a failure — its stderr, else its stdout, else the exception's
    own text — kept as the detail after the sentence."""
    for stream in (getattr(failure, "stderr", None), getattr(failure, "stdout", None)):
        if isinstance(stream, bytes):
            stream = stream.decode("utf-8", "replace")
        if isinstance(stream, str) and stream.strip():
            return stream.strip()
    return "" if isinstance(failure, subprocess.CompletedProcess) else str(failure)


def _subcommand(cmd: object) -> str:
    """The git subcommand a failed run was (``clone``, ``add``), past ``-C``/``-c`` and their
    values."""
    args = [str(a) for a in cmd] if isinstance(cmd, (list, tuple)) else []
    rest = args[1:] if args[:1] == ["git"] else args
    while len(rest) >= 2 and rest[0] in ("-C", "-c"):
        rest = rest[2:]
    return rest[0] if rest else "git"


def _remote_trouble(words: str) -> str:
    """What git's words say went wrong with the remote, and what to do — "" when they name
    none of the kinds of trouble this knows."""
    low = words.lower()
    if any(needle in low for needle in _HOST_KEY_CHANGED):
        return (
            "the remote's SSH host key has changed since this machine last trusted it, so SSH "
            "refused to connect. Only if you know why it changed, remove its old key from this "
            "machine's known_hosts file, then connect to that host once from a terminal here."
        )
    if any(needle in low for needle in _HOST_KEY):
        return (
            "SSH on this machine doesn't trust the remote's host key yet. Connect to that host "
            "once from a terminal here to accept its key."
        )
    if any(needle in low for needle in _CREDENTIALS):
        return (
            "the remote didn't accept this machine's credentials, or git had none to give it. "
            "Check that git on this machine can reach it — its SSH key, or a credential helper "
            "for an https URL."
        )
    if any(needle in low for needle in _UNREACHABLE):
        return (
            "the remote couldn't be reached from this machine. Check that it is online and that "
            f"Git remote URL {_ON_CARD} names the right host."
        )
    if any(needle in low for needle in _NO_GIT_THERE):
        return (
            "the remote host couldn't run git — it isn't installed there, or isn't on the PATH "
            "its SSH logins get. Install git on that host."
        )
    if any(needle in low for needle in _NO_REPOSITORY):
        return (
            "no repository at that address is visible to this machine — it doesn't exist, or "
            f"this machine's credentials can't see it. Check Git remote URL {_ON_CARD}."
        )
    return ""


def _git_unrunnable(failure: BaseException) -> bool:
    """Whether ``failure`` is this machine not being able to start the git executable at all."""
    return isinstance(failure, (FileNotFoundError, PermissionError)) and failure.filename == "git"


#: Said when git itself could not be started.
_NO_GIT = (
    "Git Sync runs the git command, and PersonalClaw couldn't start it on this machine: it "
    "isn't installed, isn't on the PATH PersonalClaw runs with, or isn't executable. Install "
    "git where PersonalClaw can run it."
)


class GitSyncProvider(SyncTransportProvider):
    """A durability sync transport backed by a git remote the user owns."""

    name = "git-sync"
    display_name = "Git Sync"

    def __init__(
        self,
        repo_url: str = "",
        local_clone: str = "~/.personalclaw/sync/git-sync",
        branch: str = "main",
    ) -> None:
        self._repo_url = repo_url or ""
        # Expand ``~`` and ``$VARS`` so a configured "~/.personalclaw/sync/git-sync" or
        # "$HOME/sync" resolves to a real path. An empty clone path leaves the transport
        # idle rather than crashing.
        self._clone = os.path.expandvars(os.path.expanduser(local_clone)) if local_clone else ""
        self._branch = branch or "main"

    # ── internal helpers ─────────────────────────────────────────────────────────────

    @property
    def _idle(self) -> bool:
        """No remote (or nowhere to clone it) → the transport is idle, not broken."""
        return not self._repo_url or not self._clone

    def _resolve(self, key: str) -> str:
        """Map a remote-relative posix key to an absolute path inside the working clone."""
        # Split on "/" and rejoin with the OS separator so nested keys land in real
        # subdirectories regardless of platform.
        return os.path.join(self._clone, *key.split("/"))

    def _run(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess:
        """Run ``git <args>`` with output captured and a hard timeout. Not scoped to the
        clone — used for ``clone`` (the clone dir does not exist yet) and ``ls-remote``."""
        return subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            check=check,
        )

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        """Run ``git -C <clone> <args>`` — every operation against the working clone."""
        return self._run(["-C", self._clone, *args], check=check)

    def _commit(self, message: str) -> None:
        """Commit the staged tree under the transport's own deterministic identity, set via
        ``-c`` so it never leans on (or pollutes) ambient git config."""
        self._git(
            "-c",
            f"user.name={_COMMIT_NAME}",
            "-c",
            f"user.email={_COMMIT_EMAIL}",
            "commit",
            "-m",
            message,
        )

    def _ensure_clone(self) -> None:
        """Make ``<clone>`` a checkout of the remote on the configured branch. Idempotent.

        A brand-new empty remote is not an error — it is the first machine: ``git clone``
        of an empty remote succeeds (with a warning) leaving an unborn branch, which we
        adopt with ``checkout -B`` so the first push publishes it.
        """
        git_dir = os.path.join(self._clone, ".git")
        if not os.path.isdir(git_dir):
            parent = os.path.dirname(self._clone.rstrip("/")) or "."
            os.makedirs(parent, exist_ok=True)
            # check=True: a real clone failure (bad URL / no auth) raises and the caller
            # converts it. An empty remote still returns 0 here.
            self._run(["clone", self._repo_url, self._clone])
        # On a populated remote the branch (or a remote-tracking DWIM of it) checks out; on
        # an empty/new remote it does not exist yet, so create it locally for the first push.
        if self._git("checkout", self._branch, check=False).returncode != 0:
            self._git("checkout", "-B", self._branch, check=False)

    def _pull_ff(self) -> None:
        """Best-effort fast-forward pull of the configured branch. A failure — an empty
        remote with no such ref yet, an offline blip — is swallowed: the cycle re-pulls
        next time and a stale-but-present clone is still usable."""
        try:
            self._git("pull", "--ff-only", "origin", self._branch, check=False)
        except (subprocess.SubprocessError, OSError):
            pass

    @staticmethod
    def _push_outcome(cp: subprocess.CompletedProcess) -> str:
        """Classify a failed ``git push``: a rejection because the remote moved is
        retryable (the cycle re-pulls and retries); anything else — bad URL, denied auth —
        will not fix on retry."""
        low = f"{cp.stderr or ''}\n{cp.stdout or ''}".lower()
        if "rejected" in low or "fetch first" in low or "non-fast-forward" in low:
            return "transient"
        return "permanent"

    def _push_refused(self, cp: subprocess.CompletedProcess, outcome: str) -> str:
        """What a ``git push`` the remote did not take says. The words follow what git said;
        a ``transient`` outcome (which the cycle retries) adds that it will try again."""
        words = _words(cp)
        low = words.lower()
        trouble = _remote_trouble(words)
        if any(needle in low for needle in _RULES):
            sentence = (
                "Git Sync couldn't push to the git remote: its rules don't let this machine push "
                f"to branch '{self._branch}'. Allow that on the remote, or set Branch "
                f"{_ON_CARD} to one that does."
            )
        elif "fetch first" in low or "non-fast-forward" in low:
            sentence = (
                "Git Sync's push was turned away because the git remote has commits this "
                "machine hasn't pulled — another machine pushed first."
            )
        elif trouble:
            sentence = f"Git Sync couldn't push to the git remote: {trouble}"
        else:
            sentence = (
                "Git Sync couldn't push to the git remote. Check Git remote URL and Branch "
                f"{_ON_CARD}, and that the remote accepts pushes from this machine."
            )
        if outcome == "transient":
            sentence = f"{sentence} {_RETRIES}"
        return sentence_with_detail(sentence, words)

    def _clone_unwritable(self) -> str:
        """What the working clone's folder refusing a write says."""
        return (
            f"Git Sync isn't allowed to write to its working clone at {self._clone}. Fix that "
            f"folder's permissions, or set Local working clone {_ON_CARD} to a folder "
            "PersonalClaw can write to."
        )

    def _clone_disk_full(self) -> str:
        """What a full disk under the working clone says."""
        return (
            f"The disk holding Git Sync's working clone at {self._clone} is full. Free some space."
        )

    def _cycle_stopped(self, failure: BaseException) -> str:
        """What a push cycle that git or the working clone stopped part-way says. Every such
        failure is a ``transient`` outcome the cycle retries, so each says so."""
        words = _words(failure)
        low = words.lower()
        if _git_unrunnable(failure):
            sentence = _NO_GIT
        elif isinstance(failure, subprocess.TimeoutExpired):
            step = _subcommand(failure.cmd)
            if step in ("clone", "push"):
                sentence = (
                    f"git {step} didn't finish within {_GIT_TIMEOUT} seconds, so this sync "
                    "stopped. If it keeps timing out, check that the git remote is reachable from "
                    "this machine, and that git reaches it without stopping to ask for anything."
                )
            else:
                sentence = (
                    f"git {step} didn't finish within {_GIT_TIMEOUT} seconds in the working clone "
                    f"at {self._clone}, so this sync stopped. If it keeps happening, run git "
                    f"{step} there from a terminal to see what it waits for."
                )
        elif isinstance(failure, subprocess.CalledProcessError):
            step = _subcommand(failure.cmd)
            trouble = _remote_trouble(words) if step == "clone" else ""
            # A clone's "Permission denied" can be the REMOTE's (a local-path remote this
            # machine can't read), so for a clone only one naming the clone's own path is its.
            denied = any(needle in low for needle in _CLONE_DENIED) and (
                step != "clone" or self._clone in words
            )
            if trouble:
                sentence = f"Git Sync couldn't clone the git remote into {self._clone}: {trouble}"
            elif denied:
                sentence = self._clone_unwritable()
            elif "no space left on device" in low:
                sentence = self._clone_disk_full()
            elif step == "clone" and "already exists and is not an empty directory" in low:
                sentence = (
                    f"Git Sync couldn't clone the git remote into {self._clone}: that folder "
                    f"already has other files in it. Set Local working clone {_ON_CARD} to an "
                    "empty or new folder."
                )
            elif step == "clone":
                sentence = (
                    f"Git Sync couldn't clone the git remote into {self._clone}. Check Git remote "
                    f"URL {_ON_CARD}, and that git on this machine can reach it."
                )
            elif "index.lock" in low:
                sentence = (
                    f"Git Sync couldn't update its working clone at {self._clone}: another git "
                    "process is using it, or an interrupted one left .git/index.lock behind. Wait "
                    "for it to finish, or remove that file."
                )
            else:
                sentence = (
                    f"Git Sync couldn't update its working clone at {self._clone}: git {step} "
                    "failed there. Check that folder, and any git settings on this machine that "
                    "apply to it."
                )
        elif isinstance(failure, PermissionError):
            sentence = self._clone_unwritable()
        elif isinstance(failure, OSError) and failure.errno == errno.ENOSPC:
            sentence = self._clone_disk_full()
        else:
            sentence = (
                f"Git Sync couldn't use its working clone at {self._clone}. Check that folder."
            )
        return sentence_with_detail(f"{sentence} {_RETRIES}", words)

    @staticmethod
    def _unreadable(words: str) -> str:
        """What a ``git ls-remote`` probe that git answered with an error says."""
        trouble = _remote_trouble(words)
        sentence = (
            f"Git Sync couldn't read the git remote: {trouble}"
            if trouble
            else (
                f"Git Sync couldn't read the git remote. Check Git remote URL {_ON_CARD}, "
                "and that git on this machine can reach it."
            )
        )
        return sentence_with_detail(sentence, words)

    @staticmethod
    def _probe_stopped(failure: BaseException) -> str:
        """What a ``git ls-remote`` probe that could not run to completion says."""
        if _git_unrunnable(failure):
            sentence = _NO_GIT
        elif isinstance(failure, subprocess.TimeoutExpired):
            sentence = (
                f"Checking the git remote didn't finish within {_GIT_TIMEOUT} seconds. Check that "
                f"it is reachable from this machine, that Git remote URL {_ON_CARD} is right, "
                "and that git reaches it without stopping to ask for anything."
            )
        else:
            sentence = (
                "Git Sync couldn't run git to check the git remote. Check that git works in a "
                "terminal on this machine."
            )
        return sentence_with_detail(sentence, _words(failure))

    # ── SyncTransportProvider contract ───────────────────────────────────────────────

    def push(self, objects: list[SyncObject]) -> PushResult:
        if self._idle:
            return PushResult(outcome="transient", detail="no git remote configured")
        pushed = skipped = 0
        try:
            self._ensure_clone()
            # Pull first so a push does not conflict with others' objects. A pull failure on
            # a fresh/empty remote is fine — keep going and let the push create the branch.
            self._pull_ff()
            for obj in objects:
                target = self._resolve(obj.key)
                # Insert-only: a key already present is skipped, never overwritten, so a
                # retried push is free and the git history stays append-only per object.
                if os.path.exists(target):
                    skipped += 1
                    continue
                os.makedirs(os.path.dirname(target) or self._clone, exist_ok=True)
                with open(target, "wb") as fh:
                    fh.write(obj.data)
                pushed += 1
            self._git("add", "-A")
            # Nothing staged (all skipped, or empty push) → delivered with pushed=0.
            if not self._git("status", "--porcelain").stdout.strip():
                return PushResult(pushed=pushed, skipped=skipped, outcome="delivered")
            self._commit(f"sync: {pushed} objects")
            push_cp = self._git("push", "origin", self._branch, check=False)
            if push_cp.returncode != 0:
                outcome = self._push_outcome(push_cp)
                return PushResult(
                    pushed=pushed,
                    skipped=skipped,
                    outcome=outcome,
                    detail=self._push_refused(push_cp, outcome),
                )
            return PushResult(pushed=pushed, skipped=skipped, outcome="delivered")
        except (subprocess.SubprocessError, OSError) as e:
            # Clone/pull/commit blew up mid-cycle — retryable.
            return PushResult(
                pushed=pushed, skipped=skipped, outcome="transient", detail=self._cycle_stopped(e)
            )

    def list_remote(self, prefix: str = "") -> list[RemoteRef]:
        # A missing or unclonable remote is an empty remote, not an error — the clone may
        # not exist yet on a fresh machine.
        if self._idle:
            return []
        try:
            self._ensure_clone()
        except (subprocess.SubprocessError, OSError):
            return []
        self._pull_ff()  # best-effort refresh; internally safe
        if not os.path.isdir(self._clone):
            return []
        refs: list[RemoteRef] = []
        for dirpath, dirnames, filenames in os.walk(self._clone):
            # Prune the entire .git tree — its objects are git's bookkeeping, never a shard.
            if ".git" in dirnames:
                dirnames.remove(".git")
            for fn in filenames:
                if fn.startswith(_TMP_PREFIX):
                    continue
                full = os.path.join(dirpath, fn)
                # Key is the path relative to the clone, always in posix form.
                key = os.path.relpath(full, self._clone).replace(os.sep, "/")
                if key == ".git" or key.startswith(".git/"):
                    continue  # defensive — pruned above, but never advertise git internals
                if not key.startswith(prefix):
                    continue
                try:
                    st = os.stat(full)
                except OSError:
                    continue  # vanished between walk and stat — skip it
                # mtime is a cheap change fingerprint; the cycle only compares it, never
                # parses it, so mtime is enough and avoids a git blob-hash per file.
                refs.append(
                    RemoteRef(key=key, size=st.st_size, fingerprint=str(int(st.st_mtime)))
                )
        return refs

    def pull(self, refs: list[RemoteRef]) -> list[SyncObject]:
        if self._idle:
            return []
        # The clone is already current from list_remote's pull; refresh best-effort if it
        # exists, but never establish it here.
        if os.path.isdir(os.path.join(self._clone, ".git")):
            self._pull_ff()
        out: list[SyncObject] = []
        for ref in refs:
            try:
                with open(self._resolve(ref.key), "rb") as fh:
                    out.append(SyncObject(key=ref.key, data=fh.read()))
            except OSError:
                # A ref the clone no longer has (or can't read) is dropped, not raised —
                # the caller reconciles against what it asked for.
                continue
        return out

    def cas_registry(self, expected_sha: str | None, data: bytes) -> bool:
        if self._idle:
            return False
        try:
            self._ensure_clone()
            self._pull_ff()
            target = self._resolve(_REGISTRY_KEY)
            if os.path.exists(target):
                with open(target, "rb") as fh:
                    current = fh.read()
                # Present: swap only if the caller's expected sha matches what's there (a
                # None expectation means "expected absent", which a present file fails).
                if expected_sha != hashlib.sha256(current).hexdigest():
                    return False
            elif expected_sha is not None:
                # Absent: only a None expectation ("expected absent") may proceed.
                return False
            os.makedirs(os.path.dirname(target) or self._clone, exist_ok=True)
            with open(target, "wb") as fh:
                fh.write(data)
            self._git("add", _REGISTRY_KEY)
            # Identical bytes already committed → the desired state is present, no swap.
            if not self._git("status", "--porcelain").stdout.strip():
                return True
            self._commit("sync: registry")
            # git's own push rejection IS the compare-and-swap: if the remote moved under
            # us the push is rejected and we report the lost race for the caller to retry.
            return self._git("push", "origin", self._branch, check=False).returncode == 0
        except (subprocess.SubprocessError, OSError):
            return False

    def test(self) -> ConnectionResult:
        if not self._repo_url:
            return ConnectionResult(ok=False, detail="no git remote configured")
        try:
            # ``ls-remote`` against the URL confirms both reachability and auth without the
            # side effect of writing a clone during a read-only probe.
            cp = self._run(["ls-remote", self._repo_url], check=False)
            if cp.returncode == 0:
                return ConnectionResult(ok=True, detail=f"git remote reachable: {self._repo_url}")
            return ConnectionResult(ok=False, detail=self._unreadable(_words(cp)))
        except (subprocess.SubprocessError, OSError) as e:
            return ConnectionResult(ok=False, detail=self._probe_stopped(e))


def create_provider(config: dict[str, Any] | None = None) -> GitSyncProvider:
    """Extension factory — builds the git-sync transport from user settings."""
    config = config or {}
    return GitSyncProvider(
        repo_url=str(config.get("repo_url", "") or ""),
        local_clone=str(config.get("local_clone", "") or "~/.personalclaw/sync/git-sync"),
        branch=str(config.get("branch", "") or "main"),
    )
