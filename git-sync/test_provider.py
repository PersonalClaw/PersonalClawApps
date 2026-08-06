"""Git-sync transport tests — real ``git`` against a local bare remote, no network.

Every case points the provider at a ``git init --bare`` repo under ``tmp_path`` (git
accepts a local path as a remote URL) and a fresh working clone, so the tests exercise
the real subprocess path with no credentials and no network. Covers the insert-only push
contract (verified by cloning the remote fresh), the empty-remote first-machine case,
list_remote's .git exclusion + prefix filtering + temp-file exclusion, pull's
drop-on-vanish, the registry compare-and-swap (present/absent/mismatch + round-trip), the
push-rejection → transient classification, two-machine convergence at the transport level,
and the reachability probe.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess

import pytest

from provider import GitSyncProvider, create_provider
from personalclaw.sdk.sync import RemoteRef, SyncObject

# git is available in this environment; skip cleanly only if it somehow is not.
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    """Raw git for test setup/verification, with a deterministic identity for commits."""
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@personalclaw.local",
            "-C",
            cwd,
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def remote(tmp_path):
    """A bare git repo acting as the remote the user owns."""
    path = str(tmp_path / "remote.git")
    subprocess.run(["git", "init", "--bare", "-b", "main", path], check=True,
                   capture_output=True, text=True)
    return path


def _provider(remote_url: str, tmp_path, name: str = "clone") -> GitSyncProvider:
    return GitSyncProvider(repo_url=remote_url, local_clone=str(tmp_path / name), branch="main")


def _files_in_fresh_remote_checkout(remote_url: str, tmp_path, name: str = "verify") -> set[str]:
    """Clone the remote fresh and return the posix keys of its tracked files."""
    dest = str(tmp_path / name)
    subprocess.run(["git", "clone", remote_url, dest], check=True, capture_output=True, text=True)
    out: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(dest):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            out.add(os.path.relpath(full, dest).replace(os.sep, "/"))
    return out


# ── push ─────────────────────────────────────────────────────────────────────────────


def test_push_writes_commits_and_pushes_to_remote(remote, tmp_path):
    p = _provider(remote, tmp_path)
    r = p.push([
        SyncObject("machines/abc/seq-0007/tasks/entities.jsonl", b"one"),
        SyncObject("registry.json", b"{}"),
    ])
    assert r.outcome == "delivered"
    assert r.pushed == 2 and r.skipped == 0
    # Verify by cloning the remote FRESH — the objects really landed on the remote.
    keys = _files_in_fresh_remote_checkout(remote, tmp_path)
    assert "machines/abc/seq-0007/tasks/entities.jsonl" in keys
    assert "registry.json" in keys


def test_push_to_empty_remote_first_machine(remote, tmp_path):
    # A brand-new empty remote is the first machine, not an error: the first push creates
    # the branch on the remote.
    p = _provider(remote, tmp_path)
    r = p.push([SyncObject("first.jsonl", b"hello")])
    assert r.outcome == "delivered" and r.pushed == 1
    assert "first.jsonl" in _files_in_fresh_remote_checkout(remote, tmp_path)


def test_push_is_insert_only_and_idempotent(remote, tmp_path):
    p = _provider(remote, tmp_path)
    p.push([SyncObject("a/x.jsonl", b"original")])
    # Re-push the same key with different bytes — must be skipped, not overwritten.
    r = p.push([SyncObject("a/x.jsonl", b"CHANGED")])
    assert r.pushed == 0 and r.skipped == 1
    assert r.outcome == "delivered"
    assert (tmp_path / "clone" / "a" / "x.jsonl").read_bytes() == b"original"


def test_push_mixed_new_and_existing(remote, tmp_path):
    p = _provider(remote, tmp_path)
    p.push([SyncObject("k1", b"1")])
    r = p.push([SyncObject("k1", b"dup"), SyncObject("k2", b"2")])
    assert r.pushed == 1 and r.skipped == 1
    assert r.outcome == "delivered"


def test_push_nothing_to_commit_is_delivered(remote, tmp_path):
    p = _provider(remote, tmp_path)
    p.push([SyncObject("only", b"x")])
    # Re-pushing only an existing key stages nothing — delivered with pushed=0.
    r = p.push([SyncObject("only", b"x")])
    assert r.outcome == "delivered" and r.pushed == 0 and r.skipped == 1


def test_push_empty_config_is_transient(tmp_path):
    r = GitSyncProvider(repo_url="", local_clone=str(tmp_path / "c")).push([SyncObject("k", b"v")])
    assert r.outcome == "transient"
    assert "no git remote" in r.detail


def test_push_rejection_is_transient(remote, tmp_path):
    # A push rejected because the remote moved under us is retryable, not permanent.
    a = _provider(remote, tmp_path, name="a")
    a.push([SyncObject("base", b"0")])  # remote now has a commit on main

    b = _provider(remote, tmp_path, name="b")
    b.list_remote()  # establishes b's clone at the current commit
    # Give b a diverging, unpushed local commit so a later --ff-only pull cannot save it.
    _git(str(tmp_path / "b"), "commit", "--allow-empty", "-m", "b-local")
    # Advance the remote out from under b via a's clone.
    a.push([SyncObject("moved", b"1")])

    r = b.push([SyncObject("bnew", b"2")])
    assert r.outcome == "transient"


# ── list_remote ──────────────────────────────────────────────────────────────────────


def test_list_remote_idle_is_empty(tmp_path):
    assert GitSyncProvider(repo_url="", local_clone=str(tmp_path / "c")).list_remote() == []


def test_list_remote_returns_all_with_posix_keys_and_sizes(remote, tmp_path):
    p = _provider(remote, tmp_path)
    p.push([
        SyncObject("machines/m1/seq-0001/a.jsonl", b"aaaa"),
        SyncObject("registry.json", b"{}"),
    ])
    refs = {r.key: r for r in p.list_remote()}
    assert set(refs) == {"machines/m1/seq-0001/a.jsonl", "registry.json"}
    assert refs["machines/m1/seq-0001/a.jsonl"].size == 4
    assert refs["registry.json"].fingerprint != ""


def test_list_remote_excludes_git_dir(remote, tmp_path):
    p = _provider(remote, tmp_path)
    p.push([SyncObject("real.jsonl", b"x")])
    # The working clone has a full .git tree; none of it may surface as a remote ref.
    assert all(not r.key.startswith(".git") for r in p.list_remote())
    assert {r.key for r in p.list_remote()} == {"real.jsonl"}


def test_list_remote_prefix_filters(remote, tmp_path):
    p = _provider(remote, tmp_path)
    p.push([
        SyncObject("machines/m1/a", b"1"),
        SyncObject("machines/m2/b", b"2"),
        SyncObject("registry.json", b"{}"),
    ])
    keys = {r.key for r in p.list_remote(prefix="machines/m1/")}
    assert keys == {"machines/m1/a"}


def test_list_remote_excludes_temp_files(remote, tmp_path):
    p = _provider(remote, tmp_path)
    p.push([SyncObject("real.jsonl", b"x")])
    # An untracked half-written temp file in the clone must never be advertised.
    (tmp_path / "clone" / ".tmp-halfwritten").write_bytes(b"partial")
    keys = {r.key for r in p.list_remote()}
    assert keys == {"real.jsonl"}


# ── pull ─────────────────────────────────────────────────────────────────────────────


def test_pull_reads_bytes(remote, tmp_path):
    p = _provider(remote, tmp_path)
    p.push([SyncObject("a/x", b"hello"), SyncObject("b/y", b"world")])
    objs = {o.key: o.data for o in p.pull([RemoteRef("a/x"), RemoteRef("b/y")])}
    assert objs == {"a/x": b"hello", "b/y": b"world"}


def test_pull_drops_vanished_ref(remote, tmp_path):
    p = _provider(remote, tmp_path)
    p.push([SyncObject("present", b"here")])
    objs = p.pull([RemoteRef("present"), RemoteRef("gone/missing.jsonl")])
    assert [o.key for o in objs] == ["present"]


def test_pull_idle_returns_empty(tmp_path):
    idle = GitSyncProvider(repo_url="", local_clone=str(tmp_path / "c"))
    assert idle.pull([RemoteRef("k")]) == []


# ── cas_registry ─────────────────────────────────────────────────────────────────────


def test_cas_registry_absent_succeeds_with_none(remote, tmp_path):
    p = _provider(remote, tmp_path)
    # expected_sha=None means "expected absent" — succeeds when the file does not exist.
    assert p.cas_registry(None, b'{"v":1}') is True
    assert "registry.json" in _files_in_fresh_remote_checkout(remote, tmp_path)


def test_cas_registry_absent_fails_with_non_none(remote, tmp_path):
    p = _provider(remote, tmp_path)
    # Registry absent but caller expected a concrete sha — lost race, no write.
    assert p.cas_registry(_sha(b"whatever"), b"new") is False
    assert "registry.json" not in _files_in_fresh_remote_checkout(remote, tmp_path)


def test_cas_registry_present_matches(remote, tmp_path):
    p = _provider(remote, tmp_path)
    p.cas_registry(None, b"first")
    assert p.cas_registry(_sha(b"first"), b"second") is True
    assert (tmp_path / "clone" / "registry.json").read_bytes() == b"second"


def test_cas_registry_present_mismatch_fails(remote, tmp_path):
    p = _provider(remote, tmp_path)
    p.cas_registry(None, b"first")
    # Stale expectation — the file no longer hashes to what the caller pulled.
    assert p.cas_registry(_sha(b"stale"), b"second") is False
    assert (tmp_path / "clone" / "registry.json").read_bytes() == b"first"


def test_cas_registry_none_fails_when_present(remote, tmp_path):
    p = _provider(remote, tmp_path)
    p.cas_registry(None, b"first")
    # "expected absent" but the file is present now — must fail.
    assert p.cas_registry(None, b"second") is False
    assert (tmp_path / "clone" / "registry.json").read_bytes() == b"first"


def test_cas_registry_round_trip_read_back(remote, tmp_path):
    p = _provider(remote, tmp_path)
    payload = b'{"machines":{"m1":7}}'
    assert p.cas_registry(None, payload) is True
    refs = [r for r in p.list_remote() if r.key == "registry.json"]
    assert len(refs) == 1
    got = p.pull(refs)
    assert got[0].data == payload


# ── two-machine convergence at the transport level ─────────────────────────────────────


def test_two_machines_converge_over_the_remote(remote, tmp_path):
    # Machine A and machine B share one remote via separate working clones.
    a = _provider(remote, tmp_path, name="machine-a")
    b = _provider(remote, tmp_path, name="machine-b")

    a.push([SyncObject("machines/a/seq-0001/tasks.jsonl", b"A-task")])
    b.push([SyncObject("machines/b/seq-0001/tasks.jsonl", b"B-task")])

    # After a list_remote each (which pulls), each machine sees the other's object.
    a_keys = {r.key for r in a.list_remote()}
    b_keys = {r.key for r in b.list_remote()}
    assert "machines/b/seq-0001/tasks.jsonl" in a_keys
    assert "machines/a/seq-0001/tasks.jsonl" in b_keys

    # And the bytes round-trip through pull on the machine that did not write them.
    pulled = a.pull([RemoteRef("machines/b/seq-0001/tasks.jsonl")])
    assert pulled and pulled[0].data == b"B-task"


# ── test() reachability probe ────────────────────────────────────────────────────────


def test_probe_ok_on_reachable_remote(remote, tmp_path):
    res = _provider(remote, tmp_path).test()
    assert res.ok is True
    assert remote in res.detail


def test_probe_not_ok_on_empty_config():
    res = GitSyncProvider(repo_url="").test()
    assert res.ok is False
    assert "no git remote" in res.detail


def test_probe_not_ok_on_bad_url(tmp_path):
    res = GitSyncProvider(
        repo_url=str(tmp_path / "does-not-exist.git"), local_clone=str(tmp_path / "c")
    ).test()
    assert res.ok is False
    assert res.detail  # a human snippet from git's stderr


# ── factory + config ─────────────────────────────────────────────────────────────────


def test_create_provider_expands_user(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    p = create_provider({"repo_url": "u@h:r.git", "local_clone": "~/myclone", "branch": "dev"})
    assert p._clone == str(tmp_path / "myclone")
    assert p._repo_url == "u@h:r.git"
    assert p._branch == "dev"
    assert p.name == "git-sync" and p.display_name == "Git Sync"


def test_create_provider_expands_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PC_SYNC_BASE", str(tmp_path))
    p = create_provider({"repo_url": "r", "local_clone": "$PC_SYNC_BASE/clone"})
    assert p._clone == str(tmp_path / "clone")


def test_create_provider_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    p = create_provider(None)
    assert p._repo_url == ""
    assert p._branch == "main"
    assert p._clone == str(tmp_path / ".personalclaw" / "sync" / "git-sync")
    assert p.test().ok is False
