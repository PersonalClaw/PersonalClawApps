"""Rsync sync transport — carries durability shard objects to a host over ssh.

Point every machine's rsync-sync at the same host + path and the durability layer converges
through it. The transport moves bytes only; the merge, the machine-seq registry contents and
the outbox live above it in core, and **encryption is applied above it too** — by the sync
cycle, at the transport boundary — so this module never sees a key or a passphrase.

``rsync`` is a tree-sync tool, not an object store, so the shape here differs from
``s3-sync``: pushes stage into a throwaway directory and go up in ONE invocation, and pulls
come down into a persistent local **mirror** whose whole point is that rsync then transfers
only what changed. Reads are served from that mirror.

Three things about driving ``rsync`` safely are worth stating, because each one is a defect
this module exists to avoid:

**No shell, ever, and no argument injection.** Every invocation is an argv list with
``shell=False``. Host and path are validated against strict character sets and rejected if
they could be read as options, and every command puts ``--`` before its path operands.
Without that, a host of ``-e/bin/sh`` or a path beginning with ``-`` is remote code
execution, because ``rsync`` parses its own operands.

**An update can be silently skipped.** ``rsync``'s quick check compares size and mtime, so a
file whose new content is the SAME LENGTH and is written within the same clock second is not
transferred at all — and rsync exits 0. Measured with a real registry-shaped change:
``{"seq":19}`` → ``{"seq":20}`` did **not** transfer. Shard objects are immune (insert-only,
never rewritten), but the registry is rewritten every cycle, so registry writes use
``--ignore-times`` and are then **read back and verified**.

**There is no compare-and-swap.** ``rsync`` has a create-only primitive
(``--ignore-existing``, whose ``--itemize-changes`` output reports whether the file was
actually created) but nothing conditional for an overwrite. :meth:`cas_registry` therefore
verifies, writes, and re-reads — and reports a lost race whenever the bytes it finds are not
the ones it expected, or not its own after its write. That bias is deliberate: core's CAS loop
re-pulls, re-merges peers' entries and retries on a ``False``, so a false ``False`` costs one
round trip, while a false ``True`` silently discards another machine's registration. An rsync
run that fails is no race, and raises instead: no re-pull fixes it.
"""

import errno
import hashlib
import os
import re
import shutil
import subprocess  # noqa: S404 — argv-only, shell=False; see the module docstring
import tempfile
from typing import Any

from personalclaw.sdk.net import sentence_with_detail
from personalclaw.sdk.sync import (
    ConnectionResult,
    PushResult,
    RemoteRef,
    SyncObject,
    SyncTransportProvider,
)
from personalclaw.sdk.util import child_process_env

#: The single shared registry object every machine compare-and-swaps.
_REGISTRY_KEY = "registry.json"

# ── What a failure says ──────────────────────────────────────────────────────────────
#
# rsync's and ssh's own words ("rsync error: unexplained error (code 255)", a Python
# ``FileNotFoundError`` naming the binary) say neither what is wrong in the user's setup nor
# what to do. Each failure is said as that, and their words follow as the detail.

#: Where this transport's own settings (SSH host, Sync root path, SSH port, SSH identity file)
#: are set.
_ON_CARD = "on the Rsync Sync card in Settings → Providers"
#: Said after a failure the sync cycle retries (a ``transient`` outcome).
_RETRIES = "Sync tries again on its next run."

# What ssh prints for each kind of trouble reaching the host, matched lower-cased in the order
# ``RsyncSyncProvider._refused`` checks them. The login refusal names ssh's auth methods, because
# rsync's own "Permission denied (13)" — a folder refusing a write — also opens with the same
# two words.
_HOST_KEY_CHANGED = ("remote host identification has changed",)
_HOST_KEY = ("host key verification failed",)
_SHELL_NOT_CLEAN = ("is your shell clean",)
_NO_RSYNC_THERE = ("rsync: command not found", "rsync: not found", "remote command not found")
_UNREACHABLE = (
    "could not resolve hostname",
    "connection refused",
    "connection timed out",
    "operation timed out",
    "no route to host",
    "network is unreachable",
)
_LOGIN_REFUSED = re.compile(
    r"permission denied \((?:publickey|password|keyboard-interactive|gssapi|hostbased)"
    r"|permission denied, please try again"
    r"|too many authentication failures"
)
#: GNU rsync's errno for "No such file or directory", which it prints after those words in
#: whatever language the host's rsync speaks.
_ENOENT = re.compile(r"\(2\)\s*$")

#: Hostnames (and the optional ``user@``) may contain only these characters. Deliberately
#: strict: anything outside this set is either meaningless to ssh or a way to smuggle an
#: option/shell metacharacter into rsync's own operand parser.
_HOST_RE = re.compile(r"^(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9._-]+$")

#: An identity-file path may not contain whitespace or quotes, because it is embedded in the
#: single string rsync hands to its remote-shell command and rsync splits that string itself.
_KEY_PATH_RE = re.compile(r"^[A-Za-z0-9._~/@+-]+$")


class RsyncConfigError(Exception):
    """A setting that cannot be used safely. Raised at validation, never at transfer time."""


class WorkdirUnusable(OSError):
    """This machine's Local working directory refused what a sync needed of it, before rsync
    ran at all. Its text is the sentence (what is wrong, the setting that fixes it) and then the
    filesystem's own words; the ``OSError`` itself is chained.

    ``push`` turns it into its outcome. ``pull`` and ``cas_registry`` have no outcome to carry a
    sentence, so they raise it — the sync cycle reports a read or a registry swap that raised
    as the cycle's error, in this text, rather than the bare ``[Errno 13] …`` it used to relay.
    """

    def __init__(self, sentence: str, failure: OSError) -> None:
        super().__init__(sentence_with_detail(sentence, failure))
        self.sentence = sentence
        self.failure = failure


class RsyncFailed(RuntimeError):
    """An rsync run a listing, a read or the registry swap needed that didn't complete — it
    timed out, couldn't start, or ended with an error — said as what is wrong and what to do,
    then rsync's (or ssh's) own words.

    ``list_remote``, ``pull`` and ``cas_registry`` raise it, having no outcome to carry a
    sentence. Answering empty, or ``False``, instead read as a remote with nothing on it, or as a
    swap another machine won: the sync cycle took an empty registry, published as if this were
    the first machine, and reported its registry swap lost five times over — to no other machine
    at all. Raised, the cycle records it as its failure, in this text.
    """

    def __init__(self, sentence: str, words: object) -> None:
        super().__init__(sentence_with_detail(sentence, words))
        self.sentence = sentence


def validate_host(host: str) -> str:
    """Return ``host`` if it is a safe ssh destination, else raise.

    Rejects an empty-but-present value, a leading ``-`` (which rsync would read as an
    option), and every character outside :data:`_HOST_RE` — notably space, ``:``, ``;``,
    ``$``, backtick and quotes.
    """
    h = (host or "").strip()
    if not h:
        return ""
    if h.startswith("-"):
        raise RsyncConfigError("ssh host may not begin with '-' (rsync would read it as an option)")
    if not _HOST_RE.match(h):
        raise RsyncConfigError(
            f"ssh host {h!r} contains characters that are not allowed "
            "(letters, digits, dot, dash, underscore and one optional 'user@')"
        )
    return h


def validate_remote_path(path: str) -> str:
    """Return ``path`` if it is a safe rsync path operand, else raise.

    Rejects a leading ``-``, any ``:`` (which makes rsync re-interpret the operand as a
    ``host:path`` or a ``host::module`` daemon spec), and control characters/newlines.
    """
    p = (path or "").strip()
    if not p:
        return ""
    if p.startswith("-"):
        raise RsyncConfigError("sync path may not begin with '-' (rsync would read it as an option)")
    if ":" in p:
        raise RsyncConfigError(
            "sync path may not contain ':' — rsync would read it as a host:path or "
            "host::module spec rather than a path"
        )
    if any(ch in p for ch in "\n\r\x00") or any(ord(ch) < 32 for ch in p):
        raise RsyncConfigError("sync path may not contain control characters")
    return p


class RsyncSyncProvider(SyncTransportProvider):
    """A durability sync transport backed by ``rsync``, over ssh or to a local path."""

    name = "rsync-sync"
    display_name = "Rsync Sync"

    def __init__(
        self,
        host: str = "",
        path: str = "",
        *,
        port: int = 22,
        ssh_key: str = "",
        staging_dir: str = "~/.personalclaw/sync/rsync-sync",
        timeout_secs: int = 300,
        rsync_bin: str = "rsync",
    ) -> None:
        # Validation errors are CAPTURED, not raised: a provider must construct so the Store
        # can show it and the user can fix the field. Every method refuses while it is set.
        self._config_error = ""
        try:
            self._host = validate_host(host)
        except RsyncConfigError as e:
            self._host, self._config_error = "", str(e)
        raw_path = (path or "").strip()
        try:
            # Only a LOCAL path is expanded — ~ and $VARS on a remote target would expand
            # against THIS machine's environment, which is never what the user meant.
            if raw_path and not self._host:
                raw_path = os.path.expandvars(os.path.expanduser(raw_path))
            self._path = validate_remote_path(raw_path)
        except RsyncConfigError as e:
            self._path = ""
            self._config_error = self._config_error or str(e)
        self._port = int(port) if port else 22
        key = (ssh_key or "").strip()
        if key and not _KEY_PATH_RE.match(key):
            self._config_error = self._config_error or (
                "ssh identity path contains characters that are not allowed "
                "(no spaces or quotes — rsync splits the remote-shell string itself)"
            )
            key = ""
        self._ssh_key = os.path.expanduser(key) if key else ""
        self._staging_root = os.path.expandvars(
            os.path.expanduser(staging_dir or "~/.personalclaw/sync/rsync-sync")
        )
        self._timeout = max(1, int(timeout_secs) if timeout_secs else 300)
        self._rsync = rsync_bin or "rsync"

    # ── configuration / readiness ────────────────────────────────────────────────────

    @property
    def configured(self) -> bool:
        return bool(self._path) and not self._config_error

    def _unconfigured_detail(self) -> str:
        if self._config_error:
            return f"rsync-sync is misconfigured — {self._config_error}"
        return "rsync-sync is not configured — missing: sync root path"

    @property
    def _mirror(self) -> str:
        """The persistent local mirror pulls come down into (what makes them incremental)."""
        return os.path.join(self._staging_root, "mirror")

    def _target(self, trailing_slash: bool = True) -> str:
        """The rsync path operand for the sync root — ``host:path`` or a plain local path."""
        base = self._path.rstrip("/")
        base = f"{base}/" if trailing_slash else base
        return f"{self._host}:{base}" if self._host else base

    def _rsh_arg(self) -> list[str]:
        """The ``-e`` remote-shell argument, or nothing for a local transfer.

        ``BatchMode=yes`` is set on purpose: without it a host whose key is not yet trusted
        (or whose key needs a passphrase) makes ssh PROMPT, and a prompt inside a background
        sync job hangs until the timeout instead of failing with a readable reason.

        Host-key checking is deliberately left at the user's own ssh default. Passing
        ``StrictHostKeyChecking=no`` would make first-contact "just work" by accepting any
        key, which is exactly the man-in-the-middle this transport must not open.
        """
        if not self._host:
            return []
        parts = ["ssh", "-o", "BatchMode=yes"]
        if self._port and self._port != 22:
            parts += ["-p", str(int(self._port))]
        if self._ssh_key:
            parts += ["-i", self._ssh_key]
        return ["-e", " ".join(parts)]

    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        """Run one rsync invocation. argv only, no shell, always bounded by a timeout.

        A provider runs inside the gateway, whose environment holds every secret saved in
        PersonalClaw, and rsync starts ssh, which reads its environment too. So rsync gets the
        child allowlist, plus the owner's SSH agent socket it signs in to their host through,
        and nothing else of the gateway's."""
        return subprocess.run(  # noqa: S603 — argv list, shell=False, operands validated
            [self._rsync, *args],
            capture_output=True,
            text=True,
            timeout=self._timeout,
            shell=False,
            check=False,
            env=child_process_env(ssh_agent=True),
        )

    def _run_or_fail(self, args: list[str]) -> subprocess.CompletedProcess:
        """Run one rsync invocation a listing, a read or the registry swap needs, raising
        :class:`RsyncFailed` — said as a push's failure is — when it times out or can't start."""
        try:
            return self._run(args)
        except subprocess.TimeoutExpired as e:
            raise RsyncFailed(self._timed_out(), e) from e
        except OSError as e:
            raise RsyncFailed(self._not_started(e), e) from e

    # ── what a failure says ──────────────────────────────────────────────────────────

    def _ssh_command(self) -> str:
        """The ssh command that reaches the host the way rsync's remote shell does, to run by
        hand."""
        parts = ["ssh"]
        if self._port and self._port != 22:
            parts += ["-p", str(int(self._port))]
        if self._ssh_key:
            parts += ["-i", self._ssh_key]
        return " ".join([*parts, self._host])

    def _cannot_start(self, failure: OSError) -> str:
        """What rsync not starting on this machine at all says, with the error's own words."""
        return sentence_with_detail(self._not_started(failure), failure)

    def _not_started(self, failure: OSError) -> str:
        """The sentence alone for rsync not starting on this machine at all."""
        if (
            isinstance(failure, (FileNotFoundError, PermissionError))
            and failure.filename == self._rsync
        ):
            return (
                "Rsync Sync runs the rsync command, and PersonalClaw couldn't start it on this "
                "machine: it isn't installed, isn't on the PATH PersonalClaw runs with, or isn't "
                "executable. Install rsync where PersonalClaw can run it."
            )
        return (
            "Rsync Sync couldn't start rsync on this machine. Check that rsync runs from a "
            "terminal here."
        )

    def _workdir_unusable(self, failure: OSError) -> str:
        """What this machine's Local working directory refusing a write says. The sentence
        alone: the caller says whether the cycle retries, and adds the filesystem's words, which
        name the exact path."""
        if failure.errno == errno.ENOSPC:
            return self._workdir_says("full")
        if failure.errno == errno.EROFS:
            return self._workdir_says("read-only")
        if isinstance(failure, PermissionError):
            return self._workdir_says("denied")
        if isinstance(failure, (FileExistsError, NotADirectoryError)):
            return self._workdir_says("in-the-way")
        return self._workdir_says("")

    def _workdir_says(self, trouble: str) -> str:
        """The Local working directory's sentence for ``trouble`` (``full``, ``read-only``,
        ``denied``, ``in-the-way``, or ``""`` for anything else)."""
        where = self._staging_root
        setting = f"Local working directory {_ON_CARD}"
        if trouble == "full":
            return (
                f"The disk holding Rsync Sync's local working directory {where} is full. Free "
                f"some space on it, or set {setting} to a folder on another disk."
            )
        if trouble == "read-only":
            return (
                f"Rsync Sync's local working directory {where} is on a read-only disk. Set "
                f"{setting} to a folder PersonalClaw can write to."
            )
        if trouble == "denied":
            return (
                f"Rsync Sync isn't allowed to write to its local working directory {where}. Fix "
                f"that folder's permissions, or set {setting} to a folder PersonalClaw can write "
                "to."
            )
        if trouble == "in-the-way":
            return (
                f"Rsync Sync couldn't create its local working directory {where}, or a folder in "
                f"it, because a file is in the way. Move that file, or set {setting} to another "
                "folder."
            )
        return (
            f"Rsync Sync couldn't use its local working directory {where}. Check that folder, or "
            f"set {setting} to another one."
        )

    def _run_refused(self, proc: subprocess.CompletedProcess, local: str) -> str:
        """What a run that rsync ended with an error says, where ``local`` is this machine's side
        of it: the mirror a pull comes down into, or the staging folder a registry read or write
        goes through, both under the Local working directory. An error naming it is that
        folder's — its disk full, read-only, or not writable. Anything else is the target's, or
        the ssh under rsync, said as for any run."""
        words = _words(proc)
        if local not in words:
            return self._refused(proc)
        low = words.lower()
        if "no space left on device" in low:
            return self._workdir_says("full")
        if "read-only file system" in low:
            return self._workdir_says("read-only")
        if "permission denied" in low:
            return self._workdir_says("denied")
        return self._workdir_says("")

    def _stage(self, prefix: str) -> str:
        """A fresh staging folder under the Local working directory."""
        try:
            return tempfile.mkdtemp(prefix=prefix, dir=_ensure_dir(self._staging_root))
        except OSError as e:
            raise WorkdirUnusable(self._workdir_unusable(e), e) from e

    def _stage_file(self, stage: str, key: str, data: bytes) -> None:
        """Write one object into ``stage`` at its key."""
        target = os.path.join(stage, *key.split("/"))
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as fh:
                fh.write(data)
        except OSError as e:
            raise WorkdirUnusable(self._workdir_unusable(e), e) from e

    @staticmethod
    def _not_staged(unusable: WorkdirUnusable) -> PushResult:
        """A push the Local working directory stopped before rsync ran. ``transient``: the
        objects wait in the outbox until the folder is fixed, which the sentence says how to do."""
        return PushResult(
            outcome="transient",
            detail=sentence_with_detail(f"{unusable.sentence} {_RETRIES}", unusable.failure),
        )

    def _timed_out(self) -> str:
        """What an rsync run stopped at Command timeout says."""
        if self._host:
            return (
                f"rsync didn't finish within {self._timeout} seconds. If it keeps happening, "
                f"check that {self._ssh_command()} logs in from a terminal here — a host that "
                "doesn't answer keeps rsync waiting — or, for a slow link, raise Command timeout "
                f"{_ON_CARD}."
            )
        return (
            f"rsync didn't finish within {self._timeout} seconds. If it keeps happening, check "
            f"that the disk holding the sync root path {self._path} is connected and responding, "
            f"or raise Command timeout {_ON_CARD}."
        )

    def _refused(self, proc: subprocess.CompletedProcess) -> str:
        """What a run that rsync, or the ssh under it, ended with an error means, and what to do.
        The sentence alone: the caller says whether the cycle retries, and adds rsync's words."""
        low = f"{proc.stderr or ''}\n{proc.stdout or ''}".lower()
        host = self._host.rsplit("@", 1)[-1]
        if self._host:
            if any(needle in low for needle in _HOST_KEY_CHANGED):
                return (
                    f"SSH refused {host} because its host key has changed since this machine last "
                    "trusted it. Only if you know why it changed, remove its old key from this "
                    f"machine's known_hosts file, then run {self._ssh_command()} once from a "
                    "terminal here to accept the new one."
                )
            if any(needle in low for needle in _HOST_KEY):
                return (
                    f"SSH on this machine doesn't trust {host}'s host key yet. Run "
                    f"{self._ssh_command()} once from a terminal here to accept it."
                )
            if any(needle in low for needle in _SHELL_NOT_CLEAN):
                return (
                    f"Something on {host} prints text when rsync logs in over SSH — a login "
                    "message, or output from a shell startup file — and it garbles rsync's "
                    f"connection. Stop that output for non-interactive logins on {host}."
                )
            if any(needle in low for needle in _NO_RSYNC_THERE):
                return (
                    f"{host} couldn't run rsync — it isn't installed there, or isn't on the PATH "
                    f"its SSH logins get. Install rsync on {host}."
                )
            if any(needle in low for needle in _UNREACHABLE):
                return (
                    f"{host} couldn't be reached from this machine. Check that it is online, and "
                    f"that SSH host and SSH port {_ON_CARD} are right."
                )
            if _LOGIN_REFUSED.search(low):
                return (
                    f"{host} turned down this machine's SSH login. Check that "
                    f"{self._ssh_command()} logs in from a terminal here without asking for "
                    f"anything, or set SSH identity file {_ON_CARD} to a key {host} accepts."
                )
        where = f"on {host}" if self._host else "on this machine"
        if "no such file or directory" in low:
            create = (
                "Create that folder there"
                if self._host
                else "Create that folder or reconnect the drive it lives on"
            )
            return (
                f"The sync root path {self._path} doesn't exist {where}. {create}, or set Sync "
                f"root path {_ON_CARD} to one that exists."
            )
        if "read-only file system" in low:
            return (
                f"The sync root path {self._path} {where} is on a read-only disk or mount. Make "
                f"it writable, or set Sync root path {_ON_CARD} to a writable folder."
            )
        if "no space left on device" in low:
            return (
                f"The disk holding the sync root path {self._path} {where} is full. Free some "
                "space on it."
            )
        if "permission denied" in low:
            who = "this machine's SSH login" if self._host else "PersonalClaw"
            return (
                f"The sync root path {self._path} {where} doesn't let {who} read or write it. "
                f"Fix that folder's permissions, or set Sync root path {_ON_CARD} to one {who} "
                "can write to."
            )
        if self._host:
            return (
                f"rsync couldn't sync with {host} (rsync exit {proc.returncode}). Check SSH host "
                f"and Sync root path {_ON_CARD}, and that rsync is installed on both machines."
            )
        return (
            f"rsync couldn't sync with the sync root path {self._path} (rsync exit "
            f"{proc.returncode}). Check Sync root path {_ON_CARD}."
        )

    def _missing(self, proc: subprocess.CompletedProcess, name: str = "") -> bool:
        """Whether a failed run says the sync root path — or the file ``name`` in it — isn't
        there: "No such file or directory" on a line naming that path.

        Each rsync words it its own way — GNU rsync ``change_dir "/srv/sync" failed: No such
        file or directory (2)``, openrsync ``error: /srv/sync/: (l)stat: No such file or
        directory`` — so it is read off the path and the error, not the line's shape, and by GNU
        rsync's errno too, for a host whose rsync speaks another language. A path the host's
        shell resolves, under ``~`` or relative to the SSH login's home, rsync names in full, so
        only its part after that is matched."""
        path = self._path.rstrip("/")
        if name:
            path = f"{path}/{name}" if path else name
        loose = not path.startswith("/")
        if path.startswith("~"):
            path = path[1:].lstrip("/")
        if not path:
            return False  # the login's own home, which is always there
        lead = r"(?:\S*/)?" if loose else ""
        named = re.compile(rf"(?:^|[\s\"']){lead}{re.escape(path)}/?(?=[\"':\s]|$)")
        return any(
            named.search(line)
            and ("no such file or directory" in line.lower() or _ENOENT.search(line))
            for line in _words(proc).splitlines()
        )

    # ── SyncTransportProvider contract ───────────────────────────────────────────────

    def push(self, objects: list[SyncObject]) -> PushResult:
        if not self.configured:
            return PushResult(outcome="transient", detail=self._unconfigured_detail())
        if not objects:
            return PushResult(outcome="delivered")
        # A FRESH staging tree per push holds only the objects being pushed, so the itemize
        # output maps one-to-one onto them. Reusing one growing directory would make every
        # cycle re-consider every object ever pushed.
        try:
            stage = self._stage("push-")
        except WorkdirUnusable as e:
            return self._not_staged(e)
        try:
            try:
                for obj in objects:
                    self._stage_file(stage, obj.key, obj.data)
            except WorkdirUnusable as e:
                return self._not_staged(e)
            args = [
                "-rt",
                "--itemize-changes",
                # Insert-only: a key already on the target is skipped, never overwritten,
                # so a retried push is free. This is the contract, not an optimisation.
                "--ignore-existing",
                *self._rsh_arg(),
                "--",
                f"{stage}/",
                self._target(),
            ]
            try:
                proc = self._run(args)
            except subprocess.TimeoutExpired as e:
                return PushResult(
                    outcome="transient",
                    detail=sentence_with_detail(f"{self._timed_out()} {_RETRIES}", e),
                )
            except OSError as e:
                return PushResult(outcome="permanent", detail=self._cannot_start(e))
            if proc.returncode != 0:
                outcome = _outcome_for_rsync(proc.returncode)
                sentence = self._refused(proc)
                if outcome == "transient":
                    sentence = f"{sentence} {_RETRIES}"
                return PushResult(
                    outcome=outcome, detail=sentence_with_detail(sentence, _words(proc))
                )
            transferred = _transferred_paths(proc.stdout)
            pushed = sum(1 for o in objects if o.key in transferred)
            return PushResult(
                pushed=pushed, skipped=len(objects) - pushed, outcome="delivered"
            )
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def list_remote(self, prefix: str = "") -> list[RemoteRef]:
        # EMPTY only while there is nothing there yet: an unconfigured transport, or a sync root
        # path that doesn't exist yet, which the first machine's first push creates. A listing
        # that fails otherwise raises, said as what went wrong — see :class:`RsyncFailed`.
        if not self.configured:
            return []
        proc = self._run_or_fail(["-r", "--list-only", *self._rsh_arg(), "--", self._target()])
        # 24 is files that vanished while rsync listed them; the rest of the listing stands.
        if proc.returncode not in (0, 24):
            if self._missing(proc):
                return []
            raise RsyncFailed(self._refused(proc), _words(proc))
        refs: list[RemoteRef] = []
        for key, size, fingerprint in _parse_listing(proc.stdout):
            if not key.startswith(prefix):
                continue
            refs.append(RemoteRef(key=key, size=size, fingerprint=fingerprint))
        return refs

    def pull(self, refs: list[RemoteRef]) -> list[SyncObject]:
        if not self.configured or not refs:
            return []
        # ONE invocation brings the whole tree into the persistent mirror; rsync transfers
        # only what changed, which is the entire reason to use rsync rather than N fetches.
        try:
            mirror = _ensure_dir(self._mirror)
        except OSError as e:
            # Not an absence to drop the refs for: the target was never asked. Raised, said as
            # what to fix, for the cycle to report.
            raise WorkdirUnusable(self._workdir_unusable(e), e) from e
        # A run that fails is not an empty remote: raised, said as what went wrong, for the
        # cycle to record — see :class:`RsyncFailed`.
        proc = self._run_or_fail(["-rt", *self._rsh_arg(), "--", self._target(), f"{mirror}/"])
        # 24 is rsync's "some files vanished before they could be transferred" — another
        # machine rewriting the registry through a temporary file while this one copied. The
        # rest arrived, and a ref that didn't is dropped below like any the target no longer has.
        if proc.returncode not in (0, 24):
            raise RsyncFailed(self._run_refused(proc, mirror), _words(proc))
        out: list[SyncObject] = []
        for ref in refs:
            local = os.path.join(mirror, *ref.key.split("/"))
            # Never let a crafted key escape the mirror (a ".." in a remote listing).
            if not _within(mirror, local):
                continue
            try:
                with open(local, "rb") as fh:
                    out.append(SyncObject(key=ref.key, data=fh.read()))
            except OSError:
                continue  # a ref the target no longer has — dropped, not raised
        return out

    def cas_registry(self, expected_sha: str | None, data: bytes) -> bool:
        """Compare-and-swap ``registry.json``; see the module docstring on why this is
        verify-write-verify rather than a real CAS.

        ``False`` is a lost race and nothing else: a registry already there when this machine
        expected none, one with other bytes than the caller expected — before this machine's
        write or after it — or none there when it expected one. An rsync run that fails raises
        :class:`RsyncFailed`, and a Local working directory that refuses the staging raises
        :class:`WorkdirUnusable`: re-pulling and swapping again fixes neither, and a ``False``
        would send core round its loop to no purpose, then report the swap lost to another
        machine."""
        if not self.configured:
            return False
        if expected_sha is None:
            return self._create_only_registry(data)
        current = self._read_remote_registry()
        if current is None or hashlib.sha256(current).hexdigest() != expected_sha:
            # Gone, or swapped by another machine since the caller read it — a lost race.
            return False
        self._write_registry(data)
        # READ-BACK VERIFY. Without --ignore-times rsync would have skipped a same-length
        # same-second rewrite and still exited 0; and with no CAS, a peer may have written
        # between our check and our write. Both show up here as bytes that are not ours.
        return self._read_remote_registry() == data

    # ── registry helpers ─────────────────────────────────────────────────────────────

    def _create_only_registry(self, data: bytes) -> bool:
        """Create ``registry.json`` only if absent, reporting whether WE created it.

        ``--ignore-existing`` will not overwrite, and ``--itemize-changes`` names the files
        actually transferred — so an empty itemize means the file was already there and this
        machine lost the race. A run that fails raises.
        """
        stage = self._stage("reg-")
        try:
            self._stage_file(stage, _REGISTRY_KEY, data)
            args = [
                "-rt",
                "--itemize-changes",
                "--ignore-existing",
                *self._rsh_arg(),
                "--",
                f"{stage}/",
                self._target(),
            ]
            proc = self._run_or_fail(args)
            if proc.returncode != 0:
                raise RsyncFailed(self._run_refused(proc, stage), _words(proc))
            return _REGISTRY_KEY in _transferred_paths(proc.stdout)
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def _read_remote_registry(self) -> bytes | None:
        """The target's current ``registry.json`` bytes, or None when it has none — the file
        isn't there, or the sync root path isn't. A run that fails otherwise raises."""
        stage = self._stage("regr-")
        try:
            src = self._target(trailing_slash=False) + "/" + _REGISTRY_KEY
            # --ignore-times so a stale same-size local copy can never stand in for the
            # target's real bytes (the staging dir is fresh, but the flag states the intent).
            args = ["-t", "--ignore-times", *self._rsh_arg(), "--", src, f"{stage}/"]
            proc = self._run_or_fail(args)
            # 24: the file vanished as rsync copied it, which the read below finds.
            if proc.returncode not in (0, 24):
                if self._missing(proc, _REGISTRY_KEY):
                    return None
                raise RsyncFailed(self._run_refused(proc, stage), _words(proc))
            try:
                with open(os.path.join(stage, _REGISTRY_KEY), "rb") as fh:
                    return fh.read()
            except FileNotFoundError:
                return None
            except OSError as e:
                raise WorkdirUnusable(self._workdir_unusable(e), e) from e
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def _write_registry(self, data: bytes) -> None:
        """Overwrite ``registry.json`` on the target, forcing the transfer. A run that fails
        raises.

        ``--ignore-times`` is load-bearing, not defensive: rsync's size+mtime quick check
        silently skips a same-length rewrite inside the same clock second and still exits 0.
        A registry going from ``{"seq":19}`` to ``{"seq":20}`` is exactly that shape.
        """
        stage = self._stage("regw-")
        try:
            self._stage_file(stage, _REGISTRY_KEY, data)
            args = [
                "-rt",
                "--ignore-times",
                *self._rsh_arg(),
                "--",
                f"{stage}/",
                self._target(),
            ]
            proc = self._run_or_fail(args)
            if proc.returncode != 0:
                raise RsyncFailed(self._run_refused(proc, stage), _words(proc))
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    # ── reachability ─────────────────────────────────────────────────────────────────

    def test(self) -> ConnectionResult:
        if not self.configured:
            return ConnectionResult(ok=False, detail=self._unconfigured_detail())
        # A recursive listing is the cheapest command that exercises ssh, auth, the host key
        # and the path all at once. For a local target it also proves the path exists.
        args = ["-r", "--list-only", *self._rsh_arg(), "--", self._target()]
        try:
            proc = self._run(args)
        except subprocess.TimeoutExpired as e:
            return ConnectionResult(ok=False, detail=sentence_with_detail(self._timed_out(), e))
        except OSError as e:
            return ConnectionResult(ok=False, detail=self._cannot_start(e))
        where = self._target(trailing_slash=False)
        if proc.returncode == 0:
            return ConnectionResult(
                ok=True,
                detail=f"sync root reachable at {where}",
                extra={"host": self._host, "path": self._path, "local": not self._host},
            )
        return ConnectionResult(
            ok=False, detail=sentence_with_detail(self._refused(proc), _words(proc))
        )


# ── module helpers ───────────────────────────────────────────────────────────────────


def _ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def _within(root: str, candidate: str) -> bool:
    """True when ``candidate`` really lives under ``root`` (no ``..`` escape)."""
    root_abs = os.path.realpath(root)
    cand_abs = os.path.realpath(candidate)
    return cand_abs == root_abs or cand_abs.startswith(root_abs + os.sep)


def _transferred_paths(stdout: str) -> set[str]:
    """Which FILES an ``--itemize-changes`` run actually transferred.

    An itemize line is ``<11-char change flags> <path>``; the first character is the update
    type and the second the entry type, so a regular file that moved reads ``>f...``. Lines
    for directories (``cd+++++++``) and any non-itemize chatter are ignored — counting a
    directory as a pushed object would inflate every push count.
    """
    out: set[str] = set()
    for line in stdout.splitlines():
        parts = line.split(" ", 1)
        if len(parts) != 2:
            continue
        flags, path = parts[0], parts[1].strip()
        if len(flags) < 2 or flags[1] != "f":
            continue
        if flags[0] not in "><ch.":
            continue
        out.add(path.lstrip("./") if path.startswith("./") else path)
    return out


def _parse_listing(stdout: str) -> list[tuple[str, int, str]]:
    """Parse ``rsync --list-only`` output into ``(key, size, fingerprint)`` triples.

    A line is ``<perms> <size> <date> <time> <path>``. Directories (``d`` permissions) and
    the ``.`` root entry are dropped — only real objects are refs. The date+time is used as
    the change fingerprint, which the cycle compares and never parses.
    """
    rows: list[tuple[str, int, str]] = []
    for line in stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        perms, size_s, date_s, time_s, path = parts
        if not perms or perms[0] == "d":
            continue  # a directory is not an object
        if len(perms) < 10:
            continue  # not a listing line
        path = path.strip()
        if path in (".", "") or path.startswith("./"):
            path = path[2:] if path.startswith("./") else path
        if not path or path == ".":
            continue
        try:
            size = int(size_s.replace(",", ""))
        except ValueError:
            continue
        rows.append((path, size, f"{date_s} {time_s}"))
    return rows


def _words(proc: subprocess.CompletedProcess) -> str:
    """What rsync, and the ssh under it, said about a failed run — its stderr, else its stdout —
    kept as the detail after the sentence."""
    return (proc.stderr or "").strip() or (proc.stdout or "").strip()


def _outcome_for_rsync(code: int) -> str:
    """Map an rsync exit code to the outbox's typed verdict.

    Only the codes that a retry genuinely cannot fix are ``permanent``: a syntax/usage error
    (1), an unsupported action (2), and an unknown option (4) all mean this transport is
    built wrong or the target's rsync is incompatible. Everything else — unreachable host,
    protocol hiccup, partial transfer, timeout — is retried next cycle.
    """
    return "permanent" if code in (1, 2, 4) else "transient"


def create_provider(config: dict[str, Any] | None = None) -> RsyncSyncProvider:
    """Extension factory — builds the rsync transport from user settings."""
    config = config or {}
    return RsyncSyncProvider(
        host=str(config.get("host", "") or ""),
        path=str(config.get("path", "") or ""),
        port=int(config.get("port") or 22),
        ssh_key=str(config.get("ssh_key", "") or ""),
        staging_dir=str(config.get("staging_dir", "") or "~/.personalclaw/sync/rsync-sync"),
        timeout_secs=int(config.get("timeout_secs") or 300),
    )
