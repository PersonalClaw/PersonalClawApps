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

A lost race never leaves the working clone behind the remote. Every call starts by catching
up — fetching the remote and replaying this machine's unpushed commits on top of it — and a
push the remote turns away because another machine pushed first catches up again and pushes
once more, so it lands in the same call. A registry write the remote turned away is dropped
from the clone rather than kept: it lost its swap, and the caller re-reads the remote's.
"""

import contextlib
import errno
import hashlib
import os
import re
import subprocess
from typing import Any

from personalclaw.sdk.git import (
    GitTooOld,
    git_argv,
    git_env,
    remote_refusal,
    talks_to_remote,
    transport_refusal,
)
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
#: The ``-c`` flags that put that identity on a commit — the transport's own, and every one a
#: catch-up replays (a rebase re-commits, so it needs a committer too).
_IDENTITY = ("-c", f"user.name={_COMMIT_NAME}", "-c", f"user.email={_COMMIT_EMAIL}")

#: How many times one push goes to the remote. After the first, each try follows a catch-up that
#: took in everything the remote had a moment before, so running out means other machines kept
#: pushing in that moment — the next sync tries again, from a clone that is not stuck.
_PUSH_TRIES = 3

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
# What the remote says when it turns a push away for good for a reason of its own — each said
# as what it is, with its own next step, and none of them changed by trying again: a repository
# with the branch checked out in a working tree (not a bare one), a push from a shallow clone,
# a repository it can't write the objects into, and a branch name it won't take.
_CHECKED_OUT = ("branch is currently checked out", "refusing to update checked out branch")
_SHALLOW = ("shallow update not allowed",)
_CANT_STORE = (
    "unpacker error",
    "unpack failed",
    "unable to migrate objects to permanent storage",
    "insufficient permission for adding an object",
    "unable to create temporary object directory",
)
_FUNNY_REF = ("funny ref",)
# What the remote says when the branch's ref lock is taken — another push landing at that moment,
# or a lock file an interrupted one left behind: retried, like a lost race.
_LOCKED = ("cannot lock ref", "failed to lock")
# What ``git push`` prints (untranslated) when the remote has commits this clone doesn't: a lost
# race, which a catch-up and another push recover from.
_RACE = ("fetch first", "non-fast-forward")
#: The refusals above that no retry changes, by the name ``_refusal`` gives each.
_FINAL = ("rules", "checked-out", "shallow", "cant-store", "funny-ref")
#: What git never allows in a ref name (``git check-ref-format``): a control character, space,
#: or one of ``~ ^ : ? * [ \``.
_REF_FORBIDDEN = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")
# ``git status --porcelain``'s states for a path a stopped rebase couldn't merge.
_UNMERGED = ("DD", "AU", "UD", "UA", "DU", "AA", "UU")
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
    # Past git's own leading options: `-C`/`-c` take the next argument, and the settings
    # `git_argv` puts in front of the subcommand start with `--no-pager`.
    while rest and rest[0].startswith("-"):
        rest = rest[2:] if rest[0] in ("-C", "-c") else rest[1:]
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


def _remote_wins(state: str, path: str) -> bool:
    """Whether a path a replay couldn't merge settles on the remote's copy: a key both sides
    added (insert-only — the remote had it first), or the registry (a write of it the remote
    doesn't have lost its compare-and-swap). ``_catch_up`` says why nothing else can be."""
    return state == "AA" or path == _REGISTRY_KEY


def _valid_branch(name: str) -> bool:
    """Whether git takes ``name`` as a branch name, by ``git check-ref-format --branch``'s rules:
    no leading ``-``, not ``HEAD``, none of the forbidden characters, no ``..`` or ``@{``, no
    trailing ``.``, and no empty part, part starting with ``.`` or part ending in ``.lock``."""
    if not name or name.startswith("-") or name == "HEAD":
        return False
    if _REF_FORBIDDEN.search(name) or ".." in name or "@{" in name or name.endswith("."):
        return False
    return all(
        part and not part.startswith(".") and not part.endswith(".lock")
        for part in name.split("/")
    )


def _branch_refusal(branch: str) -> str:
    """What a Branch git won't take as a branch name says, or ``""``. Checked before any git step
    runs: git itself says only ``invalid refspec``, at the push, after the objects were committed
    to whatever branch the clone had checked out."""
    if _valid_branch(branch):
        return ""
    return (
        f"Git Sync can't sync on '{branch}': git doesn't accept that as a branch name. Set Branch "
        f"{_ON_CARD} to one it does, such as main."
    )


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

    @property
    def _refused(self) -> str:
        """Why these settings can't be used, and what to set instead; ``""`` when they can. The
        configured remote may be one PersonalClaw's git doesn't reach (a local path, ``ext::``,
        ``git://``), or the Branch one git won't take as a branch name. Said before git runs, so
        the owner reads it beside the setting rather than as git's ``transport 'file' not
        allowed`` or ``invalid refspec``."""
        return remote_refusal(self._repo_url) or _branch_refusal(self._branch)

    def _resolve(self, key: str) -> str:
        """Map a remote-relative posix key to an absolute path inside the working clone."""
        # Split on "/" and rejoin with the OS separator so nested keys land in real
        # subdirectories regardless of platform.
        return os.path.join(self._clone, *key.split("/"))

    def _run(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess:
        """Run ``git <args>`` with output captured and a hard timeout. Not scoped to the
        clone — used for ``clone`` (the clone dir does not exist yet) and ``ls-remote``.

        An agent's shell can write the working clone's ``.git`` as easily as its files, so git
        runs with the settings that stop the repository's own configuration from running a
        program (``git_argv``): its ssh command and credential helpers are the owner's own,
        from their global configuration. The environment is the child allowlist, never the
        gateway's secrets, with the SSH agent for a command that talks to the remote
        (``git_env``)."""
        return subprocess.run(
            git_argv(args),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            check=check,
            env=git_env(remote=talks_to_remote(args)),
        )

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        """Run ``git -C <clone> <args>`` — every operation against the working clone."""
        return self._run(["-C", self._clone, *args], check=check)

    def _commit(self, message: str) -> None:
        """Commit the staged tree under the transport's own deterministic identity, set via
        ``-c`` so it never leans on (or pollutes) ambient git config."""
        self._git(*_IDENTITY, "commit", "-m", message)

    @property
    def _upstream(self) -> str:
        """The clone's copy of the remote's branch, as the last fetch saw it."""
        return f"refs/remotes/origin/{self._branch}"

    def _rev(self, ref: str) -> str | None:
        """The commit ``ref`` names in the working clone, or None when it names none (an unborn
        branch, a remote branch never fetched)."""
        cp = self._git("rev-parse", "--verify", "-q", ref, check=False)
        sha = (cp.stdout or "").strip()
        return sha if cp.returncode == 0 and sha else None

    def _ahead(self) -> bool:
        """Whether the clone holds commits the remote's branch doesn't — an earlier push that
        never landed, which the next push must carry."""
        head = self._rev("HEAD")
        if head is None:
            return False
        upstream = self._rev(self._upstream)
        if upstream is None:
            return True  # the remote has no such branch yet, so none of this is on it
        count = self._git("rev-list", "--count", f"{upstream}..{head}", check=False)
        return (count.stdout or "").strip() not in ("", "0")

    def _rebasing(self) -> bool:
        """Whether a rebase is stopped part-way in the working clone."""
        git_dir = os.path.join(self._clone, ".git")
        states = ("rebase-merge", "rebase-apply")
        return any(os.path.isdir(os.path.join(git_dir, state)) for state in states)

    def _catch_up(self) -> str:
        """Bring the working clone level with the remote's branch: fetch it, then replay this
        machine's commits that the remote doesn't have on top of it (``git rebase``).

        Returns "" once level — and also when the remote can't be fetched right now (a brand-new
        remote with no branch yet, an offline blip), since a stale clone still serves reads and a
        push that follows says what went wrong. Otherwise returns what a real conflict says.

        What a replay can conflict on follows from the layout. Every shard object has a key of
        its own, written once by the machine whose id it carries, so replaying one of this
        transport's commits can only meet a key the remote already has. With the same bytes git
        sees the change as already applied and drops it; with other bytes the remote's copy
        stays and this machine's is skipped — the insert-only rule every push follows. The
        shared ``registry.json`` is only ever compare-and-swapped, so a write of it the remote
        doesn't have lost its swap: the remote's copy stays there too, and core re-reads it.
        Anything else is a real conflict — a path that is a file on one side and a folder on the
        other, or a file changed on both sides by something other than this transport (a commit
        made by hand in the clone, a rewritten remote). Then the replay is undone, the clone
        keeps its commits for whoever resolves it, and the caller is told what to do.
        """
        if self._rebasing():
            # A replay an earlier run was stopped in the middle of (killed, or timed out):
            # undo it, so this one starts from the clone's own commits.
            self._git("rebase", "--abort", check=False)
        fetched = self._git(
            "fetch", "origin", f"+refs/heads/{self._branch}:{self._upstream}", check=False
        )
        if fetched.returncode != 0:
            return ""
        if self._rev("HEAD") is None:
            # Cloned while the remote was still empty, so the branch has no commits here yet:
            # take the remote's as they are.
            self._git("reset", "--hard", self._upstream, check=False)
            return ""
        count = self._git("rev-list", "--count", f"{self._upstream}..HEAD", check=False)
        commits = int((count.stdout or "").strip() or 0) if count.returncode == 0 else 0
        replay = self._git(*_IDENTITY, "rebase", self._upstream, check=False)
        stops = 0
        try:
            # Level with the remote once the replay ends — or when git wouldn't start it at all
            # (a change left uncommitted in the clone), which the push that follows commits.
            while replay.returncode != 0 and self._rebasing():
                unmerged = self._unmerged()
                real = [path for state, path in unmerged if not _remote_wins(state, path)]
                # Settling a stop moves the replay on to the next commit, so it stops at most
                # once per commit it carries; a stop with nothing to settle is git's own trouble.
                if real or not unmerged or stops == commits:
                    break
                stops += 1
                for _state, path in unmerged:
                    self._keep_remote(path)
                if self._git("diff", "--cached", "--quiet", "HEAD", check=False).returncode == 0:
                    # Keeping the remote's copies left this commit with nothing of its own.
                    replay = self._git("rebase", "--skip", check=False)
                else:
                    # Committing the settled commit here, under its own message and author,
                    # leaves ``--continue`` nothing to commit — so it never opens an editor.
                    self._git(*_IDENTITY, "commit", "-C", "REBASE_HEAD")
                    replay = self._git(*_IDENTITY, "rebase", "--continue", check=False)
            else:
                return ""
            self._git("rebase", "--abort", check=False)
        except BaseException:
            with contextlib.suppress(subprocess.SubprocessError, OSError):
                if self._rebasing():
                    self._git("rebase", "--abort", check=False)
            raise
        return self._replay_conflict(real, replay)

    def _unmerged(self) -> list[tuple[str, str]]:
        """Each path a stopped rebase couldn't merge, with git's two-letter state for it."""
        out = self._git(
            "status", "--porcelain", "-z", "--no-renames", "--untracked-files=no"
        ).stdout
        return [
            (entry[:2], entry[3:])
            for entry in (out or "").split("\0")
            if len(entry) > 3 and entry[:2] in _UNMERGED
        ]

    def _keep_remote(self, path: str) -> None:
        """Settle ``path`` on the remote's side. Mid-rebase, git's "ours" is the branch being
        replayed onto — the remote's."""
        if self._git("checkout", "--ours", "--", path, check=False).returncode == 0:
            self._git("add", "--", path)
        else:
            # The remote has no copy at all (it deleted the registry), so neither does the result.
            self._git("rm", "-q", "--force", "--", path)

    def _replay_conflict(self, paths: list[str], replay: subprocess.CompletedProcess) -> str:
        """What this machine's unpushed commits not going on top of the remote's says. git's own
        ``CONFLICT`` lines follow as the detail."""
        if paths:
            # A path that is a file on one side and a folder on the other is reported under a
            # name git made up for the side it moved aside ("machines~HEAD"); the path itself is
            # what to look at.
            path = paths[0].split("~", 1)[0]
            trouble = f"{path} was changed on both sides in ways git can't combine"
        else:
            trouble = "git stopped part-way through"
        sentence = (
            "Git Sync couldn't put this machine's unpushed commits on top of what the git remote "
            f"has: {trouble}. Run git pull --rebase origin {self._branch} in the working clone at "
            f"{self._clone} and resolve it there — or, if nothing there needs keeping, delete that "
            "folder and Git Sync clones the remote afresh on its next run."
        )
        words = " ".join(
            line[line.index("CONFLICT") :]
            for line in (replay.stdout or "").splitlines()
            if "CONFLICT" in line
        )
        return sentence_with_detail(sentence, words or _words(replay))

    def _refresh(self) -> None:
        """Best-effort catch-up for a read. A clone that can't catch up right now — offline, or
        a real conflict, which the next push reports — still serves what it has."""
        with contextlib.suppress(subprocess.SubprocessError, OSError):
            self._catch_up()

    def _landed(self, written: list[str], base: str | None) -> int:
        """How many of the objects this push wrote reached the remote as this machine's copy,
        when the push landed on top of ``base`` (the remote's branch just before it). A key the
        remote gained from another machine while this push was catching up kept that copy."""
        if base is None:
            return len(written)
        added = self._git(
            "diff", "--name-only", "-z", "--no-renames", "--diff-filter=A", base, "HEAD",
            check=False,
        )
        if added.returncode != 0:
            return len(written)  # the push landed either way; only the tally is unknown
        new = set((added.stdout or "").split("\0"))
        return sum(1 for key in written if key in new)

    def _undo_commit(self, before: str | None) -> None:
        """Take back the registry commit just made on top of ``before`` (None: the branch's
        first commit)."""
        if before is not None:
            self._git("reset", "--hard", before, check=False)
            return
        self._git("update-ref", "-d", f"refs/heads/{self._branch}", check=False)
        self._git("read-tree", "--empty", check=False)
        with contextlib.suppress(OSError):
            os.remove(self._resolve(_REGISTRY_KEY))

    def _restore_registry(self) -> None:
        """Put ``registry.json`` back as the clone's last commit has it — or gone, if none has."""
        if self._git("checkout", "HEAD", "--", _REGISTRY_KEY, check=False).returncode != 0:
            self._git("rm", "-q", "--cached", "--ignore-unmatch", "--", _REGISTRY_KEY, check=False)
            with contextlib.suppress(OSError):
                os.remove(self._resolve(_REGISTRY_KEY))

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

    @staticmethod
    def _refusal(cp: subprocess.CompletedProcess) -> str:
        """What kind of "no" a failed ``git push`` got, else ``""``. Final ones first — the
        remote's rules (a hook, branch protection), a checked-out branch, a shallow clone, a
        repository that can't store the push, a branch name it won't take — since a push turned
        away for any of them is no race, however its words read. Then the retried ones: the
        branch's ref lock taken (``"locked"``), and the remote having commits this clone doesn't
        (``"race"``)."""
        low = f"{cp.stderr or ''}\n{cp.stdout or ''}".lower()
        for kind, needles in (
            ("rules", _RULES),
            ("checked-out", _CHECKED_OUT),
            ("shallow", _SHALLOW),
            ("cant-store", _CANT_STORE),
            ("funny-ref", _FUNNY_REF),
            ("locked", _LOCKED),
            ("race", _RACE),
        ):
            if any(needle in low for needle in needles):
                return kind
        return ""

    @classmethod
    def _push_outcome(cls, cp: subprocess.CompletedProcess) -> str:
        """Classify a failed ``git push`` for the outbox. A refusal no retry changes — the
        remote's rules, a checked-out branch, a shallow clone, a repository that can't store the
        push, a branch name it won't take — is ``permanent``, like a bad URL or denied auth. A
        race still lost after every catch-up, a ref lock still taken, the network, or another
        refusal the remote may lift is ``transient``: the cycle tries again."""
        refusal = cls._refusal(cp)
        if refusal in _FINAL or transport_refusal(_words(cp)):
            # A remote PersonalClaw's git does not reach (a local path) is refused the same way
            # on every try.
            return "permanent"
        low = f"{cp.stderr or ''}\n{cp.stdout or ''}".lower()
        if refusal or "rejected" in low or any(n in low for n in _UNREACHABLE):
            return "transient"
        return "permanent"

    def _push_refused(self, cp: subprocess.CompletedProcess, outcome: str) -> str:
        """What a ``git push`` the remote did not take says. The words follow what git said;
        a ``transient`` outcome (which the cycle retries) adds that it will try again."""
        words = _words(cp)
        refused = transport_refusal(words)
        if refused:
            # The working clone's own remote is one PersonalClaw's git does not reach (a local
            # path). Retrying cannot change that, and the outcome is permanent.
            return sentence_with_detail(refused, words)
        trouble = _remote_trouble(words)
        refusal = self._refusal(cp)
        branch = self._branch
        if refusal == "rules":
            sentence = (
                "Git Sync couldn't push to the git remote: its rules don't let this machine push "
                f"to branch '{branch}'. Allow that on the remote, or set Branch "
                f"{_ON_CARD} to one that does."
            )
        elif refusal == "checked-out":
            sentence = (
                "Git Sync couldn't push to the git remote: it is a repository with a working tree "
                f"that has branch '{branch}' checked out, and git won't push into a checked-out "
                f"branch. Point Git remote URL {_ON_CARD} at a bare repository (one made with "
                "git init --bare), or set Branch to one that isn't checked out there."
            )
        elif refusal == "shallow":
            sentence = (
                f"Git Sync couldn't push to the git remote: the working clone at {self._clone} is "
                "shallow — it holds only part of its history — and the remote won't take a push "
                "from a shallow clone. If nothing there needs keeping, delete that folder, and "
                "Git Sync clones the remote in full on its next run."
            )
        elif refusal == "cant-store":
            sentence = (
                "Git Sync couldn't push to the git remote: the remote couldn't store what this "
                "machine sent — the repository there isn't writable by the account this machine "
                "pushes as, or the disk it is on is full. Fix that on the remote."
            )
        elif refusal == "funny-ref":
            sentence = (
                f"Git Sync couldn't push to the git remote: it won't take '{branch}' as the name "
                f"of a branch. Set Branch {_ON_CARD} to a name it accepts, such as main."
            )
        elif refusal == "locked":
            sentence = (
                f"Git Sync couldn't push to the git remote: branch '{branch}' there was still "
                f"locked by another git process after {_PUSH_TRIES} tries — a push landing at the "
                f"same moment, or an interrupted one that left refs/heads/{branch}.lock behind. If "
                "it keeps happening, remove that file in the remote repository."
            )
        elif refusal == "race":
            sentence = (
                f"Git Sync couldn't push to the git remote: each of the {_PUSH_TRIES} times it "
                "caught up with the remote and pushed, another push had reached it first."
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
        if isinstance(failure, GitTooOld):
            # PersonalClaw's git will not run a git this old, and says what it needs, what it
            # found and what to do: that is the whole sentence.
            return f"{failure} {_RETRIES}"
        words = _words(failure)
        low = words.lower()
        if _git_unrunnable(failure):
            sentence = _NO_GIT
        elif isinstance(failure, subprocess.TimeoutExpired):
            step = _subcommand(failure.cmd)
            if step in ("clone", "fetch", "push"):
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
        elif isinstance(failure, subprocess.CalledProcessError) and transport_refusal(words):
            sentence = transport_refusal(words)
        elif isinstance(failure, subprocess.CalledProcessError):
            step = _subcommand(failure.cmd)
            trouble = _remote_trouble(words) if step == "clone" else ""
            # A clone's "Permission denied" can be the REMOTE's (a path on the remote host this
            # machine's login can't read), so for a clone only one naming the clone's own path
            # is its.
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
        refused = transport_refusal(words)
        if refused:
            return sentence_with_detail(refused, words)
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
        if isinstance(failure, GitTooOld):
            return str(failure)
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
        if self._refused:
            return PushResult(outcome="permanent", detail=self._refused)
        pushed = skipped = 0
        written: list[str] = []
        try:
            self._ensure_clone()
            # Catch up first, so the objects are checked against — and committed on top of —
            # everything the remote has. A brand-new empty remote has nothing to catch up with;
            # the push below creates its branch.
            conflict = self._catch_up()
            if conflict:
                return PushResult(outcome="permanent", detail=conflict)
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
                written.append(obj.key)
            pushed = len(written)
            self._git("add", "-A")
            if self._git("status", "--porcelain").stdout.strip():
                self._commit(f"sync: {pushed} objects")
            elif not self._ahead():
                # Nothing new, and nothing of this machine's the remote lacks → delivered.
                return PushResult(pushed=pushed, skipped=skipped, outcome="delivered")
            for attempt in range(1, _PUSH_TRIES + 1):
                base = self._rev(self._upstream)
                push_cp = self._git("push", "origin", self._branch, check=False)
                if push_cp.returncode == 0:
                    if attempt > 1:
                        # Caught up in between: a key another machine pushed meanwhile kept its
                        # copy, and counts as skipped.
                        pushed = self._landed(written, base)
                        skipped = len(objects) - pushed
                    return PushResult(pushed=pushed, skipped=skipped, outcome="delivered")
                if attempt == _PUSH_TRIES or self._refusal(push_cp) not in ("race", "locked"):
                    break
                # Lost the race: another machine pushed between the catch-up and this push (or
                # was pushing at that moment, holding the branch's lock). Catch up again — this
                # commit goes on top of theirs — and push once more.
                conflict = self._catch_up()
                if conflict:
                    return PushResult(pushed=pushed, skipped=skipped, outcome="permanent",
                                      detail=conflict)
            outcome = self._push_outcome(push_cp)
            return PushResult(
                pushed=pushed,
                skipped=skipped,
                outcome=outcome,
                detail=self._push_refused(push_cp, outcome),
            )
        except (subprocess.SubprocessError, OSError) as e:
            # Clone/pull/commit blew up mid-cycle — retryable.
            return PushResult(
                pushed=pushed, skipped=skipped, outcome="transient", detail=self._cycle_stopped(e)
            )

    def list_remote(self, prefix: str = "") -> list[RemoteRef]:
        # A missing or unclonable remote is an empty remote, not an error — the clone may
        # not exist yet on a fresh machine.
        if self._idle or self._refused:
            return []
        try:
            self._ensure_clone()
        except (subprocess.SubprocessError, OSError):
            return []
        self._refresh()  # best-effort; internally safe
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
        if self._idle or self._refused:
            return []
        # The clone is already current from list_remote's catch-up; refresh best-effort if it
        # exists, but never establish it here.
        if os.path.isdir(os.path.join(self._clone, ".git")):
            self._refresh()
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
        if self._idle or self._refused:
            return False
        before: str | None = None
        wrote = committed = False
        try:
            self._ensure_clone()
            self._catch_up()
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
            before = self._rev("HEAD")
            os.makedirs(os.path.dirname(target) or self._clone, exist_ok=True)
            wrote = True
            with open(target, "wb") as fh:
                fh.write(data)
            self._git("add", _REGISTRY_KEY)
            if self._git("status", "--porcelain").stdout.strip():
                self._commit("sync: registry")
                committed = True
            elif not self._ahead():
                # Identical bytes already committed and on the remote → the desired state is
                # present, no swap.
                return True
            # git's own push rejection IS the compare-and-swap: if the remote moved under
            # us the push is rejected and we report the lost race for the caller to retry.
            if self._git("push", "origin", self._branch, check=False).returncode == 0:
                return True
        except (subprocess.SubprocessError, OSError):
            pass
        # The write never reached the remote, so it mustn't stay in the clone either: the
        # caller's re-read would find these bytes instead of the remote's, and a later push
        # would carry them onto whatever registry the remote has by then — a write with no
        # compare at all.
        with contextlib.suppress(subprocess.SubprocessError, OSError):
            if committed:
                self._undo_commit(before)
            elif wrote:
                self._restore_registry()
        return False

    def test(self) -> ConnectionResult:
        if not self._repo_url:
            return ConnectionResult(ok=False, detail="no git remote configured")
        if self._refused:
            return ConnectionResult(ok=False, detail=self._refused)
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
