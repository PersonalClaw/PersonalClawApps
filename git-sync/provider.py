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
no subprocess error ever leaves a contract method as itself, since its text names git's whole
command line, Git remote URL and any credential in it included: a push says what went wrong
in its result, and a listing, a read or a registry swap raises :class:`GitSyncFailed` saying
it. Git Sync commits into, and pushes from, only a clone it made itself.

A lost race never leaves the working clone behind the remote. Every call starts by catching
up — fetching the remote and replaying this machine's unpushed commits on top of it — and a
push the remote turns away because another machine pushed first catches up again and pushes
once more, so it lands in the same call. A registry write the remote turned away is dropped
from the clone rather than kept: it lost its swap, and the caller re-reads the remote's.
"""

import contextlib
import errno
import hashlib
import ipaddress
import os
import re
import shutil
import stat
import subprocess
import tempfile
from typing import Any
from urllib.parse import urlsplit

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

#: What the sync layer puts at the top of a sync root: the registry, the encryption salt, and each
#: machine's shards under ``machines/``. A working clone Git Sync made holds nothing else.
_SYNC_ROOT = frozenset({_REGISTRY_KEY, "encryption-salt", "machines"})

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
# A branch the remote hides from pushes (``receive.hideRefs``), and a repository missing objects
# its own branch needs — damaged, so it can't connect anything a push sends to what it holds.
_HIDDEN = ("deny updating a hidden ref",)
_DAMAGED = ("missing necessary objects",)
# git keeps branch names as paths, so "sync" can't sit beside a "sync/main" the remote has.
_CLASH = ("refname conflict", "exists; cannot create")
# A ref the remote could not update is said on its own error line, "cannot lock ref '…': <cause>"
# (the bracketed reason's words differ between git versions), so the cause decides what it was:
# the ref store not writable, a lock file in the way, or the ref moved under this push.
_REF_LOCK = "cannot lock ref"
_REF_UNWRITABLE = ("permission denied", "read-only file system", "no space left on device")
_REF_LOCK_TAKEN = "file exists"
_REF_NOT_UPDATED = ("failed to update ref",)
# What the remote says when the branch's ref lock is taken — another push landing at that moment,
# or a lock file an interrupted one left behind: retried, like a lost race.
_LOCKED = ("failed to lock",)
# What ``git push`` prints (untranslated) when the remote has commits this clone doesn't, or when
# another push moved the branch between the remote advertising it and updating it: a lost race,
# which a catch-up and another push recover from.
_RACE = (
    "fetch first",
    "non-fast-forward",
    "but expected",
    "incorrect old value provided",
    "reference already exists",
    "reference does not exist",
)
#: The refusals above that no retry changes, by the name ``_refusal`` gives each.
_FINAL = (
    "rules", "checked-out", "shallow", "hidden", "damaged", "cant-store", "clash", "funny-ref",
)
#: git's words for a branch the remote has that another branch's name nests with, naming it.
_CLASHING = re.compile(r"'refs/heads/([^']+)' exists; cannot create 'refs/heads/")
#: git's words for a lock file it found in its way, naming the file.
_LOCK_FILE = re.compile(r"unable to create '([^']+?\.lock)': file exists", re.IGNORECASE)
#: Set in the configuration of every working clone Git Sync makes — ``git clone -c`` sets it before
#: anything is fetched — so a clone of its own is told from a folder of someone else's work that
#: Local working clone was pointed at, whatever remote that folder has.
_MARK = "personalclaw.gitSyncClone"
#: The keys a working clone's own configuration is read for: the ones that decide where a fetch or
#: push goes, and :data:`_MARK`.
_CLONE_KEYS = (
    r"^(remote\.origin\.(url|pushurl)|url\..*\.(insteadof|pushinsteadof)"
    r"|personalclaw\.gitsyncclone)$"
)
#: What a fetch says when the remote has no such branch yet: a brand-new remote, or one with other
#: branches only. English whatever the owner's locale (``git_env``).
_NO_BRANCH_YET = "couldn't find remote ref"
#: What each working clone's history was last found to hold, so the look at a folder walks it
#: once rather than on every call (``GitSyncProvider._commits_git_syncs``): by folder and walk,
#: the refs it was walked at, and whether every commit it met was Git Sync's.
_WALKED: dict[tuple[str, bool], tuple[str, bool]] = {}
#: A URL's authority up to its last ``@``: the ``user:password@``, or ``token@``, it can carry.
_USERINFO = re.compile(r"://[^/?#]*@")
#: The user name Access token signs in with when User name is empty. git asks for one along
#: with a token, and fails without one, since nobody is there to type it. Hosts differ in what
#: they want: GitHub takes this name with any token and needs it with an app's, and a host that
#: checks only the token takes any name. A host that wants the account's own name beside its
#: token needs User name set.
_TOKEN_USERNAME = "x-access-token"
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


def _shown(url: str) -> str:
    """``url`` as a sentence may show it: without the user name and password, or the token, it
    carries — ``https://<token>@host/…`` is a common way to give git one, and then the token is
    the whole of what comes before the ``@``. A query or fragment, which can carry one too, is
    left off as well. An scp-like ``user@host:path`` loses its ``user@``, so nothing written
    before the host is ever shown, whichever way the URL is written."""
    if "://" in url:
        return re.split(r"[?#]", _USERINFO.sub("://", url, count=1), maxsplit=1)[0]
    head, slash, path = url.partition("/")
    return head.rsplit("@", 1)[-1] + slash + path


def _bare_url(url: str) -> str:
    """``url`` without the ``<transport>::`` a remote helper's URL can start with."""
    head = url.split("/", 1)[0]
    return url.split("::", 1)[1] if "::" in head else url


def _web(url: str) -> bool:
    """Whether git reaches ``url`` over http or https."""
    scheme = re.match(r"([A-Za-z][A-Za-z0-9+.-]*)://", _bare_url(url))
    return bool(scheme) and scheme.group(1).lower() in ("http", "https")


def _takes_token(url: str) -> bool:
    """Whether Access token may be handed to git for ``url``: an https remote, or an http one
    on this machine. A plain http remote anywhere else would get it unencrypted."""
    if not _web(url):
        return False
    try:
        parts = urlsplit(_bare_url(url))
        host = parts.hostname or ""
    except ValueError:  # a malformed address git would refuse anyway
        return False
    if parts.scheme.lower() == "https":
        return True
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _scrub(git_dir: str, needles: list[tuple[bytes, bytes]]) -> None:
    """Replace each ``(copy, replacement)`` in every file under ``git_dir`` — its configuration,
    its fetch record, its logs — but its objects and its index, which never hold a remote's URL.
    A file is rewritten whole, with its mode kept, and put in place in one step."""
    for dirpath, dirnames, filenames in os.walk(git_dir):
        if dirpath == git_dir:
            dirnames[:] = [name for name in dirnames if name != "objects"]
        for filename in filenames:
            path = os.path.join(dirpath, filename)
            if (dirpath == git_dir and filename == "index") or os.path.islink(path):
                continue
            with open(path, "rb") as fh:
                data = fh.read()
            scrubbed = data
            for copy, replacement in needles:
                scrubbed = scrubbed.replace(copy, replacement)
            if scrubbed == data:
                continue
            fd, part = tempfile.mkstemp(prefix=f".{filename}-", dir=dirpath)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(scrubbed)
                os.chmod(part, stat.S_IMODE(os.stat(path).st_mode))
                os.replace(part, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.remove(part)
                raise


def _reraise(error: OSError) -> None:
    """``os.walk``'s error hook that stops the walk on a folder it couldn't read, rather than
    leaving that folder's files out without a word."""
    raise error


def _as_failure(cp: subprocess.CompletedProcess) -> subprocess.CalledProcessError:
    """``cp``, a git run that failed, as the error a checked run raises."""
    return subprocess.CalledProcessError(
        cp.returncode, cp.args, output=cp.stdout, stderr=cp.stderr
    )


#: Said when git itself could not be started.
_NO_GIT = (
    "Git Sync runs the git command, and PersonalClaw couldn't start it on this machine: it "
    "isn't installed, isn't on the PATH PersonalClaw runs with, or isn't executable. Install "
    "git where PersonalClaw can run it."
)


class GitSyncFailed(RuntimeError):
    """A step Git Sync can't go on from, said as what is wrong and what to do: its text is that
    sentence, with git's words, masked, as its detail — never a failed git run's own text, which
    names its whole command line. Raised for a folder Git Sync won't sync through, and by a
    listing, a read or a registry swap that couldn't be made: an empty listing, a dropped object
    or ``False`` would each say something that isn't so."""


class GitSyncProvider(SyncTransportProvider):
    """A durability sync transport backed by a git remote the user owns."""

    name = "git-sync"
    display_name = "Git Sync"

    def __init__(
        self,
        repo_url: str = "",
        local_clone: str = "~/.personalclaw/sync/git-sync",
        branch: str = "main",
        token: str = "",
        username: str = "",
    ) -> None:
        self._repo_url = repo_url or ""
        # Expand ``~`` and ``$VARS`` so a configured "~/.personalclaw/sync/git-sync" or
        # "$HOME/sync" resolves to a real path. An empty clone path leaves the transport
        # idle rather than crashing.
        self._clone = os.path.expandvars(os.path.expanduser(local_clone)) if local_clone else ""
        self._branch = branch or "main"
        # Access token (a sensitive setting, so it is kept in the credential store) and the user
        # name it signs in with. Pasted text can end in a newline; a token never does.
        self._token = (token or "").strip()
        self._username = (username or "").strip()

    # ── internal helpers ─────────────────────────────────────────────────────────────

    @property
    def _idle(self) -> bool:
        """No remote (or nowhere to clone it) → the transport is idle, not broken."""
        return not self._repo_url or not self._clone

    @property
    def _refused(self) -> str:
        """Why these settings can't be used, and what to set instead; ``""`` when they can. The
        configured remote may be one PersonalClaw's git doesn't reach (a local path, ``ext::``,
        ``git://``), an http one with a user name or token written into it, or one Access token
        can't be handed to, or the Branch one git won't take as a branch name. Said before git
        runs, so the owner reads it beside the setting rather than as git's ``transport 'file'
        not allowed`` or ``invalid refspec``."""
        return (
            remote_refusal(self._repo_url)
            or self._credential_in_url()
            or self._token_refused()
            or _branch_refusal(self._branch)
        )

    def _credential_in_url(self) -> str:
        """Why Git Sync won't use an http(s) Git remote URL with a user name or token written
        into it, or ``""``. git would keep it in the working clone's ``.git/config`` and pass
        it on its command line, where anyone on this machine can read it."""
        if not _web(self._repo_url) or not _USERINFO.search(self._repo_url):
            return ""
        return (
            "Git Sync won't use a user name or token written into Git remote URL: git keeps it "
            "in the working clone's .git/config and shows it on its command line to anyone on "
            f"this machine. Set Git remote URL {_ON_CARD} to the address without it "
            f"({_shown(self._repo_url)}), and put the token in Access token — and the user name "
            "in User name, if your host wants one."
        )

    def _token_refused(self) -> str:
        """Why Access token can't be handed to git for Git remote URL, or ``""``: a plain http
        remote on another machine would get it unencrypted, and a line break or a NUL would be
        read by git as more than one answer. Only an http(s) remote is ever given the token."""
        if not self._token or not _web(self._repo_url):
            return ""
        if not _takes_token(self._repo_url):
            host = urlsplit(_bare_url(self._repo_url)).hostname or "the remote"
            return (
                f"Git Sync won't send Access token over http, which would carry it unencrypted "
                f"to {host}. Set Git remote URL {_ON_CARD} to the remote's https address."
            )
        for label, value in (("Access token", self._token), ("User name", self._username)):
            if any(bad in value for bad in ("\n", "\r", "\0")):
                return (
                    f"{label} {_ON_CARD} has a line break or a NUL in it, which git would read "
                    f"as more than one answer. Enter it again, on its own."
                )
        return ""

    @property
    def _sign_in(self) -> tuple[str, str]:
        """The token and the user name git signs in to Git remote URL with, or ``("", "")`` for
        the owner's own sign-in — their ssh key, their credential helpers. Only an http(s)
        remote gets the token; ssh never does."""
        if not self._token or not _takes_token(self._repo_url):
            return "", ""
        return self._token, self._username or _TOKEN_USERNAME

    def _detail(self, sentence: str, words: str) -> str:
        """``sentence``, then git's ``words`` as its detail — every detail Git Sync gives is made
        here. Git remote URL can carry a credential, and git's words can carry the URL: a failed
        clone or probe names its whole command line. The detail is redacted on its way out, but
        that finds a credential in a URL only after ``://``, so first any copy of the URL in the
        words is replaced by the URL as :func:`_shown` shows it — an scp-like
        ``user:password@host:path`` included."""
        url = self._repo_url
        if url and url in words:
            words = words.replace(url, _shown(url))
        return sentence_with_detail(sentence, words)

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
        from their global configuration. With Access token set for an http(s) remote, a command
        that talks to it signs in with that token instead: git's one credential helper hands it
        over from that command's environment, and none of the owner's runs, so the token is on
        no command line and in no file. The environment is the child allowlist, never the
        gateway's secrets, with the SSH agent for a command that talks to the remote
        (``git_env``) — and without the writes git makes only along the way
        (``GIT_OPTIONAL_LOCKS=0``: ``git status`` doesn't refresh the index), so a look at a
        folder that turns out not to be Git Sync's leaves it byte-for-byte as it was. Every
        write Git Sync itself makes takes its lock whatever that says."""
        token, username = self._sign_in
        env = git_env(remote=talks_to_remote(args), token=token, username=username)
        env["GIT_OPTIONAL_LOCKS"] = "0"
        return subprocess.run(
            git_argv(args, token=bool(token)),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            check=check,
            env=env,
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

    def _holds(self, ref: str) -> bool:
        """Whether the working clone's ``HEAD`` holds every commit ``ref`` names."""
        cp = self._git("merge-base", "--is-ancestor", ref, "HEAD", check=False)
        return cp.returncode == 0

    def _rebasing(self) -> bool:
        """Whether a rebase is stopped part-way in the working clone."""
        git_dir = os.path.join(self._clone, ".git")
        states = ("rebase-merge", "rebase-apply")
        return any(os.path.isdir(os.path.join(git_dir, state)) for state in states)

    def _catch_up(self, *, pushing: bool = False) -> str:
        """Bring the working clone level with the remote's branch: fetch it, then replay this
        machine's commits that the remote doesn't have on top of it (``git rebase``).

        Returns "" once level — and when the remote has no such branch yet (a brand-new remote,
        or one with other branches only), since then there is nothing to catch up with. A fetch
        that fails for any other reason raises, as the failed git run, so the caller says what
        stopped it — and so does a replay git won't start (a change left uncommitted in the
        clone) while the remote has commits the clone doesn't: a read or a registry swap against
        a clone that isn't level would serve, or swap against, what the remote had before.
        Unless ``pushing``: the push that follows commits that change, reaches the remote itself
        and says what went wrong in its own words. Otherwise returns what a real conflict says.

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

        A lock file an interrupted git left in the clone is neither: it stops every fetch, and
        every replay's last step, until it goes. That raises, as the failed git run, so the
        caller says which file it is — a push's too.
        """
        if self._rebasing():
            # A replay an earlier run was stopped in the middle of (killed, or timed out):
            # undo it, so this one starts from the clone's own commits.
            self._git("rebase", "--abort", check=False)
        fetched = self._git(
            "fetch", "origin", f"+refs/heads/{self._branch}:{self._upstream}", check=False
        )
        if fetched.returncode != 0:
            words = _words(fetched)
            if not self._stale_lock(words) and (pushing or _NO_BRANCH_YET in words.lower()):
                return ""
            raise _as_failure(fetched)
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
                if replay.returncode != 0 and self._stale_lock(_words(replay)):
                    raise _as_failure(replay)
                if replay.returncode != 0 and not pushing and not self._holds(self._upstream):
                    # git wouldn't start the replay (a change left uncommitted in the clone),
                    # and the remote has commits this clone doesn't: not level, so not readable.
                    raise _as_failure(replay)
                return ""
            self._git("rebase", "--abort", check=False)
        except BaseException:
            with contextlib.suppress(subprocess.SubprocessError, OSError):
                if self._rebasing():
                    self._git("rebase", "--abort", check=False)
            raise
        if not real and self._stale_lock(_words(replay)):
            # Nothing to settle by hand: the replay's last step couldn't write the branch.
            raise _as_failure(replay)
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
        return self._detail(sentence, words or _words(replay))

    def _level(self) -> None:
        """Catch the working clone up for a read or a registry swap, or raise
        :class:`GitSyncFailed` saying what stopped it — the remote out of reach, a lock file in
        the clone, a real conflict. A clone that isn't level would be served as the remote, or
        swapped against as if it were."""
        try:
            conflict = self._catch_up()
        except (subprocess.SubprocessError, OSError) as e:
            raise GitSyncFailed(self._cycle_stopped(e)) from None
        if conflict:
            raise GitSyncFailed(conflict)

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

    def _clone_config(self) -> list[tuple[str, str]]:
        """The working clone's own settings that say where its fetches and pushes go, and whether
        Git Sync made it, as ``(key, value)`` with each key lower-cased as git names it. They live
        in its ``.git/config``, which the owner changing Git remote URL never touched, and which
        anything that can write the clone can change."""
        cp = self._git(
            "config", "--local", "--includes", "--null", "--get-regexp", _CLONE_KEYS, check=False
        )
        if cp.returncode not in (0, 1):  # 1: none of them is set
            raise _as_failure(cp)
        said = [entry.partition("\n") for entry in (cp.stdout or "").split("\0") if entry]
        return [(key.lower(), value) for key, _, value in said]

    def _commits_git_syncs(self, *, since_remote: bool) -> bool:
        """Whether every commit in the working clone was made under the transport's own identity
        — or, ``since_remote``, every one its remote's branches, as last fetched, don't have.

        Walking the history (``git log --all``) is what the look costs, and a folder refused for
        its commits would be walked on every call, so each walk's answer is kept (:data:`_WALKED`)
        against the refs it was made at (``git show-ref --head``): no commit comes, goes or
        changes without one of them moving."""
        refs = self._git("show-ref", "--head", check=False)
        if refs.returncode not in (0, 1):  # 1: no refs at all yet, as in a clone of an empty remote
            return False
        walk = (os.path.realpath(self._clone), since_remote)
        walked = _WALKED.get(walk)
        if walked is not None and walked[0] == refs.stdout:
            return walked[1]
        log = ["log", "--all", "--format=%ae%n%ce"]
        if since_remote:
            log += ["--not", "--remotes=origin"]
        commits = self._git(*log, check=False)
        if commits.returncode != 0:
            return False
        ours = not set((commits.stdout or "").split()) - {_COMMIT_EMAIL}
        _WALKED[walk] = (refs.stdout, ours)
        return ours

    def _only_git_syncs(self, *, since_remote: bool) -> bool:
        """Whether the working clone holds nothing but Git Sync's own work: its commits
        (:meth:`_commits_git_syncs`), and nothing git hasn't committed, ignored files included,
        outside the sync layout. A folder of the owner's own has commits or files of theirs; a
        hosting service's first commit on a remote is the remote's, not the folder's."""
        if not self._commits_git_syncs(since_remote=since_remote):
            return False
        status = self._git(
            "status", "--porcelain", "-z", "--no-renames", "--untracked-files=all", "--ignored",
            check=False,
        )
        if status.returncode != 0:
            return False
        return all(
            entry[3:].split("/", 1)[0] in _SYNC_ROOT
            for entry in (status.stdout or "").split("\0")
            if len(entry) > 3
        )

    def _leaked(self, where: list[tuple[str, str]]) -> bool:
        """Whether the clone's origin — and its push URL, if it has one — is Git remote URL with
        a user name or token written into it, and nothing else sends its fetches or pushes
        anywhere: a clone made from such a URL, before Git Sync took the token from Access
        token instead."""
        return (
            _web(self._repo_url)
            and any(key == "remote.origin.url" for key, _ in where)
            and any(_USERINFO.search(value) for _, value in where)
            and all(
                key in ("remote.origin.url", "remote.origin.pushurl")
                and _USERINFO.sub("://", value, count=1) == self._repo_url
                for key, value in where
            )
        )

    def _standing(self) -> str:
        """What the folder already at Local working clone is to Git Sync, read without changing
        anything in it:

        - ``"ours"``: a clone Git Sync made (:data:`_MARK`), pointing at Git remote URL.
        - ``"adopt"``: pointing at Git remote URL, unmarked, and holding nothing but Git Sync's
          own work — a clone an older Git Sync made, before clones were marked.
        - ``"rewrite"``: Git Sync's, its origin Git remote URL with a user name or token written
          into it (:meth:`_leaked`). Its origin is rewritten without it, and every copy of it
          taken out of the clone (:meth:`_rewrite_origin`), rather than the clone replaced.
        - ``"replace"``: Git Sync's, but pointing at another remote — Git remote URL changed, or
          its own settings send pushes elsewhere — and holding nothing that remote lacks but Git
          Sync's own work. A fresh clone of Git remote URL takes its place.
        - ``"refuse"``: it holds work Git Sync didn't make. Nothing is ever committed into it,
          pushed from it or changed in it."""
        config = self._clone_config()
        mark = _MARK.lower()
        marked = (mark, "true") in config
        where = [kv for kv in config if kv[0] != mark]
        follows = where == [("remote.origin.url", self._repo_url)]
        leaked = not follows and self._leaked(where)
        if marked:
            if follows:
                return "ours"
            if leaked:
                return "rewrite"
            return "replace" if self._only_git_syncs(since_remote=True) else "refuse"
        if not self._only_git_syncs(since_remote=False):
            return "refuse"
        if leaked:
            return "rewrite"
        return "adopt" if follows else "replace"

    def _take_up(self, standing: str) -> None:
        """Make a clone Git Sync may sync through fully its own, as :meth:`_standing` found it: a
        credential written into its origin taken out, and an unmarked one marked."""
        if standing == "rewrite":
            self._rewrite_origin()
        if standing in ("adopt", "rewrite"):
            self._mark()

    def _mark(self) -> None:
        """Mark the working clone as one Git Sync made."""
        self._git("config", "--local", _MARK, "true")

    def _rewrite_origin(self) -> None:
        """Point the clone's origin at Git remote URL itself, with no push URL of its own, and
        take the user name and token its old URL carried out of every file under ``.git`` that
        holds a copy. git today writes the URL whole only into ``.git/config`` — its fetch
        record and its logs name the URL without the credential — but a clone an older git made
        may hold it in those as well."""
        old = {
            value
            for key, value in self._clone_config()
            if key in ("remote.origin.url", "remote.origin.pushurl") and _USERINFO.search(value)
        }
        self._git("config", "--local", "--replace-all", "remote.origin.url", self._repo_url)
        self._git("config", "--local", "--unset-all", "remote.origin.pushurl", check=False)
        needles: list[tuple[bytes, bytes]] = [
            (value.encode(), self._repo_url.encode()) for value in old
        ]
        for value in old:
            userinfo = _USERINFO.search(value)
            if userinfo:
                needles.append((userinfo.group(0).encode(), b"://"))
        _scrub(os.path.join(self._clone, ".git"), needles)

    def _replace_clone(self) -> None:
        """Swap the working clone for a fresh clone of Git remote URL (:meth:`_standing` said
        ``"replace"``).

        The fresh clone is made, and marked, in a new folder beside the old one, and takes its
        place — with the old folder's permissions — only once it has succeeded. A remote that
        can't be cloned leaves the old clone where it was — still not pointing at the setting, so
        neither fetched from nor pushed to — and says why, as any clone does. Nothing in the old
        clone is needed: it held only Git Sync's own work, and every cycle re-exports this
        machine's whole state."""
        clone = os.path.abspath(self._clone)
        parent, name = os.path.split(clone)
        mode = stat.S_IMODE(os.stat(clone).st_mode)
        fresh = tempfile.mkdtemp(prefix=f".{name}-", dir=parent)
        try:
            self._run(["clone", "-c", f"{_MARK}=true", self._repo_url, fresh])
            os.chmod(fresh, mode)
            retired = f"{fresh}-replaced"
            os.rename(clone, retired)
        except BaseException:
            shutil.rmtree(fresh, ignore_errors=True)
            raise
        try:
            os.rename(fresh, clone)
        except BaseException:
            os.rename(retired, clone)
            shutil.rmtree(fresh, ignore_errors=True)
            raise
        shutil.rmtree(retired, ignore_errors=True)

    def _ensure_clone(self) -> None:
        """Make ``<clone>`` a checkout of Git remote URL on the configured branch. Idempotent.

        A brand-new empty remote is not an error — it is the first machine: ``git clone``
        of an empty remote succeeds (with a warning), and Branch starts there for the first push
        to publish (:meth:`_start_branch`). A clone is marked as Git Sync's as it is made. A
        folder already there is looked at first, without changing it (:meth:`_standing`): a
        clone pointing elsewhere is replaced, one an older Git Sync made is marked, one whose
        origin carries a credential is rewritten without it, and a folder holding work Git Sync
        didn't make raises :class:`GitSyncFailed` — before any git step that could change it
        runs.
        """
        git_dir = os.path.join(self._clone, ".git")
        if not os.path.isdir(git_dir):
            parent = os.path.dirname(self._clone.rstrip("/")) or "."
            os.makedirs(parent, exist_ok=True)
            # check=True: a real clone failure (bad URL / no auth) raises and the caller
            # converts it. An empty remote still returns 0 here.
            self._run(["clone", "-c", f"{_MARK}=true", self._repo_url, self._clone])
        else:
            standing = self._standing()
            if standing == "refuse":
                raise GitSyncFailed(self._not_ours())
            if standing == "replace":
                self._replace_clone()
            else:
                self._take_up(standing)
        # Branch checks out when the clone has it, or the remote does (git makes it from the
        # remote's); a branch neither has yet starts empty.
        if self._rev(f"refs/heads/{self._branch}") or self._rev(self._upstream):
            self._git("checkout", "-q", self._branch)
        else:
            self._start_branch()

    def _start_branch(self) -> None:
        """Start Branch, which neither the working clone nor the remote has yet, empty: an
        orphan with none of the files the clone came with. A clone comes checked out on the
        remote's own branch, and a Branch started from it would carry that branch's files — which
        the listing would serve as sync objects, and the first push would publish on Branch."""
        head = self._git("symbolic-ref", "-q", "HEAD", check=False)
        if (head.stdout or "").strip() == f"refs/heads/{self._branch}":
            return  # already on it, before its first commit
        if self._git("switch", "-q", "--orphan", self._branch, check=False).returncode == 0:
            return
        # A git before 2.23 has no ``switch``: make the orphan, then take the files the clone
        # came with out of the index and the working tree, as ``switch --orphan`` does.
        tracked = [path for path in self._git("ls-files", "-z").stdout.split("\0") if path]
        self._git("checkout", "-q", "--orphan", self._branch)
        self._git("read-tree", "--empty")
        root = os.path.normpath(self._clone)
        for path in tracked:
            full = self._resolve(path)
            with contextlib.suppress(FileNotFoundError):
                os.remove(full)
            folder = os.path.dirname(full)
            while (
                os.path.normpath(folder) != root
                and os.path.isdir(folder)
                and not os.listdir(folder)
            ):
                os.rmdir(folder)
                folder = os.path.dirname(folder)

    @staticmethod
    def _refusal(cp: subprocess.CompletedProcess) -> str:
        """What kind of "no" a failed ``git push`` got, else ``""``. Final ones first — the
        remote's rules (a hook, branch protection), a checked-out branch, a shallow clone, a
        branch it hides, a repository missing its own objects, one that can't store the push, a
        branch name clashing with one it has, a name it won't take — since a push turned away for
        any of them is no race, however its words read. Then the retried ones: the branch's ref
        lock taken (``"locked"``), the remote having commits this clone doesn't or the branch
        moving under this push (``"race"``), and a branch it couldn't update for a reason of its
        own (``"ref-update"``)."""
        low = f"{cp.stderr or ''}\n{cp.stdout or ''}".lower()
        ref_lock = _REF_LOCK in low
        unwritable = ref_lock and any(needle in low for needle in _REF_UNWRITABLE)
        for kind, found in (
            ("rules", any(needle in low for needle in _RULES)),
            ("checked-out", any(needle in low for needle in _CHECKED_OUT)),
            ("shallow", any(needle in low for needle in _SHALLOW)),
            ("hidden", any(needle in low for needle in _HIDDEN)),
            ("damaged", any(needle in low for needle in _DAMAGED)),
            ("cant-store", unwritable or any(needle in low for needle in _CANT_STORE)),
            ("clash", any(needle in low for needle in _CLASH)),
            ("funny-ref", any(needle in low for needle in _FUNNY_REF)),
            (
                "locked",
                (ref_lock and _REF_LOCK_TAKEN in low)
                or any(needle in low for needle in _LOCKED),
            ),
            ("race", any(needle in low for needle in _RACE)),
            ("ref-update", any(needle in low for needle in _REF_NOT_UPDATED)),
        ):
            if found:
                return kind
        return ""

    @classmethod
    def _push_outcome(cls, cp: subprocess.CompletedProcess) -> str:
        """Classify a failed ``git push`` for the outbox. A refusal no retry changes — the
        remote's rules, a checked-out branch, a shallow clone, a hidden branch, a damaged
        repository, one that can't store the push, a clashing or unaccepted branch name — is
        ``permanent``, like a bad URL or denied auth. A race still lost after every catch-up, a
        ref lock still taken, the network, or another refusal the remote may lift is
        ``transient``: the cycle tries again."""
        refusal = cls._refusal(cp)
        if refusal in _FINAL or transport_refusal(_words(cp)):
            # A remote PersonalClaw's git does not reach (a local path) is refused the same way
            # on every try.
            return "permanent"
        low = f"{cp.stderr or ''}\n{cp.stdout or ''}".lower()
        if refusal or "rejected" in low or any(n in low for n in _UNREACHABLE):
            return "transient"
        return "permanent"

    def _push_refused(
        self, cp: subprocess.CompletedProcess, outcome: str, tries: int = _PUSH_TRIES
    ) -> str:
        """What a ``git push`` the remote did not take says, after ``tries`` of it — a push
        tries again after a lost race or a taken lock, a registry swap doesn't. The words follow
        what git said; a ``transient`` outcome (which the cycle retries) adds that it will try
        again."""
        words = _words(cp)
        refused = transport_refusal(words)
        if refused:
            # The working clone's own remote is one PersonalClaw's git does not reach (a local
            # path). Retrying cannot change that, and the outcome is permanent.
            return self._detail(refused, words)
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
        elif refusal == "hidden":
            sentence = (
                f"Git Sync couldn't push to the git remote: it hides branch '{branch}', and takes "
                f"no push to a branch it hides. Set Branch {_ON_CARD} to one it doesn't hide, or "
                "stop hiding this one there (git's receive.hideRefs or transfer.hideRefs setting)."
            )
        elif refusal == "damaged":
            sentence = (
                "Git Sync couldn't push to the git remote: its repository is missing objects its "
                "own history needs, so no push to it can land. Run git fsck in that repository to "
                "see what is missing and restore it from a backup, or set Git remote URL "
                f"{_ON_CARD} to another repository."
            )
        elif refusal == "cant-store":
            sentence = (
                "Git Sync couldn't push to the git remote: the remote couldn't store what this "
                "machine sent — the repository there isn't writable by the account this machine "
                "pushes as, or the disk it is on is full. Fix that on the remote."
            )
        elif refusal == "clash":
            other = _CLASHING.search(words)
            has = f"a branch '{other.group(1)}'" if other else "a branch of another name"
            sentence = (
                f"Git Sync couldn't push to the git remote: it has {has}, and git can't keep "
                f"branch '{branch}' beside it, since one name is a folder of the other. Set "
                f"Branch {_ON_CARD} to another name."
            )
        elif refusal == "funny-ref":
            sentence = (
                f"Git Sync couldn't push to the git remote: it won't take '{branch}' as the name "
                f"of a branch. Set Branch {_ON_CARD} to a name it accepts, such as main."
            )
        elif refusal == "locked":
            held = (
                f"still locked by another git process after {tries} tries"
                if tries > 1
                else "locked by another git process"
            )
            sentence = (
                f"Git Sync couldn't push to the git remote: branch '{branch}' there was {held} — "
                "a push landing at the same moment, or an interrupted one that left "
                f"refs/heads/{branch}.lock behind. If it keeps happening, remove that file in the "
                "remote repository."
            )
        elif refusal == "race":
            sentence = (
                f"Git Sync couldn't push to the git remote: each of the {_PUSH_TRIES} times it "
                "caught up with the remote and pushed, another push had reached it first."
            )
        elif refusal == "ref-update":
            sentence = (
                "Git Sync couldn't push to the git remote: it took what this machine sent but "
                f"couldn't move branch '{branch}' to it. If it keeps happening, check that "
                "repository on the remote."
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
        return self._detail(sentence, words)

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

    def _stale_lock(self, words: str) -> str:
        """The lock file inside the working clone that git's ``words`` say is in its way, as a
        path relative to the clone (``.git/index.lock``); ``""`` when they name none there — a
        lock on the remote is the remote's."""
        clone = os.path.realpath(self._clone)
        for match in _LOCK_FILE.finditer(words):
            path = os.path.realpath(match.group(1))
            if path.startswith(clone + os.sep):
                return os.path.relpath(path, clone)
        return ""

    def _lock_left(self, lock: str) -> str:
        """What a lock file left in the working clone says: which file, in which folder, and to
        remove it if no git is running there."""
        return (
            f"Git Sync couldn't update its working clone at {self._clone}: another git process is "
            f"using it, or an interrupted one left {lock} behind. Wait for it to finish, or, if no "
            "git is running there, remove that file."
        )

    def _not_ours(self) -> str:
        """What a folder at Local working clone that holds work Git Sync didn't make says —
        whichever remote it has, Git remote URL included."""
        return (
            f"Git Sync won't commit into or push from {self._clone}: that folder holds commits or "
            f"files Git Sync didn't make. Set Local working clone {_ON_CARD} to a new folder, "
            "where Git Sync makes a clone of its own."
        )

    def _cannot_read(self, failure: OSError) -> str:
        """What a file or folder in the working clone that couldn't be read says."""
        name = failure.filename
        what = os.path.relpath(name, self._clone) if isinstance(name, str) and name else "a file"
        if isinstance(failure, PermissionError):
            sentence = (
                f"Git Sync isn't allowed to read {what} in its working clone at {self._clone}. "
                "Fix its permissions."
            )
        else:
            sentence = (
                f"Git Sync couldn't read {what} in its working clone at {self._clone}. Check that "
                "folder, and the disk it is on."
            )
        return self._detail(f"{sentence} {_RETRIES}", str(failure))

    def _cycle_stopped(self, failure: BaseException) -> str:
        """What a sync step that git or the working clone stopped part-way says: a push's
        ``transient`` outcome, or what a listing, a read or a registry swap raises. The cycle
        tries again on its next run either way, so each says so."""
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
            trouble = _remote_trouble(words) if step in ("clone", "fetch") else ""
            # A clone's "Permission denied" can be the REMOTE's (a path on the remote host this
            # machine's login can't read), so for a clone only one naming the clone's own path
            # is its.
            denied = any(needle in low for needle in _CLONE_DENIED) and (
                step != "clone" or self._clone in words
            )
            if trouble and step == "fetch":
                sentence = f"Git Sync couldn't fetch from the git remote: {trouble}"
            elif trouble:
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
            elif self._stale_lock(words):
                sentence = self._lock_left(self._stale_lock(words))
            elif "index.lock" in low:
                sentence = self._lock_left(".git/index.lock")
            elif step == "fetch":
                sentence = (
                    "Git Sync couldn't fetch from the git remote. Check Git remote URL "
                    f"{_ON_CARD}, and that git on this machine can reach it."
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
        return self._detail(f"{sentence} {_RETRIES}", words)

    def _unreadable(self, words: str) -> str:
        """What a ``git ls-remote`` probe that git answered with an error says."""
        refused = transport_refusal(words)
        if refused:
            return self._detail(refused, words)
        trouble = _remote_trouble(words)
        sentence = (
            f"Git Sync couldn't read the git remote: {trouble}"
            if trouble
            else (
                f"Git Sync couldn't read the git remote. Check Git remote URL {_ON_CARD}, "
                "and that git on this machine can reach it."
            )
        )
        return self._detail(sentence, words)

    def _probe_stopped(self, failure: BaseException) -> str:
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
        return self._detail(sentence, _words(failure))

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
            conflict = self._catch_up(pushing=True)
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
                conflict = self._catch_up(pushing=True)
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
        except GitSyncFailed as e:
            # A folder it won't sync through: retrying changes nothing until the owner picks
            # another one.
            return PushResult(pushed=pushed, skipped=skipped, outcome="permanent", detail=str(e))
        except (subprocess.SubprocessError, OSError) as e:
            # Clone/pull/commit blew up mid-cycle — retryable.
            return PushResult(
                pushed=pushed, skipped=skipped, outcome="transient", detail=self._cycle_stopped(e)
            )

    def list_remote(self, prefix: str = "") -> list[RemoteRef]:
        # Empty only when there is nothing there yet: no remote set, or a remote with nothing on
        # the branch. Anything else that stops the listing raises GitSyncFailed saying what —
        # settings git can't be run with, a clone that fails, a folder Git Sync won't sync
        # through, a catch-up that can't be made — since an empty or a stale listing would be
        # taken for the remote's.
        if self._idle:
            return []
        if self._refused:
            raise GitSyncFailed(self._refused)
        try:
            self._ensure_clone()
        except (subprocess.SubprocessError, OSError) as e:
            raise GitSyncFailed(self._cycle_stopped(e)) from None
        self._level()
        refs: list[RemoteRef] = []
        try:
            for dirpath, dirnames, filenames in os.walk(self._clone, onerror=_reraise):
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
                    except FileNotFoundError:
                        continue  # vanished between walk and stat — skip it
                    # mtime is a cheap change fingerprint; the cycle only compares it, never
                    # parses it, so mtime is enough and avoids a git blob-hash per file.
                    refs.append(
                        RemoteRef(key=key, size=st.st_size, fingerprint=str(int(st.st_mtime)))
                    )
        except OSError as e:
            # A folder the walk couldn't read would leave its objects out of the listing.
            raise GitSyncFailed(self._cannot_read(e)) from None
        return refs

    def pull(self, refs: list[RemoteRef]) -> list[SyncObject]:
        if self._idle:
            return []
        if self._refused:
            raise GitSyncFailed(self._refused)
        # list_remote makes the clone; a read never does, and there's nothing to read without one.
        if not os.path.isdir(os.path.join(self._clone, ".git")):
            return []
        try:
            standing = self._standing()
            if standing in ("refuse", "replace"):
                # A folder of work Git Sync didn't make, or a clone of another remote: nothing in
                # it is served, and it is never fetched from.
                return []
            self._take_up(standing)
        except (subprocess.SubprocessError, OSError) as e:
            raise GitSyncFailed(self._cycle_stopped(e)) from None
        self._level()
        out: list[SyncObject] = []
        for ref in refs:
            try:
                with open(self._resolve(ref.key), "rb") as fh:
                    out.append(SyncObject(key=ref.key, data=fh.read()))
            except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
                # A ref the clone doesn't have is dropped, not raised — the caller reconciles
                # against what it asked for.
                continue
            except OSError as e:
                raise GitSyncFailed(self._cannot_read(e)) from None
        return out

    def cas_registry(self, expected_sha: str | None, data: bytes) -> bool:
        """Compare-and-swap ``registry.json`` through the remote's own push rejection.

        ``False`` is a lost race and nothing else — the registry isn't what the caller expected,
        or the remote moved under the swap's push — besides a transport with no remote set.
        Anything else that stops the swap — settings git can't be run with, a clone that can't be
        made or caught up, a folder Git Sync won't sync through, a lock file in the clone, a push
        the remote refuses for another reason — raises :class:`GitSyncFailed` saying so, once the
        swap's own write is taken back: ``False`` would send the cycle round to re-read and swap
        again until it gave up, as "registry CAS lost"."""
        if self._idle:
            return False
        if self._refused:
            raise GitSyncFailed(self._refused)
        before: str | None = None
        wrote = committed = False
        failure = ""
        try:
            self._ensure_clone()
            self._level()
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
            pushed = self._git("push", "origin", self._branch, check=False)
            if pushed.returncode == 0:
                return True
            if self._refusal(pushed) != "race":
                outcome = self._push_outcome(pushed)
                failure = self._push_refused(pushed, outcome, tries=1)
        except GitSyncFailed as e:
            failure = str(e)
        except (subprocess.SubprocessError, OSError) as e:
            failure = self._cycle_stopped(e)
        # The write never reached the remote, so it mustn't stay in the clone either: the
        # caller's re-read would find these bytes instead of the remote's, and a later push
        # would carry them onto whatever registry the remote has by then — a write with no
        # compare at all.
        with contextlib.suppress(subprocess.SubprocessError, OSError):
            if committed:
                self._undo_commit(before)
            elif wrote:
                self._restore_registry()
        if failure:
            raise GitSyncFailed(failure)
        return False

    def test(self) -> ConnectionResult:
        if not self._repo_url:
            return ConnectionResult(ok=False, detail="no git remote configured")
        if self._refused:
            return ConnectionResult(ok=False, detail=self._refused)
        try:
            # A folder every sync would refuse is said here too, read without changing it: a
            # green probe beside a sync that can't run says nothing true.
            refused = (
                bool(self._clone)
                and os.path.isdir(os.path.join(self._clone, ".git"))
                and self._standing() == "refuse"
            )
            if refused:
                return ConnectionResult(ok=False, detail=self._not_ours())
            # ``ls-remote`` against the URL confirms both reachability and auth without the
            # side effect of writing a clone during a read-only probe.
            cp = self._run(["ls-remote", self._repo_url], check=False)
            if cp.returncode == 0:
                shown = _shown(self._repo_url)
                return ConnectionResult(ok=True, detail=f"git remote reachable: {shown}")
            return ConnectionResult(ok=False, detail=self._unreadable(_words(cp)))
        except (subprocess.SubprocessError, OSError) as e:
            return ConnectionResult(ok=False, detail=self._probe_stopped(e))


def create_provider(config: dict[str, Any] | None = None) -> GitSyncProvider:
    """Extension factory — builds the git-sync transport from user settings. ``token`` is
    declared sensitive, so it is kept in the credential store and arrives here resolved."""
    config = config or {}
    return GitSyncProvider(
        repo_url=str(config.get("repo_url", "") or ""),
        local_clone=str(config.get("local_clone", "") or "~/.personalclaw/sync/git-sync"),
        branch=str(config.get("branch", "") or "main"),
        token=str(config.get("token", "") or ""),
        username=str(config.get("username", "") or ""),
    )
