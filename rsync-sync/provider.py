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
verifies, writes, and re-reads — and reports failure whenever it cannot prove its own bytes
landed. That bias is deliberate: core's CAS loop re-pulls, re-merges peers' entries and
retries on a ``False``, so a false ``False`` costs one round trip, while a false ``True``
silently discards another machine's registration.
"""

import hashlib
import os
import re
import shutil
import subprocess  # noqa: S404 — argv-only, shell=False; see the module docstring
import tempfile
from typing import Any

from personalclaw.sdk.sync import (
    ConnectionResult,
    PushResult,
    RemoteRef,
    SyncObject,
    SyncTransportProvider,
)

#: The single shared registry object every machine compare-and-swaps.
_REGISTRY_KEY = "registry.json"

#: Hostnames (and the optional ``user@``) may contain only these characters. Deliberately
#: strict: anything outside this set is either meaningless to ssh or a way to smuggle an
#: option/shell metacharacter into rsync's own operand parser.
_HOST_RE = re.compile(r"^(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9._-]+$")

#: An identity-file path may not contain whitespace or quotes, because it is embedded in the
#: single string rsync hands to its remote-shell command and rsync splits that string itself.
_KEY_PATH_RE = re.compile(r"^[A-Za-z0-9._~/@+-]+$")


class RsyncConfigError(Exception):
    """A setting that cannot be used safely. Raised at validation, never at transfer time."""


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
        """Run one rsync invocation. argv only, no shell, always bounded by a timeout."""
        return subprocess.run(  # noqa: S603 — argv list, shell=False, operands validated
            [self._rsync, *args],
            capture_output=True,
            text=True,
            timeout=self._timeout,
            shell=False,
            check=False,
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
        stage = tempfile.mkdtemp(prefix="push-", dir=_ensure_dir(self._staging_root))
        try:
            for obj in objects:
                target = os.path.join(stage, *obj.key.split("/"))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as fh:
                    fh.write(obj.data)
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
            except subprocess.TimeoutExpired:
                return PushResult(
                    outcome="transient", detail=f"rsync timed out after {self._timeout}s"
                )
            except OSError as e:
                return PushResult(outcome="permanent", detail=f"cannot run rsync: {e}")
            if proc.returncode != 0:
                return PushResult(
                    outcome=_outcome_for_rsync(proc.returncode),
                    detail=f"rsync exit {proc.returncode}: {_first_error(proc)}",
                )
            transferred = _transferred_paths(proc.stdout)
            pushed = sum(1 for o in objects if o.key in transferred)
            return PushResult(
                pushed=pushed, skipped=len(objects) - pushed, outcome="delivered"
            )
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def list_remote(self, prefix: str = "") -> list[RemoteRef]:
        # An unreachable target lists as EMPTY, not as an error: a fresh machine legitimately
        # has nothing there yet, and the cycle reconciles against what it asked for.
        if not self.configured:
            return []
        args = ["-r", "--list-only", *self._rsh_arg(), "--", self._target()]
        try:
            proc = self._run(args)
        except (subprocess.TimeoutExpired, OSError):
            return []
        if proc.returncode != 0:
            return []
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
        mirror = _ensure_dir(self._mirror)
        args = ["-rt", *self._rsh_arg(), "--", self._target(), f"{mirror}/"]
        try:
            proc = self._run(args)
        except (subprocess.TimeoutExpired, OSError):
            return []
        if proc.returncode != 0:
            return []
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
        verify-write-verify rather than a real CAS."""
        if not self.configured:
            return False
        if expected_sha is None:
            return self._create_only_registry(data)
        current = self._read_remote_registry()
        if current is None:
            # Caller expected specific bytes but we cannot read them — a lost race.
            return False
        if hashlib.sha256(current).hexdigest() != expected_sha:
            return False
        if not self._write_registry(data):
            return False
        # READ-BACK VERIFY. Without --ignore-times rsync would have skipped a same-length
        # same-second rewrite and still exited 0; and with no CAS, a peer may have written
        # between our check and our write. Both show up here as bytes that are not ours.
        after = self._read_remote_registry()
        return after == data

    # ── registry helpers ─────────────────────────────────────────────────────────────

    def _create_only_registry(self, data: bytes) -> bool:
        """Create ``registry.json`` only if absent, reporting whether WE created it.

        ``--ignore-existing`` will not overwrite, and ``--itemize-changes`` names the files
        actually transferred — so an empty itemize means the file was already there and this
        machine lost the race.
        """
        stage = tempfile.mkdtemp(prefix="reg-", dir=_ensure_dir(self._staging_root))
        try:
            with open(os.path.join(stage, _REGISTRY_KEY), "wb") as fh:
                fh.write(data)
            args = [
                "-rt",
                "--itemize-changes",
                "--ignore-existing",
                *self._rsh_arg(),
                "--",
                f"{stage}/",
                self._target(),
            ]
            try:
                proc = self._run(args)
            except (subprocess.TimeoutExpired, OSError):
                return False
            if proc.returncode != 0:
                return False
            return _REGISTRY_KEY in _transferred_paths(proc.stdout)
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def _read_remote_registry(self) -> bytes | None:
        """Fetch the target's current ``registry.json`` bytes, or None if unreadable."""
        stage = tempfile.mkdtemp(prefix="regr-", dir=_ensure_dir(self._staging_root))
        try:
            src = self._target(trailing_slash=False) + "/" + _REGISTRY_KEY
            # --ignore-times so a stale same-size local copy can never stand in for the
            # target's real bytes (the staging dir is fresh, but the flag states the intent).
            args = ["-t", "--ignore-times", *self._rsh_arg(), "--", src, f"{stage}/"]
            try:
                proc = self._run(args)
            except (subprocess.TimeoutExpired, OSError):
                return None
            if proc.returncode != 0:
                return None
            try:
                with open(os.path.join(stage, _REGISTRY_KEY), "rb") as fh:
                    return fh.read()
            except OSError:
                return None
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def _write_registry(self, data: bytes) -> bool:
        """Overwrite ``registry.json`` on the target, forcing the transfer.

        ``--ignore-times`` is load-bearing, not defensive: rsync's size+mtime quick check
        silently skips a same-length rewrite inside the same clock second and still exits 0.
        A registry going from ``{"seq":19}`` to ``{"seq":20}`` is exactly that shape.
        """
        stage = tempfile.mkdtemp(prefix="regw-", dir=_ensure_dir(self._staging_root))
        try:
            with open(os.path.join(stage, _REGISTRY_KEY), "wb") as fh:
                fh.write(data)
            args = [
                "-rt",
                "--ignore-times",
                *self._rsh_arg(),
                "--",
                f"{stage}/",
                self._target(),
            ]
            try:
                proc = self._run(args)
            except (subprocess.TimeoutExpired, OSError):
                return False
            return proc.returncode == 0
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
        except subprocess.TimeoutExpired:
            return ConnectionResult(
                ok=False,
                detail=(
                    f"rsync timed out after {self._timeout}s — with BatchMode set this "
                    "usually means the host is unreachable, not that it asked for a password"
                ),
            )
        except OSError as e:
            return ConnectionResult(ok=False, detail=f"cannot run rsync: {e}")
        where = self._target(trailing_slash=False)
        if proc.returncode == 0:
            return ConnectionResult(
                ok=True,
                detail=f"sync root reachable at {where}",
                extra={"host": self._host, "path": self._path, "local": not self._host},
            )
        return ConnectionResult(
            ok=False, detail=f"{where} unreachable (rsync exit {proc.returncode}): {_first_error(proc)}"
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


def _first_error(proc: subprocess.CompletedProcess) -> str:
    """The most useful single line of a failed rsync's output, for a human-readable detail."""
    for stream in (proc.stderr or "", proc.stdout or ""):
        for line in stream.splitlines():
            if line.strip():
                return line.strip()[:300]
    return "no output"


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
