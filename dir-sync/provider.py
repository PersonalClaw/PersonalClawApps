"""Folder-sync transport — carries durability shard objects through a shared folder.

Point two machines' dir-sync at the same synced folder (a cloud-sync mount, an NFS
share, a mounted USB drive) and the durability layer converges through it, with no
credentials and no server. The folder holds one object per shard key; the transport only
moves bytes — the merge, the machine-seq registry, and the outbox live above it in core.

Every method is insert-only and idempotent on the object key: a retried push of an object
already present is a no-op (skipped, never overwritten), so the sync cycle can retry
freely after a CAS race. A synced folder has no cross-process atomic compare-and-swap, so
``cas_registry`` degrades to a rename-based lock (``os.mkdir`` on a lock directory, which
is atomic on POSIX and on the network filesystems people sync through).
"""

import hashlib
import os
import tempfile
from typing import Any

from personalclaw.sdk.sync import (
    ConnectionResult,
    PushResult,
    RemoteRef,
    SyncObject,
    SyncTransportProvider,
)

# Prefix for the in-place temp files our atomic writes create. ``list_remote`` skips any
# file whose basename starts with this so a half-written object is never advertised.
_TMP_PREFIX = ".tmp-"

# The rename-lock directory that serializes registry compare-and-swap within the folder.
_LOCK_DIR = ".registry.lock"

# The single shared registry object every machine compare-and-swaps.
_REGISTRY_KEY = "registry.json"


class DirSyncProvider(SyncTransportProvider):
    """A durability sync transport backed by a shared/synced local folder."""

    name = "dir-sync"
    display_name = "Folder Sync"

    def __init__(self, root: str = "") -> None:
        # Expand ``~`` and ``$VARS`` so a configured "~/synced/personalclaw" or
        # "$HOME/sync" resolves to a real path. An empty root stays empty — the provider
        # still constructs, but every method treats it as unreachable rather than crashing.
        self._root = os.path.expandvars(os.path.expanduser(root)) if root else ""

    # ── internal helpers ─────────────────────────────────────────────────────────────

    def _resolve(self, key: str) -> str:
        """Map a remote-relative posix key to an absolute path under the root."""
        # Split on "/" and rejoin with the OS separator so nested keys land in real
        # subdirectories regardless of platform.
        return os.path.join(self._root, *key.split("/"))

    def _atomic_write(self, target: str, data: bytes) -> None:
        """Write ``data`` to ``target`` atomically via a temp file in the same dir."""
        parent = os.path.dirname(target)
        os.makedirs(parent, exist_ok=True)
        # Temp file in the SAME directory so os.replace is a rename, not a cross-device
        # copy; the _TMP_PREFIX keeps it recognizable so list_remote can exclude it.
        fd, tmp = tempfile.mkstemp(prefix=_TMP_PREFIX, dir=parent)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, target)
        except BaseException:
            # Never leave a stray temp file behind on any failure.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ── SyncTransportProvider contract ───────────────────────────────────────────────

    def push(self, objects: list[SyncObject]) -> PushResult:
        if not self._root:
            return PushResult(outcome="transient", detail="no sync folder configured")
        pushed = skipped = 0
        try:
            for obj in objects:
                target = self._resolve(obj.key)
                # Insert-only: an object whose key already exists is skipped, not
                # overwritten, so a retried push is free and never duplicates bytes.
                if os.path.exists(target):
                    skipped += 1
                    continue
                self._atomic_write(target, obj.data)
                pushed += 1
        except OSError as e:
            # Root vanished mid-cycle, a permission blip, a full disk — all retryable.
            return PushResult(
                pushed=pushed, skipped=skipped, outcome="transient", detail=str(e)
            )
        return PushResult(pushed=pushed, skipped=skipped, outcome="delivered")

    def list_remote(self, prefix: str = "") -> list[RemoteRef]:
        # A missing root is an empty remote, not an error — the folder may not have synced
        # down yet on a fresh machine.
        if not self._root or not os.path.isdir(self._root):
            return []
        refs: list[RemoteRef] = []
        for dirpath, _dirnames, filenames in os.walk(self._root):
            for fn in filenames:
                if fn.startswith(_TMP_PREFIX):
                    continue  # our own half-written object — not a real remote entry
                full = os.path.join(dirpath, fn)
                # Key is the path relative to the root, always in posix form.
                key = os.path.relpath(full, self._root).replace(os.sep, "/")
                if not key.startswith(prefix):
                    continue
                try:
                    st = os.stat(full)
                except OSError:
                    continue  # vanished between walk and stat — skip it
                # mtime is a cheap change fingerprint; the cycle compares it, never parses.
                refs.append(
                    RemoteRef(key=key, size=st.st_size, fingerprint=str(int(st.st_mtime)))
                )
        return refs

    def pull(self, refs: list[RemoteRef]) -> list[SyncObject]:
        if not self._root:
            return []
        out: list[SyncObject] = []
        for ref in refs:
            try:
                with open(self._resolve(ref.key), "rb") as fh:
                    out.append(SyncObject(key=ref.key, data=fh.read()))
            except OSError:
                # A ref the folder no longer has (or can't read) is dropped, not raised —
                # the caller reconciles against what it asked for.
                continue
        return out

    def cas_registry(self, expected_sha: str | None, data: bytes) -> bool:
        if not self._root:
            return False
        try:
            os.makedirs(self._root, exist_ok=True)
        except OSError:
            return False
        lock = os.path.join(self._root, _LOCK_DIR)
        try:
            # os.mkdir is atomic and fails if the directory already exists, giving us a
            # cross-process rename lock; a held lock means another machine is mid-swap, so
            # we report a lost race and let the caller re-pull and retry.
            os.mkdir(lock)
        except FileExistsError:
            return False
        except OSError:
            return False
        try:
            target = os.path.join(self._root, _REGISTRY_KEY)
            if os.path.exists(target):
                try:
                    with open(target, "rb") as fh:
                        current = fh.read()
                except OSError:
                    return False
                # Present: swap only if the caller's expected sha matches what's there
                # (a None expectation means "expected absent", which a present file fails).
                if expected_sha != hashlib.sha256(current).hexdigest():
                    return False
            elif expected_sha is not None:
                # Absent: only a None expectation ("expected absent") may proceed.
                return False
            try:
                self._atomic_write(target, data)
            except OSError:
                return False
            return True
        finally:
            try:
                os.rmdir(lock)
            except OSError:
                pass

    def test(self) -> ConnectionResult:
        if not self._root:
            return ConnectionResult(ok=False, detail="no sync folder configured")
        try:
            if os.path.isdir(self._root):
                if os.access(self._root, os.W_OK):
                    return ConnectionResult(ok=True, detail=f"sync folder ready at {self._root}")
                return ConnectionResult(
                    ok=False, detail=f"sync folder is not writable: {self._root}"
                )
            if os.path.exists(self._root):
                return ConnectionResult(
                    ok=False, detail=f"sync path is not a directory: {self._root}"
                )
            # Not there yet — the transport creates parents on push, so a creatable path
            # is reachable. Create it now so the probe reflects real writability.
            os.makedirs(self._root, exist_ok=True)
            return ConnectionResult(ok=True, detail=f"sync folder created at {self._root}")
        except OSError as e:
            return ConnectionResult(ok=False, detail=f"sync folder unreachable: {e}")


def create_provider(config: dict[str, Any] | None = None) -> DirSyncProvider:
    """Extension factory — builds the folder-sync transport from user settings."""
    config = config or {}
    return DirSyncProvider(root=str(config.get("root", "") or ""))
