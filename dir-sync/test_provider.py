"""Folder-sync transport tests — pure filesystem, no network.

Every case drives a ``tmp_path`` root so nothing touches a real sync folder. Covers the
insert-only push contract, list_remote prefix filtering + temp-file exclusion, pull's
drop-on-vanish, the rename-locked registry CAS (present/absent/mismatch + round-trip),
and the reachability probe.
"""

from __future__ import annotations

import errno
import hashlib
import os

import pytest

import provider as dir_sync
from provider import DirSyncProvider, create_provider
from personalclaw.sdk.sync import RemoteRef, SyncObject


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── push ─────────────────────────────────────────────────────────────────────────────


def test_push_writes_objects_and_creates_parent_dirs(tmp_path):
    p = DirSyncProvider(str(tmp_path))
    r = p.push([
        SyncObject("machines/abc/seq-0007/tasks/entities.jsonl", b"one"),
        SyncObject("registry.json", b"{}"),
    ])
    assert r.outcome == "delivered"
    assert r.pushed == 2 and r.skipped == 0
    nested = tmp_path / "machines" / "abc" / "seq-0007" / "tasks" / "entities.jsonl"
    assert nested.read_bytes() == b"one"
    assert (tmp_path / "registry.json").read_bytes() == b"{}"


def test_push_is_insert_only_and_idempotent(tmp_path):
    p = DirSyncProvider(str(tmp_path))
    p.push([SyncObject("a/x.jsonl", b"original")])
    # Re-push the same key with different bytes — must be skipped, not overwritten.
    r = p.push([SyncObject("a/x.jsonl", b"CHANGED")])
    assert r.pushed == 0 and r.skipped == 1
    assert r.outcome == "delivered"
    assert (tmp_path / "a" / "x.jsonl").read_bytes() == b"original"


def test_push_mixed_new_and_existing(tmp_path):
    p = DirSyncProvider(str(tmp_path))
    p.push([SyncObject("k1", b"1")])
    r = p.push([SyncObject("k1", b"dup"), SyncObject("k2", b"2")])
    assert r.pushed == 1 and r.skipped == 1


def test_push_empty_root_is_transient(tmp_path):
    r = DirSyncProvider("").push([SyncObject("k", b"v")])
    assert r.outcome == "transient"
    assert "no sync folder" in r.detail


def test_push_leaves_no_temp_files(tmp_path):
    p = DirSyncProvider(str(tmp_path))
    p.push([SyncObject("nested/dir/obj.bin", b"data")])
    # No .tmp- residue anywhere under the root after a clean write.
    stray = [f for _d, _s, fs in os.walk(tmp_path) for f in fs if f.startswith(".tmp-")]
    assert stray == []


# ── list_remote ──────────────────────────────────────────────────────────────────────


def test_list_remote_missing_root_is_empty(tmp_path):
    p = DirSyncProvider(str(tmp_path / "does-not-exist"))
    assert p.list_remote() == []


def test_list_remote_returns_all_with_posix_keys_and_sizes(tmp_path):
    p = DirSyncProvider(str(tmp_path))
    p.push([
        SyncObject("machines/m1/seq-0001/a.jsonl", b"aaaa"),
        SyncObject("registry.json", b"{}"),
    ])
    refs = {r.key: r for r in p.list_remote()}
    assert set(refs) == {"machines/m1/seq-0001/a.jsonl", "registry.json"}
    # posix keys even though they live in real subdirectories
    assert "/" in "machines/m1/seq-0001/a.jsonl"
    assert refs["machines/m1/seq-0001/a.jsonl"].size == 4
    assert refs["registry.json"].fingerprint != ""


def test_list_remote_prefix_filters(tmp_path):
    p = DirSyncProvider(str(tmp_path))
    p.push([
        SyncObject("machines/m1/a", b"1"),
        SyncObject("machines/m2/b", b"2"),
        SyncObject("registry.json", b"{}"),
    ])
    keys = {r.key for r in p.list_remote(prefix="machines/m1/")}
    assert keys == {"machines/m1/a"}


def test_list_remote_excludes_temp_files(tmp_path):
    p = DirSyncProvider(str(tmp_path))
    p.push([SyncObject("real.jsonl", b"x")])
    # Simulate a half-written atomic write left by an interrupted push.
    (tmp_path / ".tmp-halfwritten").write_bytes(b"partial")
    keys = {r.key for r in p.list_remote()}
    assert keys == {"real.jsonl"}


# ── pull ─────────────────────────────────────────────────────────────────────────────


def test_pull_reads_bytes(tmp_path):
    p = DirSyncProvider(str(tmp_path))
    p.push([SyncObject("a/x", b"hello"), SyncObject("b/y", b"world")])
    objs = {o.key: o.data for o in p.pull([RemoteRef("a/x"), RemoteRef("b/y")])}
    assert objs == {"a/x": b"hello", "b/y": b"world"}


def test_pull_drops_vanished_ref(tmp_path):
    p = DirSyncProvider(str(tmp_path))
    p.push([SyncObject("present", b"here")])
    objs = p.pull([RemoteRef("present"), RemoteRef("gone/missing.jsonl")])
    assert [o.key for o in objs] == ["present"]


def test_pull_empty_root_returns_empty(tmp_path):
    assert DirSyncProvider("").pull([RemoteRef("k")]) == []


# ── cas_registry ─────────────────────────────────────────────────────────────────────


def test_cas_registry_absent_succeeds_with_none(tmp_path):
    p = DirSyncProvider(str(tmp_path))
    # expected_sha=None means "expected absent" — succeeds when the file does not exist.
    assert p.cas_registry(None, b'{"v":1}') is True
    assert (tmp_path / "registry.json").read_bytes() == b'{"v":1}'


def test_cas_registry_absent_fails_with_non_none(tmp_path):
    p = DirSyncProvider(str(tmp_path))
    # File is absent but caller expected a concrete sha — lost race, no write.
    assert p.cas_registry(_sha(b"whatever"), b"new") is False
    assert not (tmp_path / "registry.json").exists()


def test_cas_registry_present_matches(tmp_path):
    p = DirSyncProvider(str(tmp_path))
    p.cas_registry(None, b"first")
    assert p.cas_registry(_sha(b"first"), b"second") is True
    assert (tmp_path / "registry.json").read_bytes() == b"second"


def test_cas_registry_present_mismatch_fails(tmp_path):
    p = DirSyncProvider(str(tmp_path))
    p.cas_registry(None, b"first")
    # Stale expectation — the file no longer hashes to what the caller pulled.
    assert p.cas_registry(_sha(b"stale"), b"second") is False
    assert (tmp_path / "registry.json").read_bytes() == b"first"


def test_cas_registry_none_fails_when_present(tmp_path):
    p = DirSyncProvider(str(tmp_path))
    p.cas_registry(None, b"first")
    # "expected absent" but the file is present now — must fail.
    assert p.cas_registry(None, b"second") is False
    assert (tmp_path / "registry.json").read_bytes() == b"first"


def test_cas_registry_round_trip_read_back(tmp_path):
    p = DirSyncProvider(str(tmp_path))
    payload = b'{"machines":{"m1":7}}'
    assert p.cas_registry(None, payload) is True
    refs = [r for r in p.list_remote() if r.key == "registry.json"]
    assert len(refs) == 1
    got = p.pull(refs)
    assert got[0].data == payload


def test_cas_registry_lock_held_is_lost_race(tmp_path):
    p = DirSyncProvider(str(tmp_path))
    # Pre-create the lock dir to simulate another machine mid-swap.
    os.makedirs(tmp_path / ".registry.lock")
    assert p.cas_registry(None, b"data") is False
    # The lock we didn't own is left untouched for its holder.
    assert (tmp_path / ".registry.lock").is_dir()


def test_cas_registry_releases_lock(tmp_path):
    p = DirSyncProvider(str(tmp_path))
    p.cas_registry(None, b"data")
    # Lock removed in the finally so the next swap can acquire it.
    assert not (tmp_path / ".registry.lock").exists()
    assert p.cas_registry(_sha(b"data"), b"next") is True


def test_lock_dir_not_listed_as_remote(tmp_path):
    p = DirSyncProvider(str(tmp_path))
    os.makedirs(tmp_path / ".registry.lock")
    # The lock is a directory, not a file, so os.walk never yields it as an entry.
    assert all(not r.key.startswith(".registry.lock") for r in p.list_remote())


# ── test() reachability probe ────────────────────────────────────────────────────────


def test_probe_ok_on_writable_dir(tmp_path):
    res = DirSyncProvider(str(tmp_path)).test()
    assert res.ok is True
    assert str(tmp_path) in res.detail


def test_probe_creates_missing_dir(tmp_path):
    target = tmp_path / "fresh" / "sync"
    res = DirSyncProvider(str(target)).test()
    assert res.ok is True
    assert target.is_dir()


def test_probe_not_ok_on_empty_config():
    res = DirSyncProvider("").test()
    assert res.ok is False
    assert "no sync folder" in res.detail


def test_probe_not_ok_when_path_is_a_file(tmp_path):
    f = tmp_path / "afile"
    f.write_text("x")
    res = DirSyncProvider(str(f)).test()
    assert res.ok is False
    assert "not a directory" in res.detail


# ── factory + config ─────────────────────────────────────────────────────────────────


def test_create_provider_expands_user(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    p = create_provider({"root": "~/mysync"})
    assert p._root == str(tmp_path / "mysync")
    assert p.name == "dir-sync" and p.display_name == "Folder Sync"


def test_create_provider_expands_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PC_SYNC_BASE", str(tmp_path))
    p = create_provider({"root": "$PC_SYNC_BASE/folder"})
    assert p._root == str(tmp_path / "folder")


def test_create_provider_empty_config_constructs():
    p = create_provider(None)
    assert p._root == ""
    assert p.test().ok is False


# ── what a failure says ──────────────────────────────────────────────────────────────
#
# The OS's own words ("[Errno 13] Permission denied: '…'") used to be the whole message. Now
# the failure says what is wrong with the sync folder and what to do; those words follow as
# the detail.

ON_CARD = "on the Folder Sync card in Settings → Providers"
RETRIES = "Sync tries again on its next run."
NOT_THERE = (
    "The sync folder at {root} isn't there, and Folder Sync couldn't create it. If it lives on a "
    f"drive or a sync mount, reconnect that; otherwise set Sync folder {ON_CARD} to a folder "
    "that is there."
)


def _failing_write(error: OSError):
    def _write(self, target, data):
        raise error

    return _write


@pytest.mark.parametrize(
    ("error", "says"),
    [
        (
            PermissionError(13, "Permission denied", "/sync/folder/k"),
            "Folder Sync isn't allowed to write to the sync folder at {root}. Fix that folder's "
            f"permissions, or set Sync folder {ON_CARD} to one PersonalClaw can write to.",
        ),
        (
            OSError(errno.ENOSPC, "No space left on device"),
            "The disk holding the sync folder at {root} is full. Free some space.",
        ),
        (
            OSError(errno.EROFS, "Read-only file system"),
            "The sync folder at {root} is on a read-only disk or mount. Make it writable, or set "
            f"Sync folder {ON_CARD} to a writable folder.",
        ),
        (
            OSError(errno.EIO, "Input/output error"),
            "Folder Sync couldn't write to the sync folder at {root}. Check that folder, or set "
            f"Sync folder {ON_CARD} to another folder.",
        ),
    ],
)
def test_a_push_the_folder_refuses_says_why_and_that_it_retries(tmp_path, monkeypatch, error, says):
    """The OS's own line used to be the whole message."""
    monkeypatch.setattr(DirSyncProvider, "_atomic_write", _failing_write(error))

    r = DirSyncProvider(str(tmp_path)).push([SyncObject("k", b"v")])

    assert r.outcome == "transient"
    assert r.detail == f"{says.format(root=tmp_path)} {RETRIES} Details: {error}"


def test_a_push_to_a_folder_that_is_gone_says_to_reconnect_it(tmp_path, monkeypatch):
    """An unplugged drive: recreating its mount point is refused, so the error reads
    "Permission denied" — and that line used to be the whole message."""
    root = tmp_path / "unplugged" / "sync"
    error = PermissionError(13, "Permission denied", "/unplugged")
    monkeypatch.setattr(DirSyncProvider, "_atomic_write", _failing_write(error))

    r = DirSyncProvider(str(root)).push([SyncObject("k", b"v")])

    assert r.outcome == "transient"
    assert r.detail == f"{NOT_THERE.format(root=root)} {RETRIES} Details: {error}"


def test_a_push_to_a_path_that_is_a_file_says_so(tmp_path):
    """"[Errno 17] File exists: '…'" used to be the whole message."""
    root = tmp_path / "sync"
    root.write_text("not a folder", encoding="utf-8")

    r = DirSyncProvider(str(root)).push([SyncObject("k", b"v")])

    assert r.outcome == "transient"
    assert r.detail.startswith(
        f"The sync folder at {root} is a file, not a folder. Set Sync folder {ON_CARD} to a "
        f"folder. {RETRIES} Details: "
    ), r.detail


def test_a_probe_that_cannot_create_the_folder_says_why(tmp_path, monkeypatch):
    """"sync folder unreachable: [Errno 13] Permission denied: '…'" used to be the message."""
    root = tmp_path / "new" / "sync"
    error = PermissionError(13, "Permission denied", "/new")

    def _denied(path, *args, **kwargs):
        raise error

    monkeypatch.setattr(dir_sync.os, "makedirs", _denied)

    res = DirSyncProvider(str(root)).test()

    assert res.ok is False
    assert res.detail == f"{NOT_THERE.format(root=root)} Details: {error}"
