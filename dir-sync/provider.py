"""Folder-sync transport — carries durability shard objects through a shared folder.

Point two machines' dir-sync at the same synced folder (a cloud-sync mount, an NFS
share, a mounted USB drive) and the durability layer converges through it, with no
credentials and no server. The folder holds one object per shard key; the transport only
moves bytes — the merge, the machine-seq registry, and the outbox live above it in core.

A push is insert-only and idempotent on the object key: a retried push of an object already
present is a no-op (skipped, never overwritten), so the sync cycle can retry freely after a
CAS race. A synced folder has no cross-process atomic compare-and-swap, so ``cas_registry``
degrades to a rename-based lock (``os.mkdir`` on a lock directory, which is atomic on POSIX
and on the network filesystems people sync through).

Each sync sends this machine's records as one whole copy, and the cycle removes the copies a
newer one replaced (``remove``), so the folder holds about one copy per machine rather than a
copy per sync.

Nothing outside the sync folder is read, written or removed. Whoever else writes the folder can
put a link in it to any file on this machine: read through, that file would come in as another
machine's object, written through, an object would land on it, and removed through, it would go.
So a key is taken only as a path of plain names that stays inside the folder with every link on
the way followed (``personalclaw.sdk.sync.is_path_in_store``), a link at the key itself is never
followed, and a listing follows none; anything else is refused, named, before anything of the
call is read, written or removed (``KeysRefused``), and the sync report says which. A removal
also opens each folder on the way without following a link, so one made a link since its key
was looked at leads nowhere.
"""

import errno
import hashlib
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from personalclaw.sdk.net import sentence_with_detail
from personalclaw.sdk.sync import (
    ConnectionResult,
    KeysRefused,
    PushResult,
    RemoteRef,
    SyncObject,
    SyncTransportProvider,
    is_path_in_store,
    is_safe_relative_path,
)

#: Where this transport's own setting (Sync folder) is set.
_ON_CARD = "on the Folder Sync card in Settings → Providers"
#: Said after a failure the sync cycle retries (a ``transient`` outcome).
_RETRIES = "Sync tries again on its next run."

# Prefix for the in-place temp files our atomic writes create. ``list_remote`` skips any
# file whose basename starts with this so a half-written object is never advertised.
_TMP_PREFIX = ".tmp-"

# The rename-lock directory that serializes registry compare-and-swap within the folder.
_LOCK_DIR = ".registry.lock"

# The single shared registry object every machine compare-and-swaps.
_REGISTRY_KEY = "registry.json"

#: Why a key is refused (``KeysRefused``), as the sync report says it after the key.
_NOT_A_PATH = "is not a path a sync may use in the sync folder"
_LEADS_OUT = "leads out of the sync folder through a link"
_A_LINK = "is a link in the sync folder, which Folder Sync doesn't follow"
_NOT_A_FILE = "is not a regular file in the sync folder, which Folder Sync doesn't read"

#: Opens a file without following a link there: one put there since the key was looked at.
_NO_FOLLOW = getattr(os, "O_NOFOLLOW", 0)
#: Opens without waiting: opening a named pipe to read otherwise waits for a writer, forever.
_NO_WAIT = getattr(os, "O_NONBLOCK", 0)

#: Opens a folder, and only a folder.
_A_FOLDER = getattr(os, "O_DIRECTORY", 0)

#: Whether this system removes a file through the folders it opened (``unlinkat``), which is
#: what keeps a removal from following a link put on the way since its key was looked at. One
#: that can't keeps every copy, and says so (``removes_old_copies``).
_REMOVES_SAFELY = bool(_NO_FOLLOW and _A_FOLDER) and all(
    call in os.supports_dir_fd for call in (os.open, os.unlink, os.rmdir)
)


class _NotAFile(OSError):
    """What :meth:`DirSyncProvider._read` raises for anything but a regular file at a key: a
    named pipe, a socket or a device, which something other than PersonalClaw put there."""


class DirSyncProvider(SyncTransportProvider):
    """A durability sync transport backed by a shared/synced local folder."""

    name = "dir-sync"
    display_name = "Folder Sync"
    removes_old_copies = _REMOVES_SAFELY

    def __init__(self, root: str = "") -> None:
        # Expand ``~`` and ``$VARS`` so a configured "~/synced/personalclaw" or
        # "$HOME/sync" resolves to a real path. An empty root stays empty — the provider
        # still constructs, but every method treats it as unreachable rather than crashing.
        self._root = os.path.expandvars(os.path.expanduser(root)) if root else ""

    # ── internal helpers ─────────────────────────────────────────────────────────────

    def _no_root(self) -> str:
        """What a sync folder that isn't there says. Folder Sync never creates it: it may be a
        drive or share that isn't mounted, or a mistyped path, and a folder made in its place
        would quietly take the sync onto this machine's own disk."""
        return (
            f"The sync folder at {self._root} isn't there. If it lives on a drive or a share "
            "that isn't mounted, mount it; if it's the folder you meant, create it; otherwise "
            f"set Sync folder {_ON_CARD} to the right folder."
        )

    def _folder_trouble(self, failure: OSError) -> str:
        """What an ``OSError`` from the sync folder means, and what to do about it — the OS's
        own words ("[Errno 13] Permission denied: '…'") say neither.

        Whether the folder is still there is asked of the folder, not read off the error: an
        unplugged drive's mount point is gone, so recreating it under a root-owned parent (as
        macOS's ``/Volumes`` is) fails with "Permission denied", not "No such file"."""
        if failure.errno == errno.ENOSPC:
            return f"The disk holding the sync folder at {self._root} is full. Free some space."
        if failure.errno == errno.EROFS:
            return (
                f"The sync folder at {self._root} is on a read-only disk or mount. Make it "
                f"writable, or set Sync folder {_ON_CARD} to a writable folder."
            )
        if os.path.exists(self._root) and not os.path.isdir(self._root):
            return (
                f"The sync folder at {self._root} is a file, not a folder. Set Sync folder "
                f"{_ON_CARD} to a folder."
            )
        if not os.path.isdir(self._root):
            return self._no_root()
        if isinstance(failure, PermissionError):
            return (
                f"Folder Sync isn't allowed to write to the sync folder at {self._root}. Fix that "
                f"folder's permissions, or set Sync folder {_ON_CARD} to one PersonalClaw can "
                "write to."
            )
        return (
            f"Folder Sync couldn't write to the sync folder at {self._root}. Check that folder, "
            f"or set Sync folder {_ON_CARD} to another folder."
        )

    def _path(self, key: str) -> tuple[str, str]:
        """``(path, "")``: where *key* is in the sync folder; or ``("", why)`` when Folder Sync
        won't read or write there — a key that is not a path of plain names, one that leads out
        of the folder through a link, or a link itself."""
        if not is_safe_relative_path(key):
            return "", _NOT_A_PATH
        if not is_path_in_store(Path(self._root), key):
            return "", _LEADS_OUT
        # Split on "/" and rejoin with the OS separator so nested keys land in real
        # subdirectories regardless of platform.
        path = os.path.join(self._root, *key.split("/"))
        if os.path.islink(path):
            return "", _A_LINK
        return path, ""

    def _paths(self, keys: list[str], doing: str) -> dict[str, str]:
        """Each of *keys*' path in the sync folder, or :class:`KeysRefused` naming every one
        Folder Sync won't read or write, before anything is: for *doing* them (``"read"``)."""
        paths: dict[str, str] = {}
        refused: dict[str, str] = {}
        for key in keys:
            path, why = self._path(key)
            if why:
                refused[key] = why
            else:
                paths[key] = path
        if refused:
            raise self._refusal(refused, doing)
        return paths

    def _refusal(self, refused: dict[str, str], doing: str) -> KeysRefused:
        """*refused*, each key with why, as the refusal to *doing* them, said with what to do."""
        named = "; ".join(f"{key} {why}" for key, why in sorted(refused.items())[:3])
        more = f"; and {len(refused) - 3} more" if len(refused) > 3 else ""
        what = "a key" if len(refused) == 1 else f"{len(refused)} keys"
        step = ""
        if any(why in (_LEADS_OUT, _A_LINK) for why in refused.values()):
            step = " Take the link out of the sync folder: Folder Sync follows none out of it."
        if _NOT_A_FILE in refused.values():
            step += " Take it out of the sync folder: Folder Sync reads only regular files."
        return KeysRefused(
            f"Folder Sync won't {doing} {what} in {self._root}: {named}{more}.{step}", refused
        )

    @staticmethod
    def _read(path: str) -> bytes:
        """The bytes of the file at *path*, opened without following a link there: a link put
        there since its key was looked at raises ``OSError`` (``ELOOP``). Opened without waiting,
        and read only if it is a regular file (:class:`_NotAFile` otherwise): a named pipe there
        held every sync run after it, and the durability jobs behind them, until a restart."""
        fd = os.open(path, os.O_RDONLY | _NO_FOLLOW | _NO_WAIT)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise _NotAFile(f"{path} is not a regular file")
        except BaseException:
            os.close(fd)
            raise
        with os.fdopen(fd, "rb") as fh:
            return fh.read()

    def _make_folders(self, folder: str) -> None:
        """Make ``folder`` and the folders between it and the sync folder, never the sync folder
        itself (:meth:`_no_root`). One gone since it was looked at fails the first ``mkdir``, and
        a folder outside it is refused (``_paths`` already keeps every key inside)."""
        here = self._root
        rel = os.path.relpath(folder, self._root)
        if rel == os.pardir or rel.startswith(os.pardir + os.sep):
            raise OSError(errno.EINVAL, "not a folder in the sync folder", folder)
        for name in [] if rel == os.curdir else rel.split(os.sep):
            here = os.path.join(here, name)
            try:
                os.mkdir(here)
            except FileExistsError:
                pass

    def _atomic_write(self, target: str, data: bytes) -> None:
        """Write ``data`` to ``target`` atomically via a temp file in the same dir."""
        parent = os.path.dirname(target)
        self._make_folders(parent)
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
        if not os.path.lexists(self._root):
            return PushResult(outcome="transient", detail=f"{self._no_root()} {_RETRIES}")
        paths = self._paths([obj.key for obj in objects], "write")
        pushed = skipped = 0
        try:
            for obj in objects:
                target = paths[obj.key]
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
                pushed=pushed,
                skipped=skipped,
                outcome="transient",
                detail=sentence_with_detail(f"{self._folder_trouble(e)} {_RETRIES}", e),
            )
        return PushResult(pushed=pushed, skipped=skipped, outcome="delivered")

    def list_remote(self, prefix: str = "") -> list[RemoteRef]:
        # A missing root is an empty remote, not an error — the folder may not have synced
        # down yet on a fresh machine.
        if not self._root or not os.path.isdir(self._root):
            return []
        refs: list[RemoteRef] = []
        refused: dict[str, str] = {}
        # os.walk follows no link to a folder; one to anything is named here, and never listed.
        for dirpath, dirnames, filenames in os.walk(self._root):
            for name in [*dirnames, *filenames]:
                full = os.path.join(dirpath, name)
                if not os.path.islink(full):
                    continue
                key = os.path.relpath(full, self._root).replace(os.sep, "/")
                # One under the prefix, or one on the way to it, which hides what is under it.
                if key.startswith(prefix) or prefix.startswith(f"{key}/"):
                    refused[key] = _A_LINK
            for fn in filenames:
                if fn.startswith(_TMP_PREFIX):
                    continue  # our own half-written object — not a real remote entry
                full = os.path.join(dirpath, fn)
                # Key is the path relative to the root, always in posix form.
                key = os.path.relpath(full, self._root).replace(os.sep, "/")
                if not key.startswith(prefix) or key in refused:
                    continue
                try:
                    st = os.lstat(full)
                except OSError:
                    continue  # vanished between walk and stat — skip it
                if not stat.S_ISREG(st.st_mode):
                    # A named pipe, a socket or a device: never an object, and never read.
                    refused[key] = _NOT_A_FILE
                    continue
                # mtime is a cheap change fingerprint; the cycle compares it, never parses.
                refs.append(
                    RemoteRef(key=key, size=st.st_size, fingerprint=str(int(st.st_mtime)))
                )
        if refused:
            raise self._refusal(refused, "list")
        return refs

    def pull(self, refs: list[RemoteRef]) -> list[SyncObject]:
        if not self._root:
            return []
        paths = self._paths([ref.key for ref in refs], "read")
        out: list[SyncObject] = []
        refused: dict[str, str] = {}
        for ref in refs:
            try:
                out.append(SyncObject(key=ref.key, data=self._read(paths[ref.key])))
            except OSError as e:
                if isinstance(e, _NotAFile):
                    refused[ref.key] = _NOT_A_FILE  # put there since it was looked at
                elif e.errno == errno.ELOOP:
                    refused[ref.key] = _A_LINK  # made a link since it was looked at
                # A ref the folder no longer has (or can't read) is dropped, not raised —
                # the caller reconciles against what it asked for.
                continue
        if refused:
            raise self._refusal(refused, "read")
        return out

    def cas_registry(self, expected_sha: str | None, data: bytes) -> bool:
        if not self._root:
            return False
        target = self._paths([_REGISTRY_KEY], "swap")[_REGISTRY_KEY]
        if not os.path.isdir(self._root):
            return False  # never made here (_no_root): the push says why
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
            if os.path.lexists(target):
                try:
                    current = self._read(target)
                except OSError as e:
                    if isinstance(e, _NotAFile):  # a named pipe, a socket, a device
                        raise self._refusal({_REGISTRY_KEY: _NOT_A_FILE}, "swap") from None
                    if e.errno == errno.ELOOP:  # made a link since it was looked at
                        raise self._refusal({_REGISTRY_KEY: _A_LINK}, "swap") from None
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

    def remove(self, keys: list[str]) -> int:
        """Remove the objects at *keys* from the sync folder, and the folders that leaves empty
        (never the sync folder itself); return how many were there. A key already gone is no
        error. One Folder Sync won't remove — not a path of plain names, leading out of the
        folder through a link, or a link itself — is refused before any is removed."""
        if not self._root or not keys:
            return 0
        self._paths(keys, "remove")
        removed = 0
        for key in keys:
            removed += self._unlink(key)
        return removed

    def _unlink(self, key: str) -> int:
        """Remove the file at *key*: 1 when it was there, 0 when it wasn't. Each folder on the way
        is opened without following a link, and the file removed through the last of them, so a
        folder made a link since *key* was looked at stops it here, refused, with nothing removed
        through it; then each folder the removal emptied goes, deepest first."""
        parts = key.split("/")
        try:
            fds = [os.open(self._root, os.O_RDONLY | _A_FOLDER)]
        except FileNotFoundError:
            return 0
        try:
            for name in parts[:-1]:
                try:
                    fds.append(
                        os.open(name, os.O_RDONLY | _A_FOLDER | _NO_FOLLOW, dir_fd=fds[-1])
                    )
                except FileNotFoundError:
                    return 0
                except OSError as e:
                    if e.errno in (errno.ELOOP, errno.ENOTDIR):  # made a link since
                        raise self._refusal({key: _LEADS_OUT}, "remove") from None
                    raise
            try:
                os.unlink(parts[-1], dir_fd=fds[-1])  # a link there is the link, never its file
            except FileNotFoundError:
                return 0
            for depth in range(len(parts) - 1, 0, -1):
                try:
                    os.rmdir(parts[depth - 1], dir_fd=fds[depth - 1])
                except OSError:
                    break  # not empty (another object, a file the sync service keeps), or gone
            return 1
        finally:
            for fd in reversed(fds):
                os.close(fd)

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
            # Never created here: it may be a drive or share that isn't mounted (_no_root).
            return ConnectionResult(ok=False, detail=self._no_root())
        except OSError as e:
            return ConnectionResult(
                ok=False, detail=sentence_with_detail(self._folder_trouble(e), e)
            )


def create_provider(config: dict[str, Any] | None = None) -> DirSyncProvider:
    """Extension factory — builds the folder-sync transport from user settings."""
    config = config or {}
    return DirSyncProvider(root=str(config.get("root", "") or ""))
