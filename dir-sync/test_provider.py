"""Folder-sync transport tests — pure filesystem, no network.

Every case drives a ``tmp_path`` root so nothing touches a real sync folder. Covers the
insert-only push contract, list_remote prefix filtering + temp-file exclusion, pull's
drop-on-vanish, the rename-locked registry CAS (present/absent/mismatch + round-trip),
the reachability probe, and that no key reads or writes outside the sync folder.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os

import pytest

import provider as dir_sync
from provider import DirSyncProvider, create_provider
from personalclaw.sdk.sync import KeysRefused, RemoteRef, SyncObject


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


# ── a key that leaves the sync folder ────────────────────────────────────────────────────
#
# Whoever else writes the sync folder can put a link in it to any file on this machine. A key was
# joined onto the folder and opened as it came, following whatever was there: a link planted in
# another machine's folder read a file of this machine's as that machine's object, a folder on the
# way that is a link took this machine's objects anywhere its user may write, and a key with
# ``../`` in it read or wrote anywhere at all.

#: A file of this machine's, outside the sync folder.
OUTSIDE = b"a file of this machine's, outside the sync folder\n"
NOT_A_PATH = "is not a path a sync may use in the sync folder"
LEADS_OUT = "leads out of the sync folder through a link"
A_LINK = "is a link in the sync folder, which Folder Sync doesn't follow"
PEERS = "machines/b/seq-0001/"


@pytest.fixture
def folder(tmp_path):
    """The sync folder, and beside it, outside it, a file of this machine's and a folder."""
    root = tmp_path / "sync"
    root.mkdir()
    (tmp_path / "outside.txt").write_bytes(OUTSIDE)
    (tmp_path / "elsewhere").mkdir()
    return root


def _plant(root, key: str, target) -> None:
    """A link at *key* in the sync folder, to *target*, as whoever else writes the folder can
    put one there."""
    path = root.joinpath(*key.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target)


def _refused(call) -> dict[str, str]:
    """What *call* refused (``KeysRefused.refused``); fails when it refused nothing."""
    with pytest.raises(KeysRefused) as refusal:
        call()
    assert OUTSIDE.decode().strip() not in str(refusal.value)
    return refusal.value.refused


def test_a_link_planted_in_another_machines_folder_is_not_read(folder, tmp_path):
    key = f"{PEERS}tasks/entities.jsonl"
    _plant(folder, key, tmp_path / "outside.txt")
    p = DirSyncProvider(str(folder))

    assert _refused(lambda: p.pull([RemoteRef(key)])) == {key: LEADS_OUT}
    assert _refused(lambda: p.list_remote(PEERS)) == {key: A_LINK}


def test_a_link_to_a_file_inside_the_folder_is_not_followed_either(folder):
    key = f"{PEERS}tasks/entities.jsonl"
    (folder / "other.jsonl").write_bytes(b"{}\n")
    _plant(folder, key, folder / "other.jsonl")
    p = DirSyncProvider(str(folder))

    assert _refused(lambda: p.pull([RemoteRef(key)])) == {key: A_LINK}


def test_a_folder_on_the_way_that_is_a_link_is_refused_not_walked_into(folder, tmp_path):
    (tmp_path / "elsewhere" / "seq-0001").mkdir()
    (tmp_path / "elsewhere" / "seq-0001" / "x.jsonl").write_bytes(OUTSIDE)
    _plant(folder, "machines/b", tmp_path / "elsewhere")
    p = DirSyncProvider(str(folder))

    assert _refused(lambda: p.list_remote(PEERS)) == {"machines/b": A_LINK}
    assert _refused(lambda: p.pull([RemoteRef(f"{PEERS}x.jsonl")])) == {
        f"{PEERS}x.jsonl": LEADS_OUT
    }


def test_a_listing_elsewhere_in_the_folder_is_not_held_up_by_a_link(folder, tmp_path):
    """A control: a link under another prefix is none of this listing's business."""
    _plant(folder, f"{PEERS}x.jsonl", tmp_path / "outside.txt")
    (folder / "machines" / "c" / "seq-0001").mkdir(parents=True)
    (folder / "machines" / "c" / "seq-0001" / "x.jsonl").write_bytes(b"c")
    p = DirSyncProvider(str(folder))

    assert [r.key for r in p.list_remote("machines/c/")] == ["machines/c/seq-0001/x.jsonl"]


def test_a_push_through_a_folder_that_is_a_link_writes_nothing_outside(folder, tmp_path):
    key = "machines/a/seq-0001/x.jsonl"
    _plant(folder, "machines/a", tmp_path / "elsewhere")
    p = DirSyncProvider(str(folder))

    assert _refused(lambda: p.push([SyncObject("k", b"mine"), SyncObject(key, b"mine")])) == {
        key: LEADS_OUT
    }
    assert list((tmp_path / "elsewhere").iterdir()) == [], "an object was written through it"
    assert not (folder / "k").exists(), "part of a refused push was written"


def test_a_key_that_climbs_out_is_neither_read_nor_written(folder, tmp_path):
    p = DirSyncProvider(str(folder))

    assert _refused(lambda: p.pull([RemoteRef("../outside.txt")])) == {
        "../outside.txt": NOT_A_PATH
    }
    assert _refused(lambda: p.push([SyncObject("../planted.txt", b"x")])) == {
        "../planted.txt": NOT_A_PATH
    }
    assert _refused(lambda: p.push([SyncObject(str(tmp_path / "abs.txt"), b"x")])) == {
        str(tmp_path / "abs.txt"): NOT_A_PATH
    }
    assert not (tmp_path / "planted.txt").exists() and not (tmp_path / "abs.txt").exists()


def test_a_registry_that_is_a_link_is_neither_read_nor_swapped(folder, tmp_path):
    _plant(folder, "registry.json", tmp_path / "outside.txt")
    p = DirSyncProvider(str(folder))

    assert _refused(lambda: p.cas_registry(_sha(OUTSIDE), b"{}")) == {"registry.json": LEADS_OUT}
    assert (tmp_path / "outside.txt").read_bytes() == OUTSIDE
    assert (folder / "registry.json").is_symlink(), "the swap went ahead"
    assert not (folder / ".registry.lock").exists()


def test_a_link_made_after_the_key_was_looked_at_is_not_followed(folder, tmp_path, monkeypatch):
    """The key is looked at, then opened: a link put there in between is still not followed.
    Played by a look that finds nothing wrong with it."""
    key = f"{PEERS}x.jsonl"
    _plant(folder, key, tmp_path / "outside.txt")
    monkeypatch.setattr(
        DirSyncProvider, "_path", lambda self, k: (os.path.join(self._root, *k.split("/")), "")
    )
    p = DirSyncProvider(str(folder))

    assert _refused(lambda: p.pull([RemoteRef(key)])) == {key: A_LINK}


def test_the_refusal_says_which_key_why_and_what_to_do(folder, tmp_path):
    key = f"{PEERS}tasks/entities.jsonl"
    _plant(folder, key, tmp_path / "outside.txt")

    with pytest.raises(KeysRefused) as refusal:
        DirSyncProvider(str(folder)).pull([RemoteRef(key)])

    assert str(refusal.value) == (
        f"Folder Sync won't read a key in {folder}: {key} {LEADS_OUT}. Take the link out of the "
        "sync folder: Folder Sync follows none out of it."
    )


# ── remove ───────────────────────────────────────────────────────────────────────────
#
# Each sync sends this machine's records as one whole copy, and nothing removed one: the folder
# took a full copy every fifteen minutes, for good. The cycle removes the copies a newer one
# replaced through ``remove``, which must hold to the same rule as every other call: nothing
# outside the sync folder, and no link followed.

SEQ1 = "machines/a/seq-0001/"


def _copy(root, prefix: str = SEQ1) -> list[str]:
    keys = [f"{prefix}manifest.json", f"{prefix}tasks/entities.jsonl", f"{prefix}db/memory_db.db"]
    DirSyncProvider(str(root)).push([SyncObject(k, b"x") for k in keys])
    return keys


def test_a_folder_sync_removes_old_copies():
    assert DirSyncProvider.removes_old_copies is True


def test_remove_removes_the_objects_and_the_folders_it_empties(tmp_path):
    keys = _copy(tmp_path)
    _copy(tmp_path, "machines/a/seq-0002/")
    p = DirSyncProvider(str(tmp_path))

    assert p.remove(keys) == 3

    assert not (tmp_path / "machines" / "a" / "seq-0001").exists()
    assert (tmp_path / "machines" / "a" / "seq-0002" / "manifest.json").is_file()
    assert [r.key for r in p.list_remote(SEQ1)] == []


def test_remove_is_idempotent_and_counts_what_was_there(tmp_path):
    keys = _copy(tmp_path)
    p = DirSyncProvider(str(tmp_path))
    assert p.remove(keys[:1]) == 1
    assert p.remove(keys) == 2  # the first was gone already: no error
    assert p.remove(keys) == 0
    assert p.remove([]) == 0 and DirSyncProvider("").remove(keys) == 0


def test_remove_never_removes_the_sync_folder(tmp_path):
    root = tmp_path / "sync"
    p = DirSyncProvider(str(root))
    p.push([SyncObject("only.json", b"x")])
    assert p.remove(["only.json"]) == 1
    assert root.is_dir() and list(root.iterdir()) == []


def test_remove_leaves_a_folder_something_else_still_holds(tmp_path):
    keys = _copy(tmp_path)
    (tmp_path / "machines" / "a" / "seq-0001" / ".DS_Store").write_bytes(b"finder")
    assert DirSyncProvider(str(tmp_path)).remove(keys) == 3
    assert (tmp_path / "machines" / "a" / "seq-0001" / ".DS_Store").is_file()


def test_a_key_that_climbs_out_or_leads_out_is_refused_and_nothing_is_removed(folder, tmp_path):
    keys = _copy(folder)
    _plant(folder, "machines/b", tmp_path / "elsewhere")
    (tmp_path / "elsewhere" / "seq-0001").mkdir()
    (tmp_path / "elsewhere" / "seq-0001" / "x.jsonl").write_bytes(OUTSIDE)
    p = DirSyncProvider(str(folder))

    assert _refused(lambda: p.remove([keys[0], "../outside.txt"])) == {
        "../outside.txt": NOT_A_PATH
    }
    assert _refused(lambda: p.remove([keys[0], f"{PEERS}x.jsonl"])) == {
        f"{PEERS}x.jsonl": LEADS_OUT
    }
    assert (tmp_path / "outside.txt").read_bytes() == OUTSIDE
    assert (tmp_path / "elsewhere" / "seq-0001" / "x.jsonl").read_bytes() == OUTSIDE
    assert (folder / keys[0]).is_file(), "part of a refused removal went ahead"


def test_a_link_at_the_key_is_refused_and_neither_it_nor_its_file_goes(folder, tmp_path):
    key = f"{PEERS}tasks/entities.jsonl"
    _plant(folder, key, tmp_path / "outside.txt")
    assert _refused(lambda: DirSyncProvider(str(folder)).remove([key])) == {key: LEADS_OUT}
    assert (folder / key).is_symlink() and (tmp_path / "outside.txt").read_bytes() == OUTSIDE


def test_a_folder_made_a_link_after_the_key_was_looked_at_removes_nothing_outside(
    folder, tmp_path, monkeypatch
):
    """The key is looked at, then each folder on the way opened without following a link: a
    link put there in between leads nowhere. Played by a look that finds nothing wrong."""
    (tmp_path / "elsewhere" / "seq-0001").mkdir()
    (tmp_path / "elsewhere" / "seq-0001" / "x.jsonl").write_bytes(OUTSIDE)
    _plant(folder, "machines/b", tmp_path / "elsewhere")
    monkeypatch.setattr(
        DirSyncProvider, "_path", lambda self, k: (os.path.join(self._root, *k.split("/")), "")
    )
    key = f"{PEERS}x.jsonl"

    assert _refused(lambda: DirSyncProvider(str(folder)).remove([key])) == {key: LEADS_OUT}
    assert (tmp_path / "elsewhere" / "seq-0001" / "x.jsonl").read_bytes() == OUTSIDE


def test_the_folder_keeps_this_machines_newest_copy_through_a_sync(tmp_path, monkeypatch):
    """Driven through the sync cycle on a real folder: a copy per change, and the copy a newer
    one replaced removed once the newer has stood fifteen minutes."""
    from datetime import datetime, timedelta, timezone

    from personalclaw.durability import crypto as crypto_mod
    from personalclaw.durability.sync_cycle import run_sync_cycle

    monkeypatch.setattr(crypto_mod, "load_passphrase", lambda: "a passphrase for this test")
    home, root = tmp_path / "home", tmp_path / "sync"
    (home / "tasks").mkdir(parents=True)
    p = DirSyncProvider(str(root))
    start = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)

    def cycle(minutes: int, title: str | None):
        if title is not None:
            (home / "tasks" / "t1.json").write_text(json.dumps({"id": "t1", "title": title}))
        report = run_sync_cycle(
            p, home, self_id="a", now=(start + timedelta(minutes=minutes)).isoformat()
        )
        assert report.ok, report.error
        return sorted(d.name for d in (root / "machines" / "a").iterdir())

    assert cycle(0, "one") == ["seq-0001"]
    assert cycle(15, None) == ["seq-0001"]  # nothing changed: nothing sent
    assert cycle(30, "two") == ["seq-0001", "seq-0002"]
    assert cycle(46, None) == ["seq-0002"]
    assert cycle(61, "three") == ["seq-0002", "seq-0003"]
    assert cycle(90, "four") == ["seq-0003", "seq-0004"]
