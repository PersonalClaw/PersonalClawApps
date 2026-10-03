"""Git-sync transport tests — real ``git`` against a bare remote on this machine, no network.

Every case points the provider at a ``git init --bare`` repo under ``tmp_path`` and a fresh
working clone, so the tests exercise the real subprocess path with no credentials and no
network. The transport's git refuses a remote at a local path, so the remote is reached the way
an owner reaches one: over ssh, with an ssh command of the owner's own (``core.sshCommand`` in a
scratch ``HOME``), here a stand-in that runs the server's side of git on this machine.

Covers the insert-only push contract (verified by cloning the remote fresh), the empty-remote
first-machine case, list_remote's .git exclusion + prefix filtering + temp-file exclusion,
pull's drop-on-vanish, the registry compare-and-swap (present/absent/mismatch + round-trip),
lost push races (a second clone pushing between the first's catch-up and its push) and the real
conflicts no rule settles, two-machine convergence at the transport level, the reachability
probe, and that no key reads or writes outside the working clone.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import provider as git_sync
from provider import GitSyncProvider, create_provider
from personalclaw.sdk.net import sentence_with_detail
from personalclaw.sdk.sync import KeysRefused, RemoteRef, SyncObject

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # the repo root: apps_testkit
from apps_testkit.git_too_old import REFUSAL_START, put_old_git_on_path  # noqa: E402

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
def ssh_url(tmp_path, monkeypatch):
    """``ssh_url(path)``: an ``ssh://`` URL that reaches the repository at *path* on this machine.

    The owner's ssh command, in a scratch global git file (``GIT_CONFIG_GLOBAL``) both the
    tests' git and the transport's read, is a stand-in: it records the SSH agent socket and the
    planted secret it was handed, then runs the git command a server would. It runs that command
    with an environment of its own, as a login on the server gets one, so none of the client's
    git settings reach the server's git: a hook the remote runs is the remote's."""
    home = tmp_path / "home"
    home.mkdir()
    seen = tmp_path / "ssh-saw"
    stand_in = home / "ssh-stand-in"
    stand_in.write_text(
        "#!/bin/sh\n"
        '[ "$1" = "-G" ] && exit 1\n'
        f'echo "agent=${{SSH_AUTH_SOCK:-none}} secret=${{EXAMPLE_SERVICE_API_TOKEN:-none}}" >> "{seen}"\n'
        'exec env -i PATH="$PATH" HOME="$HOME" /bin/sh -c "$2"\n'
    )
    stand_in.chmod(0o755)
    (home / ".gitconfig").write_text(f"[core]\n\tsshCommand = {stand_in}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    return lambda path: f"ssh://example.invalid{os.path.realpath(path)}"


@pytest.fixture
def remote(tmp_path, ssh_url):
    """A bare git repo acting as the remote the user owns, as the ssh URL that reaches it."""
    path = str(tmp_path / "remote.git")
    subprocess.run(["git", "init", "--bare", "-b", "main", path], check=True,
                   capture_output=True, text=True)
    return ssh_url(path)


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


# ── lost races ───────────────────────────────────────────────────────────────────────
#
# A race is lost when another machine pushes between this one's catch-up and its push. It used
# to end the push ``transient`` and leave the clone holding a commit the remote lacked; ``git
# pull --ff-only`` could never move past that commit, so every later push was turned away too.


class _Race:
    """Another machine's push, run just before each ``git push`` ``loser`` makes — after its
    catch-up, before its push: the window a real race lands in. At most ``times`` times when
    given, and never after :meth:`over`. ``runs`` counts the races run."""

    def __init__(self, monkeypatch, loser: GitSyncProvider, push, times: int | None = None):
        self.runs = 0
        self._over = False
        real_run = GitSyncProvider._run

        def _run(provider, args, check=True):
            if (
                provider is loser
                and git_sync._subcommand(args) == "push"
                and not self._over
                and (times is None or self.runs < times)
            ):
                self.runs += 1
                push()
            return real_run(provider, args, check=check)

        monkeypatch.setattr(GitSyncProvider, "_run", _run)

    def over(self) -> None:
        self._over = True


def _remote_bytes(remote_url: str, tmp_path, key: str, name: str) -> bytes:
    """``key``'s bytes on the remote, read from a fresh clone of it."""
    dest = tmp_path / name
    subprocess.run(["git", "clone", remote_url, str(dest)], check=True, capture_output=True,
                   text=True)
    return (dest / key).read_bytes()


def test_a_push_that_loses_a_race_catches_up_and_lands(remote, tmp_path, monkeypatch, c_locale):
    """Another machine pushes between this one's catch-up and its push. The push ended there,
    ``transient``, with its commit left behind in the clone."""
    a = _provider(remote, tmp_path, name="a")
    b = _provider(remote, tmp_path, name="b")
    a.push([SyncObject("base", b"0")])

    def b_pushes_first():
        assert b.push([SyncObject("machines/b/seq-0001/x.jsonl", b"B")]).outcome == "delivered"

    race = _Race(monkeypatch, a, b_pushes_first, times=1)

    r = a.push([SyncObject("machines/a/seq-0001/x.jsonl", b"A")])

    assert race.runs == 1, "the race this test is about never ran"
    assert (r.outcome, r.pushed, r.skipped) == ("delivered", 1, 0), r.detail
    keys = _files_in_fresh_remote_checkout(remote, tmp_path)
    assert {"machines/a/seq-0001/x.jsonl", "machines/b/seq-0001/x.jsonl"} <= keys


def test_after_a_push_runs_out_of_tries_the_next_one_delivers(
    remote, tmp_path, monkeypatch, c_locale
):
    """The wedge itself: once a push had lost, the next was turned away as well, and so on."""
    a = _provider(remote, tmp_path, name="a")
    b = _provider(remote, tmp_path, name="b")
    a.push([SyncObject("base", b"0")])
    seqs = iter(range(1, 10))

    def b_pushes_first():
        key = f"machines/b/seq-{next(seqs):04d}/x.jsonl"
        assert b.push([SyncObject(key, b"B")]).outcome == "delivered"

    # Another machine wins every try of this push, then stops.
    race = _Race(monkeypatch, a, b_pushes_first)
    first = a.push([SyncObject("machines/a/seq-0001/x.jsonl", b"A")])
    race.over()
    assert first.outcome == "transient"

    second = a.push([SyncObject("machines/a/seq-0002/x.jsonl", b"A")])

    assert second.outcome == "delivered", second.detail
    assert race.runs == 3, "the first push didn't catch up and try again after each lost race"
    keys = _files_in_fresh_remote_checkout(remote, tmp_path)
    assert {"machines/a/seq-0001/x.jsonl", "machines/a/seq-0002/x.jsonl"} <= keys
    assert {f"machines/b/seq-{n:04d}/x.jsonl" for n in (1, 2, 3)} <= keys


def test_a_clone_holding_a_commit_the_remote_lacks_catches_up_before_it_pushes(
    remote, tmp_path, c_locale
):
    """The state a lost race used to leave behind, made directly: a commit the remote never got,
    and a remote that has since moved on without it."""
    a = _provider(remote, tmp_path, name="a")
    a.push([SyncObject("base", b"0")])
    b = _provider(remote, tmp_path, name="b")
    b.list_remote()
    (tmp_path / "b" / "stranded.jsonl").write_bytes(b"S")
    _git(str(tmp_path / "b"), "add", "-A")
    _git(str(tmp_path / "b"), "commit", "-m", "sync: 1 objects")
    a.push([SyncObject("moved", b"1")])

    # A read catches up too, rather than serving the clone as the lost race left it.
    assert "moved" in {ref.key for ref in b.list_remote()}
    r = b.push([SyncObject("bnew", b"2")])

    assert r.outcome == "delivered", r.detail
    keys = _files_in_fresh_remote_checkout(remote, tmp_path)
    assert {"base", "moved", "stranded.jsonl", "bnew"} <= keys
    assert b.push([SyncObject("bnewer", b"3")]).outcome == "delivered"


def test_a_key_the_remote_gained_while_catching_up_keeps_the_remotes_copy(
    remote, tmp_path, monkeypatch, c_locale
):
    """Insert-only across a race: the remote had the key first, so its copy stays and this
    machine's is skipped, while the rest of the push lands. Settling that needs no editor —
    git's is set to one that fails."""
    monkeypatch.setenv("GIT_EDITOR", "false")
    a = _provider(remote, tmp_path, name="a")
    b = _provider(remote, tmp_path, name="b")
    a.push([SyncObject("base", b"0")])

    def b_pushes_first():
        assert b.push([SyncObject("shared/k.jsonl", b"theirs")]).outcome == "delivered"

    race = _Race(monkeypatch, a, b_pushes_first, times=1)

    r = a.push([SyncObject("shared/k.jsonl", b"ours"), SyncObject("machines/a/x.jsonl", b"A")])

    assert race.runs == 1, "the race this test is about never ran"
    assert (r.outcome, r.pushed, r.skipped) == ("delivered", 1, 1), r.detail
    assert _remote_bytes(remote, tmp_path, "shared/k.jsonl", "verify") == b"theirs"
    assert "machines/a/x.jsonl" in _files_in_fresh_remote_checkout(remote, tmp_path, "verify2")
    assert (tmp_path / "a" / "shared" / "k.jsonl").read_bytes() == b"theirs"


def test_a_commit_in_the_clone_that_conflicts_with_the_remote_says_so_and_is_not_retried(
    remote, tmp_path, c_locale
):
    """A file changed by hand in the working clone and on the remote as well: no rule settles
    that. The push said the remote had commits this machine hadn't pulled, retried it on every
    run, and was turned away every time."""
    a = _provider(remote, tmp_path, name="a")
    a.push([SyncObject("notes.md", b"first\n")])
    b = _provider(remote, tmp_path, name="b")
    b.list_remote()
    clone_b = tmp_path / "b"
    (clone_b / "notes.md").write_bytes(b"edited in this clone\n")
    _git(str(clone_b), "commit", "-am", "an edit made by hand")
    (tmp_path / "a" / "notes.md").write_bytes(b"edited on the remote\n")
    _git(str(tmp_path / "a"), "commit", "-am", "an edit made elsewhere")
    _git(str(tmp_path / "a"), "push", "-q", "origin", "main")
    head = _git(str(clone_b), "rev-parse", "HEAD").stdout

    r = b.push([SyncObject("machines/b/x.jsonl", b"B")])

    assert r.outcome == "permanent"
    assert r.detail == (
        "Git Sync couldn't put this machine's unpushed commits on top of what the git remote has: "
        "notes.md was changed on both sides in ways git can't combine. Run git pull --rebase "
        f"origin main in the working clone at {clone_b} and resolve it there — or, if nothing "
        "there needs keeping, delete that folder and Git Sync clones the remote afresh on its "
        "next run. Details: CONFLICT (content): Merge conflict in notes.md"
    )
    # The replay is undone, and the commit made by hand is still there for whoever resolves it.
    assert not (clone_b / ".git" / "rebase-merge").exists()
    assert _git(str(clone_b), "rev-parse", "HEAD").stdout == head
    assert (clone_b / "notes.md").read_bytes() == b"edited in this clone\n"


def test_a_key_that_is_a_file_on_the_remote_and_a_folder_here_is_a_real_conflict(
    remote, tmp_path, monkeypatch, c_locale
):
    """The one conflict pushes alone can make: another machine's key is a file where this
    push's keys need a folder."""
    a = _provider(remote, tmp_path, name="a")
    b = _provider(remote, tmp_path, name="b")
    a.push([SyncObject("base", b"0")])

    def b_pushes_first():
        assert b.push([SyncObject("machines/a", b"a file")]).outcome == "delivered"

    _Race(monkeypatch, a, b_pushes_first, times=1)

    r = a.push([SyncObject("machines/a/seq-0001/x.jsonl", b"A")])

    assert r.outcome == "permanent"
    assert r.detail.startswith(
        "Git Sync couldn't put this machine's unpushed commits on top of what the git remote has: "
        "machines/a was changed on both sides in ways git can't combine. Run git pull --rebase "
        "origin main in the working clone at "
    ), r.detail
    assert "Details: CONFLICT (file/directory)" in r.detail
    assert not (tmp_path / "a" / ".git" / "rebase-merge").exists()


def test_a_registry_swap_that_loses_a_race_leaves_the_remotes_registry_to_re_read(
    remote, tmp_path, monkeypatch, c_locale
):
    """The swap is lost — and its write used to stay in the clone as a commit the remote lacked,
    so the caller's re-read found this machine's own bytes, and every later swap and push was
    turned away."""
    a = _provider(remote, tmp_path, name="a")
    b = _provider(remote, tmp_path, name="b")
    assert a.cas_registry(None, b'{"v":1}') is True

    def b_swaps_first():
        assert b.cas_registry(_sha(b'{"v":1}'), b'{"v":"b"}') is True

    race = _Race(monkeypatch, a, b_swaps_first, times=1)

    assert a.cas_registry(_sha(b'{"v":1}'), b'{"v":"a"}') is False
    assert race.runs == 1, "the race this test is about never ran"
    # What the caller re-reads after a lost swap is the remote's registry, not the lost write…
    refs = [ref for ref in a.list_remote() if ref.key == "registry.json"]
    assert [obj.data for obj in a.pull(refs)] == [b'{"v":"b"}']
    # …and the swap it retries with that lands, as does the next push.
    assert a.cas_registry(_sha(b'{"v":"b"}'), b'{"v":"a+b"}') is True
    assert _remote_bytes(remote, tmp_path, "registry.json", "verify") == b'{"v":"a+b"}'
    assert a.push([SyncObject("machines/a/x.jsonl", b"A")]).outcome == "delivered"


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


def test_probe_not_ok_on_bad_url(tmp_path, ssh_url):
    res = GitSyncProvider(
        repo_url=ssh_url(tmp_path / "does-not-exist.git"), local_clone=str(tmp_path / "c")
    ).test()
    assert res.ok is False
    assert res.detail  # what went wrong, then git's own words


@pytest.mark.parametrize("form", ["path", "file-url"])
def test_a_remote_at_a_local_path_is_refused_with_the_reason_and_the_alternative(tmp_path, form):
    """A local remote runs its upload and receive programs, and its own hooks, on this machine,
    as the gateway. The transport refuses it before git runs and says why and what to use."""
    path = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(path)], check=True,
                   capture_output=True, text=True)
    url = str(path) if form == "path" else f"file://{path}"
    p = GitSyncProvider(repo_url=url, local_clone=str(tmp_path / "c"))

    res = p.test()
    assert res.ok is False
    assert "local path" in res.detail and "ssh or https" in res.detail, res.detail
    _refused_everywhere(p, res.detail)
    assert not (tmp_path / "c").exists(), "git ran for a refused remote"


def _refused_everywhere(p: GitSyncProvider, says: str) -> None:
    """Settings git can't be run with say ``says`` at every entry point: the push, the probe,
    and a listing, a read and a registry swap, which raise it. A listing used to answer empty
    and a swap a lost race, so the cycle took the remote for one with nothing on it."""
    pushed = p.push([SyncObject("k", b"v")])
    assert (pushed.outcome, pushed.detail) == ("permanent", says)
    probe = p.test()
    assert (probe.ok, probe.detail) == (False, says)
    for step in (
        p.list_remote,
        lambda: p.pull([RemoteRef("k")]),
        lambda: p.cas_registry(None, b"{}"),
    ):
        with pytest.raises(git_sync.GitSyncFailed) as caught:
            step()
        assert str(caught.value) == says


def test_a_clone_whose_remote_was_changed_to_a_local_path_goes_back_to_git_remote_url(
    remote, tmp_path
):
    """A clone whose remote was changed to a local path after it was made (the clone's own
    `.git/config` is what git reads). Its fetches and pushes went where that said, and git's
    refusal of the path was the transport's last word; the clone is now taken back to Git remote
    URL, and the push lands there."""
    p = _provider(remote, tmp_path)
    p.push([SyncObject("first.jsonl", b"one")])
    local = tmp_path / "elsewhere.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(local)], check=True,
                   capture_output=True, text=True)
    _git(str(tmp_path / "clone"), "remote", "set-url", "origin", str(local))
    local_refs = _refs(local)

    r = p.push([SyncObject("second.jsonl", b"two")])

    assert r.outcome == "delivered", r
    assert _refs(local) == local_refs
    assert {"first.jsonl", "second.jsonl"} <= _files_in_fresh_remote_checkout(remote, tmp_path)


def test_a_refusal_git_reports_is_said_in_the_transports_words(remote, tmp_path):
    """A remote git itself won't reach: here the owner's own url rewrite sends Git remote URL to
    a local path, which the transport's check before git runs doesn't see. The detail says why,
    and what to use instead, in PersonalClaw's words first; git's own follow as the detail."""
    p = _provider(remote, tmp_path)
    p.push([SyncObject("first.jsonl", b"one")])
    local = tmp_path / "elsewhere.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(local)], check=True,
                   capture_output=True, text=True)
    with open(os.environ["GIT_CONFIG_GLOBAL"], "a", encoding="utf-8") as fh:
        fh.write(f'[url "{local}"]\n\tinsteadOf = {remote}\n')

    r = p.push([SyncObject("second.jsonl", b"two")])

    assert r.outcome == "permanent", r
    assert r.detail.startswith(
        "PersonalClaw's git does not reach a remote at a local path, because"
    ), r.detail
    assert "Reach it over ssh or https instead. Details: " in r.detail, r.detail


# ── what git runs in the working clone, and with what ────────────────────────────────


def test_the_remote_gets_the_ssh_agent_and_no_gateway_secret(remote, tmp_path, monkeypatch):
    """git over ssh signs in through the owner's SSH agent, so the transport's git carries its
    socket; it carries nothing else of the gateway's, which holds every secret saved in
    PersonalClaw."""
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/example-agent.sock")
    monkeypatch.setenv("EXAMPLE_SERVICE_API_TOKEN", "example-secret-token-4d1e9c")
    r = _provider(remote, tmp_path).push([SyncObject("first.jsonl", b"hello")])
    assert r.outcome == "delivered", r.detail
    saw = (tmp_path / "ssh-saw").read_text().splitlines()
    assert saw, "the push never reached ssh: the test is vacuous"
    assert set(saw) == {"agent=/tmp/example-agent.sock secret=none"}, saw


def test_a_hook_or_monitor_planted_in_the_working_clone_does_not_run(remote, tmp_path):
    """An agent's shell can write the working clone's ``.git`` as easily as its files. A hook
    or a file-system monitor planted there ran, as the gateway, on the next sync."""
    p = _provider(remote, tmp_path)
    p.push([SyncObject("first.jsonl", b"one")])
    clone = tmp_path / "clone"
    marker = tmp_path / "ran"
    plant = tmp_path / "plant.sh"
    plant.write_text(f'#!/bin/sh\necho "$0" >> "{marker}"\nexit 1\n')
    plant.chmod(0o755)
    for hook in ("pre-commit", "pre-push"):
        target = clone / ".git" / "hooks" / hook
        target.write_text(plant.read_text())
        target.chmod(0o755)
    subprocess.run(["git", "-C", str(clone), "config", "core.fsmonitor", str(plant)], check=True)
    # The control: plain git in the clone runs the plant.
    subprocess.run(["git", "-C", str(clone), "status", "--porcelain"], capture_output=True)
    assert marker.exists(), "the planted monitor never ran: the test is vacuous"
    marker.unlink()

    r = p.push([SyncObject("second.jsonl", b"two")])

    assert r.outcome == "delivered" and r.pushed == 1, r.detail
    assert "second.jsonl" in _files_in_fresh_remote_checkout(remote, tmp_path)
    assert not marker.exists(), marker.read_text()


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
    monkeypatch.setenv("PERSONALCLAW_HOME", str(tmp_path / "pclaw-home"))
    monkeypatch.setenv("HOME", str(tmp_path / "account"))
    p = create_provider(None)
    assert p._repo_url == ""
    assert p._branch == "main"
    assert p._clone == str((tmp_path / "pclaw-home").resolve() / "sync" / "git-sync")
    assert p.test().ok is False


#: The settings the manifest declares, as the Configure page reads them.
_SETTINGS = json.loads((Path(git_sync.__file__).parent / "app.json").read_text(encoding="utf-8"))[
    "provider"
]["settingsSchema"]["properties"]


def _saved_with_defaults(repo_url: str) -> dict:
    """What the Configure page saves when only Git remote URL is filled in: every other setting
    at the default the manifest gives it."""
    defaults = {k: v["default"] for k, v in _SETTINGS.items() if "default" in v}
    return {**defaults, "repo_url": repo_url}


@pytest.mark.parametrize(
    "build",
    [
        lambda url: create_provider({"repo_url": url}),
        lambda url: create_provider(_saved_with_defaults(url)),
        lambda url: GitSyncProvider(repo_url=url),
    ],
    ids=["not-set", "saved-with-the-defaults", "constructed"],
)
def test_an_empty_local_working_clone_is_in_the_home_in_use(remote, tmp_path, monkeypatch, build):
    """Left empty, the working clone is sync/git-sync in the PersonalClaw home in use — the one
    PERSONALCLAW_HOME names — and not a ``~/.personalclaw`` worked out from the account's home,
    which the Configure page used to save as the setting's value. The account's home is the
    ``ssh_url`` fixture's own (``HOME``), so it can be looked in."""
    home = tmp_path / "pclaw-home"
    monkeypatch.setenv("PERSONALCLAW_HOME", str(home))
    account = Path(os.environ["HOME"])
    clone = home.resolve() / "sync" / "git-sync"

    p = build(remote)
    r = p.push([SyncObject("machines/A/k", b"v")])

    assert p._clone == str(clone)
    assert r.outcome == "delivered" and r.pushed == 1, r
    assert (clone / "machines" / "A" / "k").read_bytes() == b"v"
    assert "machines/A/k" in _files_in_fresh_remote_checkout(remote, tmp_path)
    assert not (account / ".personalclaw").exists(), "the clone was made in the account's home"


# ── what a failure says ──────────────────────────────────────────────────────────────
#
# Each case below used to hand back git's own words, or a Python ``CalledProcessError``
# naming the argv, as the whole message. Now it says what is wrong and what to do, and git's
# words follow as the detail.

ON_CARD = "on the Git Sync card in Settings → Providers"
RETRIES = "Sync tries again on its next run."
CREDENTIALS = (
    "the remote didn't accept this machine's credentials, or git had none to give it. Check "
    "that git on this machine can reach it — its SSH key, or a credential helper for an https "
    "URL."
)


@pytest.fixture
def c_locale(monkeypatch):
    """git's messages in English, the words its classification reads."""
    monkeypatch.setenv("LC_ALL", "C")


def _refusing(stderr: str):
    """A ``_run`` stand-in: git ran and answered with ``stderr`` and exit status 128."""

    def _run(self, args, check=True):
        return subprocess.CompletedProcess(["git", *args], 128, stdout="", stderr=stderr)

    return _run


def test_probe_names_a_missing_repository_and_where_to_fix_it(tmp_path, ssh_url, c_locale):
    """git's first stderr line, "fatal: '…' does not appear to be a git repository", used to be
    the whole message."""
    res = GitSyncProvider(
        repo_url=ssh_url(tmp_path / "does-not-exist.git"), local_clone=str(tmp_path / "c")
    ).test()

    assert res.ok is False
    assert res.detail.startswith(
        "Git Sync couldn't read the git remote: no repository at that address is visible to "
        "this machine — it doesn't exist, or this machine's credentials can't see it. Check Git "
        f"remote URL {ON_CARD}. Details: "
    ), res.detail
    assert "does not appear to be a git repository" in res.detail


@pytest.mark.parametrize(
    ("stderr", "trouble"),
    [
        (
            "git@git.example.com: Permission denied (publickey).\n"
            "fatal: Could not read from remote repository.",
            CREDENTIALS,
        ),
        (
            "fatal: could not read Username for 'https://git.example.com': terminal prompts "
            "disabled",
            CREDENTIALS,
        ),
        (
            "Host key verification failed.\nfatal: Could not read from remote repository.",
            "SSH on this machine doesn't trust the remote's host key yet. Connect to that host "
            "once from a terminal here to accept its key.",
        ),
        (
            "@@@@@@@@@@@@@@@@\n@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @\n"
            "@@@@@@@@@@@@@@@@\nHost key verification failed.",
            "the remote's SSH host key has changed since this machine last trusted it, so SSH "
            "refused to connect. Only if you know why it changed, remove its old key from this "
            "machine's known_hosts file, then connect to that host once from a terminal here.",
        ),
        (
            "ssh: Could not resolve hostname git.example.com: nodename nor servname provided, "
            "or not known\nfatal: Could not read from remote repository.",
            "the remote couldn't be reached from this machine. Check that it is online and that "
            f"Git remote URL {ON_CARD} names the right host.",
        ),
        (
            "bash: git-upload-pack: command not found\n"
            "fatal: Could not read from remote repository.",
            "the remote host couldn't run git — it isn't installed there, or isn't on the PATH "
            "its SSH logins get. Install git on that host.",
        ),
    ],
)
def test_probe_says_what_the_remote_refused_and_what_to_do(monkeypatch, stderr, trouble):
    """git's first stderr line used to be the whole message — "…Permission denied (publickey)."
    named nothing to fix, and a host key never seen read the same as one that had changed."""
    monkeypatch.setattr(GitSyncProvider, "_run", _refusing(stderr))

    res = GitSyncProvider(repo_url="git@git.example.com:owner/state.git").test()

    assert res.ok is False
    assert res.detail == (
        f"Git Sync couldn't read the git remote: {trouble} Details: {' '.join(stderr.split())}"
    )


def test_probe_says_when_git_itself_cannot_start(monkeypatch):
    """"[Errno 2] No such file or directory: 'git'" used to be the whole message."""

    def _no_git(self, args, check=True):
        raise FileNotFoundError(2, "No such file or directory", "git")

    monkeypatch.setattr(GitSyncProvider, "_run", _no_git)

    res = GitSyncProvider(repo_url="https://git.example.com/owner/state.git").test()

    assert res.detail == (
        "Git Sync runs the git command, and PersonalClaw couldn't start it on this machine: it "
        "isn't installed, isn't on the PATH PersonalClaw runs with, or isn't executable. Install "
        "git where PersonalClaw can run it. Details: [Errno 2] No such file or directory: 'git'"
    )


def test_a_git_too_old_to_run_is_named_by_the_probe_and_by_a_push(tmp_path, monkeypatch):
    """PersonalClaw's git refuses a git older than 2.12, which ignores some of the settings that
    keep the working clone's own configuration from running a program. Its refusal says what it
    needs, what it found and what to do, so that is the whole message, and a push adds only that
    it retries."""
    ran = put_old_git_on_path(tmp_path, monkeypatch)
    transport = GitSyncProvider(
        repo_url="ssh://git@git.example.com/owner/state.git", local_clone=str(tmp_path / "c")
    )

    probed = transport.test()
    pushed = transport.push([SyncObject("k", b"v")])

    assert probed.ok is False
    assert probed.detail.startswith(REFUSAL_START), probed.detail
    assert "Details:" not in probed.detail
    assert pushed.outcome == "transient"
    assert pushed.detail.startswith(REFUSAL_START) and pushed.detail.endswith(RETRIES)
    assert not ran.exists(), "a refused git never ran"


def test_a_push_that_cannot_clone_names_the_trouble_and_that_it_retries(
    tmp_path, ssh_url, c_locale
):
    """"Command '['git', 'clone', …]' returned non-zero exit status 128." used to be the whole
    message."""
    clone = tmp_path / "c"
    res = GitSyncProvider(
        repo_url=ssh_url(tmp_path / "does-not-exist.git"), local_clone=str(clone)
    ).push([SyncObject("k", b"v")])

    assert res.outcome == "transient"
    assert res.detail.startswith(
        f"Git Sync couldn't clone the git remote into {clone}: no repository at that address is "
        "visible to this machine — it doesn't exist, or this machine's credentials can't see it. "
        f"Check Git remote URL {ON_CARD}. {RETRIES} Details: "
    ), res.detail


def test_a_push_into_a_folder_with_other_files_says_to_pick_an_empty_one(remote, tmp_path, c_locale):
    """A Local working clone pointed at a folder that already holds files. git's "destination
    path … already exists" arrived as "Command '[…]' returned non-zero exit status 128."."""
    clone = tmp_path / "c"
    clone.mkdir()
    (clone / "notes.txt").write_text("a file that is not a git checkout", encoding="utf-8")

    r = _provider(remote, tmp_path, name="c").push([SyncObject("k", b"v")])

    assert r.outcome == "transient"
    assert r.detail.startswith(
        f"Git Sync couldn't clone the git remote into {clone}: that folder already has other "
        f"files in it. Set Local working clone {ON_CARD} to an empty or new folder. {RETRIES} "
        "Details: "
    ), r.detail


def test_a_clone_its_folder_refuses_says_where_and_what_to_set(tmp_path, monkeypatch):
    """git answering a clone with its folder's "Permission denied" — the working clone's
    trouble, not the remote's. It arrived as "Command '[…]' returned non-zero exit status 128."."""
    clone = tmp_path / "c"
    stderr = f"fatal: could not create work tree dir '{clone}': Permission denied\n"

    def _clone_denied(self, args, check=True):
        raise subprocess.CalledProcessError(128, ["git", *args], output="", stderr=stderr)

    monkeypatch.setattr(GitSyncProvider, "_run", _clone_denied)

    r = GitSyncProvider(
        repo_url="https://git.example.com/owner/state.git", local_clone=str(clone)
    ).push([SyncObject("k", b"v")])

    assert r.outcome == "transient"
    assert r.detail.startswith(
        f"Git Sync isn't allowed to write to its working clone at {clone}. Fix that folder's "
        f"permissions, or set Local working clone {ON_CARD} to a folder PersonalClaw can write "
        f"to. {RETRIES} Details: fatal: could not create work tree dir "
    ), r.detail


def test_a_push_that_keeps_losing_races_says_so_and_that_it_retries(
    remote, tmp_path, monkeypatch, c_locale
):
    """Another machine pushed first every time this one caught up. "Git Sync's push was turned
    away because the git remote has commits this machine hasn't pulled" was said after one
    try — and every run after it, since the clone never caught up."""
    a = _provider(remote, tmp_path, name="a")
    b = _provider(remote, tmp_path, name="b")
    a.push([SyncObject("base", b"0")])
    seqs = iter(range(1, 10))
    _Race(
        monkeypatch,
        a,
        lambda: b.push([SyncObject(f"machines/b/seq-{next(seqs):04d}/x.jsonl", b"B")]),
    )

    r = a.push([SyncObject("k", b"v")])

    assert r.outcome == "transient"
    assert r.detail.startswith(
        "Git Sync couldn't push to the git remote: each of the 3 times it caught up with the "
        f"remote and pushed, another push had reached it first. {RETRIES} Details: "
    ), r.detail


def test_a_push_the_remote_rules_refuse_names_the_branch(remote, tmp_path, monkeypatch, c_locale):
    """A remote whose own hook declines the push. The hook's first stderr line used to be the
    whole message; then git's "[remote rejected]" still read as a race, so the push was
    ``transient`` and retried on every run, though retrying cannot change the remote's rules."""
    bare = str(tmp_path / "remote.git")
    hook = os.path.join(bare, "hooks", "pre-receive")
    with open(hook, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\necho 'pushes to this branch are not accepted' >&2\nexit 1\n")
    os.chmod(hook, 0o755)
    # A machine-wide core.hooksPath (a system gitconfig can set one) would run its hooks in
    # place of the remote's own, and the push would land; the remote names its own directory.
    subprocess.run(["git", "-C", bare, "config", "core.hooksPath", os.path.join(bare, "hooks")],
                   check=True, capture_output=True, text=True)
    real_run = GitSyncProvider._run
    pushes: list[list[str]] = []

    def _counting(self, args, check=True):
        if git_sync._subcommand(args) == "push":
            pushes.append(args)
        return real_run(self, args, check=check)

    monkeypatch.setattr(GitSyncProvider, "_run", _counting)

    r = _provider(remote, tmp_path).push([SyncObject("k", b"v")])

    assert r.outcome == "permanent"
    assert r.detail.startswith(
        "Git Sync couldn't push to the git remote: its rules don't let this machine push to "
        f"branch 'main'. Allow that on the remote, or set Branch {ON_CARD} to one that does. "
        "Details: "
    ), r.detail
    assert RETRIES not in r.detail
    assert len(pushes) == 1, "a declined push was caught up and pushed again, as if a race"


# The remote's other refusals, each met for real: the remote is a repository on this machine
# reached through the ssh stand-in, set up to refuse the way a remote does. git's "[remote
# rejected]" read as a race for every one of them — ``transient``, and tried again on every run,
# though no retry changes any of them but the ref lock.


def _count_pushes(monkeypatch) -> list[list[str]]:
    """Every ``git push`` the transport runs, as it runs it."""
    real_run = GitSyncProvider._run
    pushes: list[list[str]] = []

    def _counting(self, args, check=True):
        if git_sync._subcommand(args) == "push":
            pushes.append(args)
        return real_run(self, args, check=check)

    monkeypatch.setattr(GitSyncProvider, "_run", _counting)
    return pushes


def _bare(path) -> str:
    """A new, empty bare repository at *path*."""
    subprocess.run(["git", "init", "--bare", "-b", "main", str(path)], check=True,
                   capture_output=True, text=True)
    return str(path)


def test_a_push_into_a_branch_checked_out_on_the_remote_says_to_use_a_bare_one(
    tmp_path, ssh_url, monkeypatch, c_locale
):
    shared = tmp_path / "shared"
    subprocess.run(["git", "init", "-b", "main", str(shared)], check=True, capture_output=True,
                   text=True)
    _git(str(shared), "config", "receive.denyCurrentBranch", "refuse")  # git's default, pinned
    (shared / "notes.md").write_text("a repository someone works in\n", encoding="utf-8")
    _git(str(shared), "add", "-A")
    _git(str(shared), "commit", "-m", "start")
    pushes = _count_pushes(monkeypatch)

    r = _provider(ssh_url(shared), tmp_path).push([SyncObject("k", b"v")])

    assert r.outcome == "permanent", r.detail
    assert r.detail.startswith(
        "Git Sync couldn't push to the git remote: it is a repository with a working tree that "
        "has branch 'main' checked out, and git won't push into a checked-out branch. Point Git "
        f"remote URL {ON_CARD} at a bare repository (one made with git init --bare), or set Branch "
        "to one that isn't checked out there. Details: "
    ), r.detail
    assert "refusing to update checked out branch" in r.detail
    assert len(pushes) == 1


def test_a_push_from_a_shallow_clone_the_remote_refuses_says_to_clone_afresh(
    tmp_path, ssh_url, monkeypatch, c_locale
):
    """A working clone holding only part of its history (made with ``--depth``) pushing to a
    remote that lacks the rest."""
    history = tmp_path / "history"
    subprocess.run(["git", "init", "-b", "main", str(history)], check=True, capture_output=True,
                   text=True)
    for n in (1, 2):
        (history / f"f{n}").write_text(f"{n}\n", encoding="utf-8")
        _git(str(history), "add", "-A")
        _git(str(history), "commit", "-m", f"commit {n}")
    source = _bare(tmp_path / "source.git")
    _git(str(history), "push", "-q", source, "main")
    target = _bare(tmp_path / "remote.git")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "--depth", "1", ssh_url(source), str(clone)], check=True,
                   capture_output=True, text=True)
    _git(str(clone), "remote", "set-url", "origin", ssh_url(target))
    # Git Sync never makes a shallow clone, and syncs through no clone it didn't make: this one
    # is marked as its own, as if it had made it.
    _git(str(clone), "config", MARK, "true")
    said: list[subprocess.CompletedProcess] = []
    real_run = GitSyncProvider._run

    def _saying(self, args, check=True):
        cp = real_run(self, args, check=check)
        if git_sync._subcommand(args) == "push":
            said.append(cp)
        return cp

    monkeypatch.setattr(GitSyncProvider, "_run", _saying)

    r = GitSyncProvider(repo_url=ssh_url(target), local_clone=str(clone)).push(
        [SyncObject("k", b"v")]
    )

    assert r.outcome == "permanent", r.detail
    assert len(said) == 1
    # git names the remote's URL before its refusal, so how much of the refusal survives the
    # cut depends on how long this run's temporary path is: the words are checked whole here,
    # and the detail as those words cut to fit.
    words = git_sync._words(said[0])
    assert "[remote rejected] main -> main (shallow update not allowed)" in words, words
    assert r.detail == sentence_with_detail(
        f"Git Sync couldn't push to the git remote: the working clone at {clone} is shallow — it "
        "holds only part of its history — and the remote won't take a push from a shallow "
        "clone. If nothing there needs keeping, delete that folder, and Git Sync clones the "
        "remote in full on its next run.",
        words,
    ), r.detail


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes into a read-only directory anyway")
def test_a_remote_that_cannot_store_the_push_says_so(remote, tmp_path, monkeypatch, c_locale):
    p = _provider(remote, tmp_path)
    assert p.push([SyncObject("base", b"0")]).outcome == "delivered"
    objects = tmp_path / "remote.git" / "objects"
    objects.chmod(0o555)  # the account this machine pushes as can't write the objects there
    try:
        pushes = _count_pushes(monkeypatch)
        r = p.push([SyncObject("k", b"v")])
    finally:
        objects.chmod(0o755)

    assert r.outcome == "permanent", r.detail
    assert r.detail.startswith(
        "Git Sync couldn't push to the git remote: the remote couldn't store what this machine "
        "sent — the repository there isn't writable by the account this machine pushes as, or "
        "the disk it is on is full. Fix that on the remote. Details: "
    ), r.detail
    assert len(pushes) == 1


def test_a_branch_name_the_remote_refuses_says_which_setting(
    remote, tmp_path, monkeypatch, c_locale
):
    """A remote that won't take the branch's name. git lets no name through that a stock remote
    then refuses, so the remote's words here are real git's, from a push it refused as a
    "funny refname", handed to the transport's push step."""
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", remote, str(seed)], check=True, capture_output=True,
                   text=True)
    (seed / "f").write_text("x\n", encoding="utf-8")
    _git(str(seed), "add", "-A")
    _git(str(seed), "commit", "-m", "x")
    refused = subprocess.run(["git", "-C", str(seed), "push", "origin", "HEAD:refs/sync"],
                             capture_output=True, text=True)
    assert refused.returncode != 0 and "funny refname" in refused.stderr, refused.stderr
    real_run = GitSyncProvider._run
    pushes: list[list[str]] = []

    def _refused_push(self, args, check=True):
        if git_sync._subcommand(args) == "push":
            pushes.append(args)
            return subprocess.CompletedProcess(
                ["git", *args], refused.returncode, stdout=refused.stdout, stderr=refused.stderr
            )
        return real_run(self, args, check=check)

    monkeypatch.setattr(GitSyncProvider, "_run", _refused_push)

    r = GitSyncProvider(repo_url=remote, local_clone=str(tmp_path / "c"), branch="sync").push(
        [SyncObject("k", b"v")]
    )

    assert r.outcome == "permanent", r.detail
    assert r.detail.startswith(
        "Git Sync couldn't push to the git remote: it won't take 'sync' as the name of a branch. "
        f"Set Branch {ON_CARD} to a name it accepts, such as main. Details: "
    ), r.detail
    assert len(pushes) == 1


def test_a_push_the_remotes_ref_lock_keeps_out_says_so_and_that_it_retries(
    remote, tmp_path, monkeypatch, c_locale
):
    """The one refusal a retry can change: the branch's lock, held by a push landing at that
    moment — or, as here, a lock file an interrupted one left behind."""
    p = _provider(remote, tmp_path)
    assert p.push([SyncObject("base", b"0")]).outcome == "delivered"
    lock = tmp_path / "remote.git" / "refs" / "heads" / "main.lock"
    lock.write_text("", encoding="utf-8")
    pushes = _count_pushes(monkeypatch)

    r = p.push([SyncObject("k", b"v")])

    assert r.outcome == "transient", r.detail
    assert r.detail.startswith(
        "Git Sync couldn't push to the git remote: branch 'main' there was still locked by "
        "another git process after 3 tries — a push landing at the same moment, or an "
        "interrupted one that left refs/heads/main.lock behind. If it keeps happening, remove "
        f"that file in the remote repository. {RETRIES} Details: "
    ), r.detail
    assert "cannot lock ref" in r.detail
    assert len(pushes) == 3, "a taken ref lock was not tried again"
    lock.unlink()
    assert p.push([SyncObject("k2", b"w")]).outcome == "delivered"


@pytest.mark.parametrize("branch", ["bad..name", "has space", "ends.lock", "-dash", "HEAD"])
def test_a_branch_git_will_not_name_is_refused_before_git_runs(remote, tmp_path, branch):
    """git itself said only "invalid refspec", at the push — after the objects had been
    committed to whatever branch the clone had checked out — and nothing named the setting."""
    clone = tmp_path / "clone"
    p = GitSyncProvider(repo_url=remote, local_clone=str(clone), branch=branch)
    says = (
        f"Git Sync can't sync on '{branch}': git doesn't accept that as a branch name. Set Branch "
        f"{ON_CARD} to one it does, such as main."
    )

    _refused_everywhere(p, says)

    assert not clone.exists(), "git ran for a Branch git won't take"


def test_the_branch_names_refused_are_the_ones_git_refuses(remote, tmp_path):
    """The check is git's own rules for a branch name. Drifting from the git in use would refuse
    a branch git takes, or let through one it won't — so each name is put to that git too."""
    names = [
        "main", "feature/x", "sync/main", "v1.0", "a-b", "a@b", "@", "@a", "a{b", "a}b", "日本",
        "a..b", "a b", "a\tb", ".hidden", "x/.y", "x.lock", "x.lock/y", "x/y.lock", "a/", "/a",
        "a//b", "HEAD", "-x", "a~b", "a^b", "a:b", "a?b", "a*b", "a[b", "a\\b", "a@{b", "a.",
        "a./b", "a\x7fb", "..",
    ]
    disagree = []
    for name in names:
        git_takes = subprocess.run(
            ["git", "check-ref-format", "--branch", name], cwd=tmp_path, capture_output=True
        ).returncode == 0
        p = GitSyncProvider(repo_url=remote, local_clone=str(tmp_path / "c"), branch=name)
        probe = p.test()
        refused = probe.detail.startswith("Git Sync can't sync on")
        if refused == git_takes:
            disagree.append((name, git_takes, probe.detail))
    assert not disagree, disagree


def test_a_push_that_cannot_reach_the_remote_says_so_and_that_it_retries(
    remote, tmp_path, monkeypatch
):
    """The network failing at the push — retrying is what fixes it. The push was ``permanent``,
    which gives it up."""
    stderr = (
        "fatal: unable to access 'https://git.example.com/owner/state.git/': Could not resolve "
        "host: git.example.com"
    )
    real_run = GitSyncProvider._run

    def _offline_push(self, args, check=True):
        if git_sync._subcommand(args) == "push":
            return subprocess.CompletedProcess(["git", *args], 128, stdout="", stderr=stderr)
        return real_run(self, args, check=check)

    monkeypatch.setattr(GitSyncProvider, "_run", _offline_push)

    r = _provider(remote, tmp_path).push([SyncObject("k", b"v")])

    assert r.outcome == "transient"
    assert r.detail == (
        "Git Sync couldn't push to the git remote: the remote couldn't be reached from this "
        f"machine. Check that it is online and that Git remote URL {ON_CARD} names the right "
        f"host. {RETRIES} Details: {stderr}"
    )


def test_a_push_that_times_out_names_the_step_and_that_it_retries(tmp_path, monkeypatch):
    """"Command '[…]' timed out after 120 seconds" used to be the whole message."""

    def _slow(self, args, check=True):
        raise subprocess.TimeoutExpired(["git", *args], git_sync._GIT_TIMEOUT)

    monkeypatch.setattr(GitSyncProvider, "_run", _slow)

    r = GitSyncProvider(
        repo_url="https://git.example.com/owner/state.git", local_clone=str(tmp_path / "c")
    ).push([SyncObject("k", b"v")])

    assert r.outcome == "transient"
    assert r.detail.startswith(
        "git clone didn't finish within 120 seconds, so this sync stopped. If it keeps timing "
        "out, check that the git remote is reachable from this machine, and that git reaches it "
        f"without stopping to ask for anything. {RETRIES} Details: "
    ), r.detail


def test_a_local_git_step_that_hangs_says_where_to_look(remote, tmp_path, monkeypatch):
    """A step that never reaches the remote — here the commit — says where it hung rather than
    pointing at the remote."""
    real_run = GitSyncProvider._run

    def _commit_hangs(self, args, check=True):
        if "commit" in args:
            raise subprocess.TimeoutExpired(["git", *args], git_sync._GIT_TIMEOUT)
        return real_run(self, args, check=check)

    monkeypatch.setattr(GitSyncProvider, "_run", _commit_hangs)
    clone = tmp_path / "clone"

    r = _provider(remote, tmp_path).push([SyncObject("k", b"v")])

    assert r.outcome == "transient"
    assert r.detail.startswith(
        f"git commit didn't finish within 120 seconds in the working clone at {clone}, so this "
        "sync stopped. If it keeps happening, run git commit there from a terminal to see what it "
        f"waits for. {RETRIES} Details: "
    ), r.detail


def test_a_git_step_this_machines_git_settings_break_says_to_check_them(
    remote, tmp_path, c_locale
):
    """git set up, in this machine's own git configuration, in a way that breaks a commit: a
    cleanup mode git does not have. The commit's failure used to arrive as "Command '[…]'
    returned non-zero exit status 128."."""
    with open(os.environ["GIT_CONFIG_GLOBAL"], "a", encoding="utf-8") as fh:
        fh.write("[commit]\n\tcleanup = pc-fixture-mode\n")
    clone = tmp_path / "clone"

    r = _provider(remote, tmp_path).push([SyncObject("k", b"v")])

    assert r.outcome == "transient"
    assert r.detail.startswith(
        f"Git Sync couldn't update its working clone at {clone}: git commit failed there. Check "
        f"that folder, and any git settings on this machine that apply to it. {RETRIES} Details: "
    ), r.detail


def test_a_push_that_cannot_write_its_clone_says_where_and_what_to_set(tmp_path, monkeypatch):
    """"[Errno 13] Permission denied: '…'" used to be the whole message."""
    clone = tmp_path / "locked" / "c"

    def _denied(path, *args, **kwargs):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(git_sync.os, "makedirs", _denied)

    r = GitSyncProvider(
        repo_url="https://git.example.com/owner/state.git", local_clone=str(clone)
    ).push([SyncObject("k", b"v")])

    assert r.outcome == "transient"
    assert r.detail.startswith(
        f"Git Sync isn't allowed to write to its working clone at {clone}. Fix that folder's "
        f"permissions, or set Local working clone {ON_CARD} to a folder PersonalClaw can write "
        f"to. {RETRIES} Details: "
    ), r.detail


# ── the remote's refusals that still read as git's generic "no" ─────────────────────────
#
# Each of these fell through to the generic sentence ("Check Git remote URL and Branch …"), as
# a ``transient`` push retried on every run, or was taken for the branch's ref lock. git names
# the reason in brackets ("[remote rejected] main -> main (<reason>)") and, for a ref it could
# not update, why on the remote's own error line; the reasons' words differ between git
# versions ("failed to update ref", "failed to update refs"), so the cause line is what decides.


def _refs(bare) -> str:
    """Every ref in the bare repository at *bare*, with the commit it names."""
    return subprocess.run(
        ["git", "-C", str(bare), "for-each-ref", "--format=%(refname) %(objectname)"],
        check=True, capture_output=True, text=True,
    ).stdout


def test_a_push_to_a_branch_the_remote_hides_says_so_and_is_not_retried(
    remote, tmp_path, monkeypatch, c_locale
):
    """A remote set to hide the branch from pushes (``receive.hideRefs``) turns every push to it
    away as "deny updating a hidden ref", and no retry changes that."""
    subprocess.run(["git", "-C", str(tmp_path / "remote.git"), "config", "receive.hideRefs",
                    "refs/heads/main"], check=True, capture_output=True, text=True)
    pushes = _count_pushes(monkeypatch)

    r = _provider(remote, tmp_path).push([SyncObject("k", b"v")])

    assert r.outcome == "permanent", r.detail
    assert r.detail.startswith(
        "Git Sync couldn't push to the git remote: it hides branch 'main', and takes no push to "
        f"a branch it hides. Set Branch {ON_CARD} to one it doesn't hide, or stop hiding this "
        "one there (git's receive.hideRefs or transfer.hideRefs setting). Details: "
    ), r.detail
    assert len(pushes) == 1


def test_a_remote_missing_objects_its_own_branch_needs_says_its_repository_is_damaged(
    remote, tmp_path, monkeypatch, c_locale
):
    """The remote's repository lost the commit its branch points at, so it can't connect what a
    push sends to what it holds: "missing necessary objects". No push can land until it is
    repaired."""
    p = _provider(remote, tmp_path)
    assert p.push([SyncObject("base", b"0")]).outcome == "delivered"
    bare = tmp_path / "remote.git"
    tip = subprocess.run(["git", "-C", str(bare), "rev-parse", "main"], check=True,
                         capture_output=True, text=True).stdout.strip()
    (bare / "objects" / tip[:2] / tip[2:]).unlink()
    pushes = _count_pushes(monkeypatch)

    r = p.push([SyncObject("k", b"v")])

    assert r.outcome == "permanent", r.detail
    assert r.detail.startswith(
        "Git Sync couldn't push to the git remote: its repository is missing objects its own "
        "history needs, so no push to it can land. Run git fsck in that repository to see what "
        f"is missing and restore it from a backup, or set Git remote URL {ON_CARD} to another "
        "repository. Details: "
    ), r.detail
    assert len(pushes) == 1


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes into a read-only directory anyway")
def test_a_remote_that_cannot_write_its_branch_says_so_rather_than_a_lock(
    remote, tmp_path, monkeypatch, c_locale
):
    """The remote's folder of branches isn't writable by the account this machine pushes as. Its
    "cannot lock ref … Permission denied" was taken for a lock another push held: tried three
    times, and said to remove a lock file there was none of."""
    p = _provider(remote, tmp_path)
    assert p.push([SyncObject("base", b"0")]).outcome == "delivered"
    heads = tmp_path / "remote.git" / "refs" / "heads"
    heads.chmod(0o555)
    try:
        pushes = _count_pushes(monkeypatch)
        r = p.push([SyncObject("k", b"v")])
    finally:
        heads.chmod(0o755)

    assert r.outcome == "permanent", r.detail
    assert r.detail.startswith(
        "Git Sync couldn't push to the git remote: the remote couldn't store what this machine "
        "sent — the repository there isn't writable by the account this machine pushes as, or "
        "the disk it is on is full. Fix that on the remote. Details: "
    ), r.detail
    assert len(pushes) == 1


def test_a_branch_whose_name_clashes_with_one_the_remote_has_says_which(
    remote, tmp_path, monkeypatch, c_locale
):
    """git keeps branch names as paths, so 'sync' can't sit beside a 'sync/main' the remote
    already has: "refname conflict". It was taken for a ref lock and tried again."""
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", remote, str(seed)], check=True, capture_output=True,
                   text=True)
    (seed / "f").write_text("x\n", encoding="utf-8")
    _git(str(seed), "add", "-A")
    _git(str(seed), "commit", "-m", "x")
    _git(str(seed), "push", "origin", "HEAD:refs/heads/sync/main")
    pushes = _count_pushes(monkeypatch)

    r = GitSyncProvider(repo_url=remote, local_clone=str(tmp_path / "c"), branch="sync").push(
        [SyncObject("k", b"v")]
    )

    assert r.outcome == "permanent", r.detail
    assert r.detail.startswith(
        "Git Sync couldn't push to the git remote: it has a branch 'sync/main', and git can't "
        "keep branch 'sync' beside it, since one name is a folder of the other. Set Branch "
        f"{ON_CARD} to another name. Details: "
    ), r.detail
    assert len(pushes) == 1


def _pushes_answered(monkeypatch, returncode: int, stderr: str) -> list[list[str]]:
    """Every ``git push`` the transport runs answered with *stderr*; the rest run for real."""
    real_run = GitSyncProvider._run
    pushes: list[list[str]] = []

    def _answered(self, args, check=True):
        if git_sync._subcommand(args) == "push":
            pushes.append(args)
            return subprocess.CompletedProcess(["git", *args], returncode, stdout="",
                                               stderr=stderr)
        return real_run(self, args, check=check)

    monkeypatch.setattr(GitSyncProvider, "_run", _answered)
    return pushes


def test_a_branch_another_push_moved_under_this_one_is_a_lost_race(
    remote, tmp_path, monkeypatch, c_locale
):
    """Another push moved the branch between the remote advertising it and updating it: "is at
    … but expected …". That is a lost race, caught up with and pushed again, not a lock. git
    forbids a remote's own hook to move a ref inside a push, so no test here can make the remote
    say it; its words are receive-pack's own, handed to the transport's push step."""
    stderr = (
        "remote: error: cannot lock ref 'refs/heads/main': is at "
        "0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c but expected "
        "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d\n"
        "To ssh://example.invalid/srv/state.git\n"
        " ! [remote rejected] main -> main (incorrect old value provided)\n"
        "error: failed to push some refs to 'ssh://example.invalid/srv/state.git'"
    )
    pushes = _pushes_answered(monkeypatch, 1, stderr)

    r = _provider(remote, tmp_path).push([SyncObject("k", b"v")])

    assert r.outcome == "transient", r.detail
    assert r.detail == sentence_with_detail(
        "Git Sync couldn't push to the git remote: each of the 3 times it caught up with the "
        f"remote and pushed, another push had reached it first. {RETRIES}",
        stderr,
    ), r.detail
    assert len(pushes) == 3, "a lost race was not caught up with and pushed again"


def test_a_branch_the_remote_could_not_update_for_its_own_reason_is_tried_again(
    remote, tmp_path, monkeypatch, c_locale
):
    """git's reason for a ref its store refused ("failed to update refs") with a cause this
    transport has no words of its own for: the push landed its objects but not the branch. The
    cause line is a stand-in; each cause known by name has its own test above."""
    stderr = (
        "remote: error: pc-fixture: the ref store refused the update\n"
        "To ssh://example.invalid/srv/state.git\n"
        " ! [remote rejected] main -> main (failed to update refs)\n"
        "error: failed to push some refs to 'ssh://example.invalid/srv/state.git'"
    )
    _pushes_answered(monkeypatch, 1, stderr)

    r = _provider(remote, tmp_path).push([SyncObject("k", b"v")])

    assert r.outcome == "transient", r.detail
    assert r.detail == sentence_with_detail(
        "Git Sync couldn't push to the git remote: it took what this machine sent but couldn't "
        "move branch 'main' to it. If it keeps happening, check that repository on the remote. "
        f"{RETRIES}",
        stderr,
    ), r.detail


# ── a lock file an interrupted git left in the working clone ────────────────────────────
#
# git names the file ("Unable to create '…/main.lock': File exists."). Only the index's lock
# was said as that; any other stopped the catch-up without a word, or read as git failing.


def _lock_left(clone, rel: str) -> str:
    return (
        f"Git Sync couldn't update its working clone at {clone}: another git process is using "
        f"it, or an interrupted one left {rel} behind. Wait for it to finish, or, if no git is "
        f"running there, remove that file. {RETRIES}"
    )


def test_a_stale_lock_on_the_clones_copy_of_the_branch_is_named_not_taken_for_a_race(
    remote, tmp_path, monkeypatch, c_locale
):
    """An interrupted fetch left the clone's copy of the remote's branch locked. Every catch-up
    after it failed without a word, so each push met a remote it hadn't caught up with and, after
    three tries, said another machine kept pushing first."""
    a = _provider(remote, tmp_path, name="a")
    b = _provider(remote, tmp_path, name="b")
    assert a.push([SyncObject("base", b"0")]).outcome == "delivered"
    assert b.push([SyncObject("machines/b/x.jsonl", b"B")]).outcome == "delivered"
    lock = tmp_path / "a" / ".git" / "refs" / "remotes" / "origin" / "main.lock"
    lock.write_text("", encoding="utf-8")
    pushes = _count_pushes(monkeypatch)

    r = a.push([SyncObject("machines/a/x.jsonl", b"A")])

    assert r.outcome == "transient", r.detail
    assert r.detail.startswith(
        _lock_left(tmp_path / "a", ".git/refs/remotes/origin/main.lock") + " Details: "
    ), r.detail
    assert pushes == [], "a clone that couldn't catch up pushed anyway"
    lock.unlink()
    assert a.push([SyncObject("machines/a/y.jsonl", b"A2")]).outcome == "delivered"


def test_a_stale_lock_on_the_clones_branch_is_named_where_the_commit_stops(
    remote, tmp_path, c_locale
):
    """The clone's own branch locked: the commit failed, and said only that git commit failed
    there."""
    p = _provider(remote, tmp_path)
    assert p.push([SyncObject("base", b"0")]).outcome == "delivered"
    (tmp_path / "clone" / ".git" / "refs" / "heads" / "main.lock").write_text("", encoding="utf-8")

    r = p.push([SyncObject("k", b"v")])

    assert r.outcome == "transient", r.detail
    assert r.detail.startswith(
        _lock_left(tmp_path / "clone", ".git/refs/heads/main.lock") + " Details: "
    ), r.detail


def test_a_stale_lock_a_replay_meets_is_named_not_taken_for_a_conflict(
    remote, tmp_path, c_locale
):
    """The same lock met while this machine's unpushed commits were being put on top of the
    remote's: the replay stopped part-way, and that was reported as a conflict to resolve by
    hand, never to be tried again."""
    a = _provider(remote, tmp_path, name="a")
    b = _provider(remote, tmp_path, name="b")
    assert a.push([SyncObject("base", b"0")]).outcome == "delivered"
    assert b.push([SyncObject("machines/b/x.jsonl", b"B")]).outcome == "delivered"
    (tmp_path / "a" / "stranded.jsonl").write_bytes(b"S")
    _git(str(tmp_path / "a"), "add", "-A")
    _git(str(tmp_path / "a"), "commit", "-m", "sync: 1 objects")
    lock = tmp_path / "a" / ".git" / "refs" / "heads" / "main.lock"
    lock.write_text("", encoding="utf-8")

    r = a.push([SyncObject("machines/a/x.jsonl", b"A")])

    assert r.outcome == "transient", r.detail
    assert r.detail.startswith(
        _lock_left(tmp_path / "a", ".git/refs/heads/main.lock") + " Details: "
    ), r.detail
    lock.unlink()
    assert a.push([SyncObject("machines/a/y.jsonl", b"A2")]).outcome == "delivered"
    keys = _files_in_fresh_remote_checkout(remote, tmp_path)
    assert {"stranded.jsonl", "machines/b/x.jsonl", "machines/a/y.jsonl"} <= keys


def test_a_registry_swap_a_stale_lock_stops_says_so_rather_than_losing_a_race(
    remote, tmp_path, c_locale
):
    """The swap caught up first, met the lock, and answered False: a lost race, which the
    cycle re-reads and retries until it gives up as "registry CAS lost"."""
    a = _provider(remote, tmp_path, name="a")
    b = _provider(remote, tmp_path, name="b")
    assert a.push([SyncObject("base", b"0")]).outcome == "delivered"
    assert b.push([SyncObject("machines/b/x.jsonl", b"B")]).outcome == "delivered"
    (tmp_path / "a" / ".git" / "refs" / "remotes" / "origin" / "main.lock").write_text(
        "", encoding="utf-8"
    )

    with pytest.raises(Exception) as caught:
        a.cas_registry(None, b'{"seq": 1}')

    assert str(caught.value).startswith(
        _lock_left(tmp_path / "a", ".git/refs/remotes/origin/main.lock") + " Details: "
    ), caught.value
    assert not (tmp_path / "a" / "registry.json").exists(), "the swap's write stayed in the clone"


# ── a changed Git remote URL ─────────────────────────────────────────────────────────────
#
# The working clone kept the remote it was first cloned from as its origin, and every fetch and
# push went there, whatever Git remote URL said after — to a remote the owner may have left on
# purpose, and git-sync keeps shards plaintext.


#: The key every clone Git Sync makes carries in its configuration, set to ``true``. Pinned
#: here as written, since a clone keeps it for good: renaming it would leave every clone
#: already made reading as a folder Git Sync didn't make.
MARK = "personalclaw.gitSyncClone"


def _unmark(clone: str) -> None:
    """Take the mark off a clone, as a clone an older Git Sync made has none."""
    subprocess.run(["git", "-C", clone, "config", "--unset", MARK], capture_output=True)


def _not_ours(folder) -> str:
    """What a folder at Local working clone holding work Git Sync didn't make says."""
    return (
        f"Git Sync won't commit into or push from {folder}: that folder holds commits or files "
        f"Git Sync didn't make. Set Local working clone {ON_CARD} to a new folder, where Git Sync "
        "makes a clone of its own."
    )


def _two_remotes(tmp_path, ssh_url):
    """The remote a working clone was made from, and the one Git remote URL names now."""
    old_bare, new_bare = tmp_path / "old.git", tmp_path / "new.git"
    return old_bare, ssh_url(_bare(old_bare)), new_bare, ssh_url(_bare(new_bare))


def _left_beside(tmp_path) -> list[str]:
    """Anything a swap left next to the working clone."""
    return sorted(p.name for p in tmp_path.iterdir() if p.name.startswith(".clone"))


def test_a_changed_git_remote_url_takes_the_working_clone_to_the_new_remote(
    tmp_path, ssh_url, c_locale
):
    old_bare, old, new_bare, new = _two_remotes(tmp_path, ssh_url)
    clone = tmp_path / "clone"
    first = GitSyncProvider(repo_url=old, local_clone=str(clone))
    assert first.push([SyncObject("machines/a/seq-0001/x.jsonl", b"1")]).outcome == "delivered"
    old_refs = _refs(old_bare)

    r = GitSyncProvider(repo_url=new, local_clone=str(clone)).push(
        [SyncObject("machines/a/seq-0002/x.jsonl", b"2")]
    )

    assert r.outcome == "delivered", r.detail
    assert _refs(old_bare) == old_refs, "a push after the change reached the old remote"
    # The new remote gets this machine's push, and nothing the old remote held.
    assert _files_in_fresh_remote_checkout(new, tmp_path) == {"machines/a/seq-0002/x.jsonl"}
    assert _git(str(clone), "config", "--get", "remote.origin.url").stdout.strip() == new
    assert _left_beside(tmp_path) == []


def test_after_a_url_change_the_reads_and_the_registry_swap_use_the_new_remote(
    tmp_path, ssh_url, c_locale
):
    old_bare, old, _new_bare, new = _two_remotes(tmp_path, ssh_url)
    seed = GitSyncProvider(repo_url=new, local_clone=str(tmp_path / "seed"))
    assert seed.push([SyncObject("machines/b/seq-0001/y.jsonl", b"new")]).outcome == "delivered"
    clone = tmp_path / "clone"
    first = GitSyncProvider(repo_url=old, local_clone=str(clone))
    assert first.push([SyncObject("machines/a/seq-0001/x.jsonl", b"old")]).outcome == "delivered"
    old_refs = _refs(old_bare)
    p = GitSyncProvider(repo_url=new, local_clone=str(clone))

    assert [ref.key for ref in p.list_remote()] == ["machines/b/seq-0001/y.jsonl"]
    pulled = p.pull(
        [RemoteRef("machines/b/seq-0001/y.jsonl"), RemoteRef("machines/a/seq-0001/x.jsonl")]
    )
    assert [(o.key, o.data) for o in pulled] == [("machines/b/seq-0001/y.jsonl", b"new")]
    assert p.cas_registry(None, b'{"seq": 1}') is True
    assert p.test().ok is True

    assert _refs(old_bare) == old_refs
    assert _remote_bytes(new, tmp_path, "registry.json", "verify") == b'{"seq": 1}'


def test_a_pull_after_a_url_change_serves_nothing_of_the_old_remote_and_fetches_nothing(
    tmp_path, ssh_url, monkeypatch, c_locale
):
    """A pull never makes the clone; it read the old one — after fetching the old remote —
    and handed its objects over as the new remote's."""
    _old_bare, old, _new_bare, new = _two_remotes(tmp_path, ssh_url)
    clone = tmp_path / "clone"
    first = GitSyncProvider(repo_url=old, local_clone=str(clone))
    assert first.push([SyncObject("machines/a/seq-0001/x.jsonl", b"old")]).outcome == "delivered"
    real_run = GitSyncProvider._run
    fetched: list[list[str]] = []

    def _counting(self, args, check=True):
        if git_sync._subcommand(args) == "fetch":
            fetched.append(args)
        return real_run(self, args, check=check)

    monkeypatch.setattr(GitSyncProvider, "_run", _counting)

    pulled = GitSyncProvider(repo_url=new, local_clone=str(clone)).pull(
        [RemoteRef("machines/a/seq-0001/x.jsonl")]
    )

    assert pulled == []
    assert fetched == []


def test_a_registry_swap_after_a_url_change_writes_the_new_remotes_registry(
    tmp_path, ssh_url, c_locale
):
    old_bare, old, _new_bare, new = _two_remotes(tmp_path, ssh_url)
    clone = tmp_path / "clone"
    first = GitSyncProvider(repo_url=old, local_clone=str(clone))
    assert first.cas_registry(None, b'{"seq": 1}') is True
    old_refs = _refs(old_bare)

    assert GitSyncProvider(repo_url=new, local_clone=str(clone)).cas_registry(
        None, b'{"seq": 7}'
    ) is True

    assert _refs(old_bare) == old_refs
    assert _remote_bytes(new, tmp_path, "registry.json", "verify") == b'{"seq": 7}'


def test_a_new_git_remote_url_that_cannot_be_cloned_keeps_the_old_clone_and_reaches_neither(
    tmp_path, ssh_url, monkeypatch, c_locale
):
    """The listing and the registry swap say so too: they answered an empty remote and a lost
    race, and the cycle took the first for a remote with nothing on it."""
    old_bare, old, _new_bare, _new = _two_remotes(tmp_path, ssh_url)
    clone = tmp_path / "clone"
    first = GitSyncProvider(repo_url=old, local_clone=str(clone))
    assert first.push([SyncObject("machines/a/seq-0001/x.jsonl", b"1")]).outcome == "delivered"
    old_refs = _refs(old_bare)
    p = GitSyncProvider(repo_url=ssh_url(tmp_path / "does-not-exist.git"), local_clone=str(clone))
    pushes = _count_pushes(monkeypatch)
    cloning = (
        f"Git Sync couldn't clone the git remote into {clone}: no repository at that address is "
        "visible to this machine — it doesn't exist, or this machine's credentials can't see it. "
        f"Check Git remote URL {ON_CARD}. {RETRIES} Details: "
    )

    r = p.push([SyncObject("machines/a/seq-0002/x.jsonl", b"2")])
    with pytest.raises(git_sync.GitSyncFailed) as listed:
        p.list_remote()
    pulled = p.pull([RemoteRef("machines/a/seq-0001/x.jsonl")])
    with pytest.raises(git_sync.GitSyncFailed) as swapped:
        p.cas_registry(None, b"{}")

    assert r.outcome == "transient", r.detail
    assert r.detail.startswith(cloning), r.detail
    assert str(listed.value).startswith(cloning), listed.value
    assert str(swapped.value).startswith(cloning), swapped.value
    assert pulled == [], "a read served the old remote's objects"
    assert pushes == []
    assert _refs(old_bare) == old_refs
    assert _git(str(clone), "config", "--get", "remote.origin.url").stdout.strip() == old
    assert (clone / "machines" / "a" / "seq-0001" / "x.jsonl").read_bytes() == b"1"
    assert _left_beside(tmp_path) == []


@pytest.mark.parametrize("theirs", ["a commit", "a file"])
def test_a_working_clone_holding_the_owners_own_work_is_never_replaced(
    tmp_path, ssh_url, monkeypatch, c_locale, theirs
):
    """Local working clone pointed at a repository of the owner's, with a remote of its own. It
    was taken for the working clone: the objects were committed into it — with anything else
    uncommitted there — and pushed to that repository's remote."""
    own_bare, own = tmp_path / "own.git", ssh_url(_bare(tmp_path / "own.git"))
    sync_bare, sync = tmp_path / "sync.git", ssh_url(_bare(tmp_path / "sync.git"))
    folder = tmp_path / "project"
    subprocess.run(["git", "clone", own, str(folder)], check=True, capture_output=True, text=True)
    if theirs == "a commit":
        (folder / "notes.md").write_text("the owner's notes\n", encoding="utf-8")
        _git(str(folder), "add", "-A")
        _git(str(folder), "commit", "-m", "notes")
        _git(str(folder), "push", "origin", "HEAD:main")
    else:
        (folder / "draft.txt").write_text("not committed yet\n", encoding="utf-8")
    files = sorted(str(p.relative_to(folder)) for p in folder.rglob("*"))
    refs = (_refs(own_bare), _refs(sync_bare))
    pushes = _count_pushes(monkeypatch)
    p = GitSyncProvider(repo_url=sync, local_clone=str(folder))
    says = _not_ours(folder)

    r = p.push([SyncObject("machines/a/seq-0001/x.jsonl", b"1")])
    probe = p.test()

    assert (r.outcome, r.detail) == ("permanent", says)
    assert (probe.ok, probe.detail) == (False, says), "the probe was green beside a sync refused"
    for refused in (p.list_remote, lambda: p.cas_registry(None, b"{}")):
        with pytest.raises(git_sync.GitSyncFailed) as caught:
            refused()
        assert str(caught.value) == says
    assert p.pull([RemoteRef("notes.md")]) == []
    assert pushes == []
    assert sorted(str(p.relative_to(folder)) for p in folder.rglob("*")) == files
    assert (_refs(own_bare), _refs(sync_bare)) == refs


@pytest.mark.parametrize(
    "setting",
    [
        ("remote.origin.pushurl", "{elsewhere}"),
        ("url.{elsewhere}.pushInsteadOf", "{sync}"),
        ("--add", "remote.origin.url", "{elsewhere}"),
    ],
    ids=["push-url", "push-rewrite", "second-url"],
)
def test_a_working_clone_whose_own_settings_send_pushes_elsewhere_is_replaced(
    tmp_path, ssh_url, c_locale, setting
):
    """Anything that can write the working clone can write its ``.git/config``. A push URL, a
    rewrite or a second URL there sent every push to another remote as well as, or instead of,
    Git remote URL, while the clone's origin still read as Git remote URL."""
    sync = ssh_url(_bare(tmp_path / "sync.git"))
    elsewhere_bare = tmp_path / "elsewhere.git"
    elsewhere = ssh_url(_bare(elsewhere_bare))
    clone = tmp_path / "clone"
    p = GitSyncProvider(repo_url=sync, local_clone=str(clone))
    assert p.push([SyncObject("machines/a/seq-0001/x.jsonl", b"1")]).outcome == "delivered"
    _git(str(clone), "config", *(s.format(elsewhere=elsewhere, sync=sync) for s in setting))
    elsewhere_refs = _refs(elsewhere_bare)

    r = p.push([SyncObject("machines/a/seq-0002/x.jsonl", b"2")])

    assert r.outcome == "delivered", r.detail
    assert _refs(elsewhere_bare) == elsewhere_refs, "a push went where the clone's settings sent it"
    keys = _files_in_fresh_remote_checkout(sync, tmp_path)
    assert {"machines/a/seq-0001/x.jsonl", "machines/a/seq-0002/x.jsonl"} <= keys
    assert _left_beside(tmp_path) == []


# ── the credential Git remote URL can carry ──────────────────────────────────────────────
#
# A token in an https URL (``https://<token>@host/…``) is a common way to give git one. The probe
# said the whole URL back as its success, token and all — and every detail and every error text
# reaches the sync job's result and its audit row, which are not masked the way the logs are.

TOKEN = "pc-fixture-sync-token-5d1e"
#: A token in Git remote URL that git is still handed: in its query, which a host can read one from.
_QUERY_TOKEN_URL = f"https://git.example.com/owner/state.git?access_token={TOKEN}"


def _answering(returncode: int = 0, stderr: str = "", *, hangs: bool = False):
    """A ``_run`` stand-in: git answers every command with ``returncode`` and ``stderr``, as a
    checked run would — or, with ``hangs``, doesn't answer within the timeout."""

    def _run(self, args, check=True):
        cmd = ["git", *args]
        if hangs:
            raise subprocess.TimeoutExpired(cmd, git_sync._GIT_TIMEOUT)
        if returncode and check:
            raise subprocess.CalledProcessError(returncode, cmd, output="", stderr=stderr)
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr=stderr)

    return _run


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        ("ssh://sync-user@git.example.com:2222/owner/state.git",
         "ssh://git.example.com:2222/owner/state.git"),
        (_QUERY_TOKEN_URL, "https://git.example.com/owner/state.git"),
        ("git@git.example.com:owner/state.git", "git.example.com:owner/state.git"),
        ("https://git.example.com/owner/state.git", "https://git.example.com/owner/state.git"),
    ],
    ids=["ssh", "query", "scp-user", "plain"],
)
def test_a_reachable_remote_is_named_without_the_credential_its_url_carries(
    tmp_path, monkeypatch, url, shown
):
    """"git remote reachable: <the URL>" said Git remote URL whole, token included. The last
    case, a URL with nothing before its host, is a control: it reads as it is written. (An
    https URL with a credential written into it, and an ssh one with a password, written as a
    URL or scp-like, are refused before git runs — see
    ``test_an_http_url_with_a_credential_written_into_it_is_refused_without_showing_it`` and
    ``test_an_ssh_url_with_a_password_written_into_it_is_refused_without_showing_it``.)"""
    monkeypatch.setattr(GitSyncProvider, "_run", _answering(0))

    res = GitSyncProvider(repo_url=url, local_clone=str(tmp_path / "c")).test()

    assert (res.ok, res.detail) == (True, f"git remote reachable: {shown}")


@pytest.mark.parametrize(
    ("url", "token"),
    [
        (_QUERY_TOKEN_URL, ""),
        ("https://git.example.com/owner/state.git", TOKEN),
    ],
    ids=["in-the-query", "access-token"],
)
def test_the_credential_in_git_remote_url_appears_in_nothing_git_sync_says(
    tmp_path, ssh_url, monkeypatch, caplog, url, token
):
    """Every text Git Sync hands back — a push's detail, the probe's detail and extra, what a
    listing, a read or a registry swap returns or raises — for a remote that answers, one whose
    error names the URL whole (as an older git's does), one that fails without a word (so the
    failure names its command line), one that never answers, a clone that fails, and a working
    clone Git Sync won't sync through. The token is in the URL's query, or in Access token. (A
    URL that is refused before git runs says only its refusal: see the refusal tests.)"""
    real_run = GitSyncProvider._run
    surfaces: list[str] = []
    signs_in = {"token": token} if token else {}

    def _drive(p: GitSyncProvider) -> None:
        pushed, probed = p.push([SyncObject("k", b"v")]), p.test()
        surfaces.extend([pushed.detail, probed.detail, repr(probed.extra), repr(p), str(p)])
        for call in (
            p.list_remote,
            lambda: p.pull([RemoteRef("k")]),
            lambda: p.cas_registry(None, b"{}"),
        ):
            try:
                surfaces.append(repr(call()))
            except Exception as exc:  # noqa: BLE001 — what any of them raises is a surface too
                surfaces.append(str(exc))

    fakes = [
        _answering(0),
        _answering(128, f"fatal: unable to access '{url}/': Could not resolve host: example"),
        _answering(128),
        _answering(hangs=True),
    ]
    with caplog.at_level(logging.DEBUG):
        for n, fake in enumerate(fakes):
            monkeypatch.setattr(GitSyncProvider, "_run", fake)
            clone = str(tmp_path / f"clone-{n}")
            _drive(GitSyncProvider(repo_url=url, local_clone=clone, **signs_in))
        # A folder of someone's own work at Local working clone, cloned from another remote.
        monkeypatch.setattr(GitSyncProvider, "_run", real_run)
        kept = tmp_path / "kept"
        subprocess.run(["git", "init", "-q", "-b", "main", str(kept)], check=True)
        (kept / "notes.md").write_text("the owner's notes\n", encoding="utf-8")
        _git(str(kept), "add", "-A")
        _git(str(kept), "commit", "-m", "notes")
        _git(str(kept), "remote", "add", "origin", ssh_url(tmp_path / "elsewhere.git"))
        _drive(GitSyncProvider(repo_url=url, local_clone=str(kept), **signs_in))
    surfaces.extend(record.getMessage() for record in caplog.records)

    # VACUITY FLOORS: the URL, as shown, must reach the details of failures that name it — or
    # the scan below would pass on a detail cut short before the URL, not on one masked.
    named = [s for s in surfaces if "owner/state.git" in s and "Details:" in s]
    assert len(named) >= 3, surfaces
    assert any("Git Sync didn't make" in s for s in surfaces), "the kept clone was never refused"
    for s in surfaces:
        assert TOKEN not in s, f"the credential leaked into: {s!r}"


# ── a folder Git Sync didn't make ────────────────────────────────────────────────────────
#
# Local working clone can name any folder, a repository of the owner's own included. When that
# repository's remote is the one Git remote URL names, nothing about its remote gave it away:
# Git Sync checked out its branch in the owner's working tree, committed the plaintext shards
# into it, with whatever else was uncommitted there, and pushed them to the owner's remote.


def _tree(folder) -> dict[str, bytes]:
    """Every file under ``folder``, ``.git`` included, with its bytes."""
    return {
        str(p.relative_to(folder)): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()
    }


def _owners_repository(tmp_path, ssh_url):
    """A repository of the owner's own, cloned from a remote of theirs: a commit of theirs on
    ``main``, their own branch checked out, one change staged, another not, and a file not yet
    added. Returns the folder, the remote's folder and the remote's URL."""
    own_bare = tmp_path / "own.git"
    own = ssh_url(_bare(own_bare))
    folder = tmp_path / "project"
    subprocess.run(["git", "clone", own, str(folder)], check=True, capture_output=True, text=True)
    notes = folder / "notes.md"
    notes.write_text("the owner's notes\n", encoding="utf-8")
    _git(str(folder), "add", "-A")
    _git(str(folder), "commit", "-m", "notes")
    _git(str(folder), "push", "-q", "origin", "HEAD:main")
    _git(str(folder), "checkout", "-q", "-b", "drafts")
    notes.write_text("the owner's notes, staged\n", encoding="utf-8")
    _git(str(folder), "add", "notes.md")
    notes.write_text("the owner's notes, staged, then edited\n", encoding="utf-8")
    (folder / "draft.txt").write_text("not added yet\n", encoding="utf-8")
    return folder, own_bare, own


def test_a_repository_of_the_owners_with_git_remote_url_as_its_remote_is_never_touched(
    tmp_path, ssh_url, monkeypatch, c_locale
):
    """Every entry point, driven for real: the owner's branch, HEAD, index, working tree,
    configuration and remote are byte-for-byte as they were."""
    folder, own_bare, own = _owners_repository(tmp_path, ssh_url)
    before = (_tree(folder), _refs(own_bare))
    pushes = _count_pushes(monkeypatch)
    p = GitSyncProvider(repo_url=own, local_clone=str(folder))
    says = _not_ours(folder)

    pushed = p.push([SyncObject("machines/a/seq-0001/x.jsonl", b"1")])
    probe = p.test()
    raised = []
    for refused in (p.list_remote, lambda: p.cas_registry(None, b"{}")):
        with pytest.raises(git_sync.GitSyncFailed) as caught:
            refused()
        raised.append(str(caught.value))
    pulled = p.pull([RemoteRef("notes.md"), RemoteRef("draft.txt")])

    assert (pushed.outcome, pushed.detail) == ("permanent", says)
    assert (probe.ok, probe.detail) == (False, says)
    assert raised == [says, says]
    assert pulled == [], "a read served the owner's files as sync objects"
    assert pushes == []
    assert (_tree(folder), _refs(own_bare)) == before


def test_a_clone_an_older_git_sync_made_is_taken_as_its_own_and_marked(remote, tmp_path, c_locale):
    """A clone made before clones were marked, holding only Git Sync's own work, syncs on."""
    p = _provider(remote, tmp_path)
    assert p.push([SyncObject("machines/a/seq-0001/x.jsonl", b"1")]).outcome == "delivered"
    clone = str(tmp_path / "clone")
    _unmark(clone)  # as an older Git Sync made it

    r = p.push([SyncObject("machines/a/seq-0002/x.jsonl", b"2")])

    assert r.outcome == "delivered", r.detail
    assert _git(clone, "config", "--get", MARK).stdout.strip() == "true"
    assert "machines/a/seq-0002/x.jsonl" in _files_in_fresh_remote_checkout(remote, tmp_path)


def test_an_unmarked_clone_holding_a_commit_git_sync_didnt_make_is_not_taken_up(
    remote, tmp_path, monkeypatch, c_locale
):
    """Unmarked, a clone is Git Sync's only if every commit in it is: a folder's own remote
    tells nothing, since it can be Git remote URL itself. So a clone an older Git Sync made of a
    remote whose first commit a hosting service made is left alone too, and the sync says to set
    Local working clone to a new folder, where Git Sync makes — and marks — a clone of its own."""
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", remote, str(seed)], check=True, capture_output=True, text=True)
    (seed / "README.md").write_text("# state\n", encoding="utf-8")
    _git(str(seed), "add", "-A")
    _git(str(seed), "commit", "-m", "a hosting service's first commit")
    _git(str(seed), "push", "-q", "origin", "HEAD:main")
    p = _provider(remote, tmp_path)
    assert p.push([SyncObject("machines/a/seq-0001/x.jsonl", b"1")]).outcome == "delivered"
    clone = tmp_path / "clone"
    _unmark(str(clone))
    pushes = _count_pushes(monkeypatch)

    r = p.push([SyncObject("machines/a/seq-0002/x.jsonl", b"2")])

    assert (r.outcome, r.detail) == ("permanent", _not_ours(clone))
    assert pushes == []


# ── a changed Git remote URL, from a clone of Git Sync's own ─────────────────────────────


def _seeded(tmp_path, ssh_url, name: str):
    """A remote a hosting service made with a first commit of its own (a README), as the folder
    holding it and the URL that reaches it."""
    bare = tmp_path / f"{name}.git"
    url = ssh_url(_bare(bare))
    seed = tmp_path / f"{name}-seed"
    subprocess.run(["git", "clone", url, str(seed)], check=True, capture_output=True, text=True)
    (seed / "README.md").write_text("# state\n", encoding="utf-8")
    _git(str(seed), "add", "-A")
    _git(str(seed), "commit", "-m", "a hosting service's first commit")
    _git(str(seed), "push", "-q", "origin", "HEAD:main")
    return bare, url


def test_a_url_change_swaps_a_clone_whose_old_remote_had_a_commit_git_sync_didnt_make(
    tmp_path, ssh_url, c_locale
):
    """A hosting service's first commit on the old remote is that remote's, not the clone's:
    the swap was refused as if the clone held someone else's work. The new remote's own first
    commit doesn't hold its fresh clone back either — then, or on any run after."""
    old_bare, old = _seeded(tmp_path, ssh_url, "old")
    _new_bare, new = _seeded(tmp_path, ssh_url, "new")
    clone = tmp_path / "clone"
    first = GitSyncProvider(repo_url=old, local_clone=str(clone))
    assert first.push([SyncObject("machines/a/seq-0001/x.jsonl", b"1")]).outcome == "delivered"
    old_refs = _refs(old_bare)
    p = GitSyncProvider(repo_url=new, local_clone=str(clone))

    swapped = p.push([SyncObject("machines/a/seq-0002/x.jsonl", b"2")])
    later = p.push([SyncObject("machines/a/seq-0003/x.jsonl", b"3")])

    assert swapped.outcome == "delivered", swapped.detail
    assert later.outcome == "delivered", later.detail
    assert _refs(old_bare) == old_refs
    assert _files_in_fresh_remote_checkout(new, tmp_path) == {
        "README.md", "machines/a/seq-0002/x.jsonl", "machines/a/seq-0003/x.jsonl"
    }
    assert _left_beside(tmp_path) == []


@pytest.mark.parametrize("theirs", ["a commit", "a file"])
def test_after_a_url_change_a_clone_holding_work_its_remote_never_had_is_kept(
    tmp_path, ssh_url, monkeypatch, c_locale, theirs
):
    """The swap's other half: a commit made by hand in Git Sync's own clone that the old remote
    never got, or a file made there by hand, is work a swap would throw away. The clone stays as
    it is, and neither remote is synced through."""
    old_bare, old, new_bare, new = _two_remotes(tmp_path, ssh_url)
    clone = tmp_path / "clone"
    first = GitSyncProvider(repo_url=old, local_clone=str(clone))
    assert first.push([SyncObject("machines/a/seq-0001/x.jsonl", b"1")]).outcome == "delivered"
    (clone / "notes.md").write_text("made by hand\n", encoding="utf-8")
    if theirs == "a commit":
        _git(str(clone), "add", "-A")
        _git(str(clone), "commit", "-m", "made by hand")
    before = (_tree(clone), _refs(old_bare), _refs(new_bare))
    pushes = _count_pushes(monkeypatch)

    r = GitSyncProvider(repo_url=new, local_clone=str(clone)).push(
        [SyncObject("machines/a/seq-0002/x.jsonl", b"2")]
    )

    assert (r.outcome, r.detail) == ("permanent", _not_ours(clone))
    assert pushes == []
    assert (_tree(clone), _refs(old_bare), _refs(new_bare)) == before


def test_a_replaced_clone_keeps_the_old_folders_permissions(tmp_path, ssh_url, c_locale):
    """The fresh clone is made in a folder only this account can open (0700), and the swapped-in
    clone kept that, not the old folder's 0755."""
    _old_bare, old, _new_bare, new = _two_remotes(tmp_path, ssh_url)
    clone = tmp_path / "clone"
    first = GitSyncProvider(repo_url=old, local_clone=str(clone))
    assert first.push([SyncObject("machines/a/seq-0001/x.jsonl", b"1")]).outcome == "delivered"
    clone.chmod(0o755)

    r = GitSyncProvider(repo_url=new, local_clone=str(clone)).push(
        [SyncObject("machines/a/seq-0002/x.jsonl", b"2")]
    )

    assert r.outcome == "delivered", r.detail
    assert oct(stat.S_IMODE(clone.stat().st_mode)) == oct(0o755)
    assert _git(str(clone), "config", "--get", "remote.origin.url").stdout.strip() == new


# ── a listing, a read or a registry swap that can't be made ─────────────────────────────
#
# Each answered as if nothing were wrong: a listing that failed was empty (or stale), a read
# that failed dropped the object, a registry swap that failed lost a race — and the cycle took
# an empty listing for a remote with nothing on it, and retried a swap five times before giving
# up as "registry CAS lost". Each now raises what stopped it.


def test_a_listing_of_a_remote_that_cannot_be_cloned_says_why(tmp_path, ssh_url, c_locale):
    clone = tmp_path / "c"
    p = GitSyncProvider(repo_url=ssh_url(tmp_path / "does-not-exist.git"), local_clone=str(clone))

    with pytest.raises(git_sync.GitSyncFailed) as caught:
        p.list_remote()

    assert str(caught.value).startswith(
        f"Git Sync couldn't clone the git remote into {clone}: no repository at that address is "
        "visible to this machine — it doesn't exist, or this machine's credentials can't see it. "
        f"Check Git remote URL {ON_CARD}. {RETRIES} Details: "
    ), caught.value


def test_a_catch_up_that_cannot_reach_the_remote_is_said_by_the_listing_the_read_and_the_swap(
    remote, tmp_path, c_locale
):
    """The remote's repository gone once the clone was made (moved, or its disk not there): the
    catch-up's fetch failed without a word, and the clone was served as the remote."""
    p = _provider(remote, tmp_path)
    assert p.push([SyncObject("machines/a/seq-0001/x.jsonl", b"1")]).outcome == "delivered"
    (tmp_path / "remote.git").rename(tmp_path / "moved.git")
    says = (
        "Git Sync couldn't fetch from the git remote: no repository at that address is visible to "
        "this machine — it doesn't exist, or this machine's credentials can't see it. Check Git "
        f"remote URL {ON_CARD}. {RETRIES} Details: "
    )

    for step in (
        p.list_remote,
        lambda: p.pull([RemoteRef("machines/a/seq-0001/x.jsonl")]),
        lambda: p.cas_registry(None, b'{"seq": 1}'),
    ):
        with pytest.raises(git_sync.GitSyncFailed) as caught:
            step()
        assert str(caught.value).startswith(says), caught.value
    assert not (tmp_path / "clone" / "registry.json").exists()


def test_a_listing_a_stale_lock_stops_says_so_rather_than_serving_the_clone(
    remote, tmp_path, c_locale
):
    a = _provider(remote, tmp_path, name="a")
    b = _provider(remote, tmp_path, name="b")
    assert a.push([SyncObject("base", b"0")]).outcome == "delivered"
    assert b.push([SyncObject("machines/b/x.jsonl", b"B")]).outcome == "delivered"
    lock = tmp_path / "a" / ".git" / "refs" / "remotes" / "origin" / "main.lock"
    lock.write_text("", encoding="utf-8")

    with pytest.raises(git_sync.GitSyncFailed) as caught:
        a.list_remote()

    assert str(caught.value).startswith(
        _lock_left(tmp_path / "a", ".git/refs/remotes/origin/main.lock") + " Details: "
    ), caught.value
    lock.unlink()
    assert "machines/b/x.jsonl" in {ref.key for ref in a.list_remote()}


def test_a_listing_of_a_clone_git_will_not_bring_level_says_so_rather_than_serving_it(
    remote, tmp_path, c_locale
):
    """A change left uncommitted in the clone to a file the remote has: git won't start the
    replay, so the clone stays behind the remote — and was served as the remote all the same."""
    a = _provider(remote, tmp_path, name="a")
    b = _provider(remote, tmp_path, name="b")
    assert a.push([SyncObject("base", b"0")]).outcome == "delivered"
    assert b.push([SyncObject("machines/b/x.jsonl", b"B")]).outcome == "delivered"
    (tmp_path / "a" / "base").write_bytes(b"changed, and never committed")

    with pytest.raises(git_sync.GitSyncFailed) as caught:
        a.list_remote()

    assert str(caught.value).startswith(
        f"Git Sync couldn't update its working clone at {tmp_path / 'a'}: git rebase failed "
        "there. Check that folder, and any git settings on this machine that apply to it. "
        f"{RETRIES} Details: "
    ), caught.value


def test_a_remote_with_nothing_on_the_branch_yet_lists_as_empty(tmp_path, ssh_url, c_locale):
    """A control: the one listing that is empty and says nothing — a brand-new remote, and one
    whose only branch is another."""
    empty = ssh_url(_bare(tmp_path / "empty.git"))
    others = ssh_url(_bare(tmp_path / "others.git"))
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", others, str(seed)], check=True, capture_output=True,
                   text=True)
    (seed / "f").write_text("x\n", encoding="utf-8")
    _git(str(seed), "add", "-A")
    _git(str(seed), "commit", "-m", "x")
    _git(str(seed), "push", "-q", "origin", "HEAD:refs/heads/other")

    for n, url in enumerate((empty, others)):
        p = GitSyncProvider(repo_url=url, local_clone=str(tmp_path / f"c{n}"))
        assert p.list_remote() == []


def test_a_real_conflict_is_said_by_a_registry_swap_rather_than_lost_as_a_race(
    remote, tmp_path, c_locale
):
    """A file changed by hand in the clone and on the remote as well: the swap went on from a
    clone its catch-up couldn't bring level, and every push after read as a lost race."""
    a = _provider(remote, tmp_path, name="a")
    a.push([SyncObject("notes.md", b"first\n")])
    b = _provider(remote, tmp_path, name="b")
    b.list_remote()
    clone_b = tmp_path / "b"
    (clone_b / "notes.md").write_bytes(b"edited in this clone\n")
    _git(str(clone_b), "commit", "-am", "an edit made by hand")
    (tmp_path / "a" / "notes.md").write_bytes(b"edited on the remote\n")
    _git(str(tmp_path / "a"), "commit", "-am", "an edit made elsewhere")
    _git(str(tmp_path / "a"), "push", "-q", "origin", "main")

    with pytest.raises(git_sync.GitSyncFailed) as caught:
        b.cas_registry(None, b'{"seq": 1}')

    assert str(caught.value).startswith(
        "Git Sync couldn't put this machine's unpushed commits on top of what the git remote has: "
        "notes.md was changed on both sides in ways git can't combine."
    ), caught.value
    assert not (clone_b / "registry.json").exists()


def _declining_registry_writes(bare) -> None:
    """A hook on the remote that turns away any push whose branch would hold ``registry.json``,
    and takes every other."""
    hook = os.path.join(bare, "hooks", "pre-receive")
    with open(hook, "w", encoding="utf-8") as fh:
        fh.write(
            "#!/bin/sh\n"
            "while read old new ref; do\n"
            '  if git ls-tree --name-only "$new" | grep -qx registry.json; then\n'
            "    echo 'registry writes are not accepted here' >&2\n"
            "    exit 1\n"
            "  fi\n"
            "done\n"
        )
    os.chmod(hook, 0o755)
    hooks = os.path.join(bare, "hooks")
    subprocess.run(["git", "-C", str(bare), "config", "core.hooksPath", hooks], check=True,
                   capture_output=True, text=True)


RULES = (
    "Git Sync couldn't push to the git remote: its rules don't let this machine push to branch "
    f"'main'. Allow that on the remote, or set Branch {ON_CARD} to one that does. Details: "
)


def test_a_registry_swap_the_remote_refuses_says_why_rather_than_losing_a_race(
    remote, tmp_path, c_locale
):
    _declining_registry_writes(tmp_path / "remote.git")
    p = _provider(remote, tmp_path)
    assert p.push([SyncObject("machines/a/seq-0001/x.jsonl", b"1")]).outcome == "delivered"

    with pytest.raises(git_sync.GitSyncFailed) as caught:
        p.cas_registry(None, b'{"seq": 1}')

    assert str(caught.value).startswith(RULES), caught.value
    assert not (tmp_path / "clone" / "registry.json").exists(), "the swap's write stayed"


def test_the_sync_cycle_reports_a_refused_registry_swap_in_those_words(
    remote, tmp_path, c_locale
):
    """Driven through the real sync cycle: it reported success, with "registry CAS lost after
    5 attempts" in the push's detail and no error."""
    from personalclaw.durability.shards import machine_id
    from personalclaw.durability.sync_cycle import run_sync_cycle

    _declining_registry_writes(tmp_path / "remote.git")
    home = tmp_path / "pc-home"
    home.mkdir()
    machine_id(home)

    report = run_sync_cycle(_provider(remote, tmp_path), home, self_id="A", now="t1",
                            encrypt="off")

    assert report.ok is False, report.detail
    assert report.error.startswith(f"push: {RULES}"), report.error
    assert "registry CAS lost" not in report.detail


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads an unreadable file anyway")
def test_a_read_of_an_object_the_clone_cannot_read_says_so_rather_than_dropping_it(
    remote, tmp_path, c_locale
):
    """A file in the working clone this account can't read was dropped as if the remote had no
    such object — for the registry, read as a remote with none."""
    p = _provider(remote, tmp_path)
    assert p.cas_registry(None, b'{"seq": 1}') is True
    registry = tmp_path / "clone" / "registry.json"
    registry.chmod(0o000)
    try:
        with pytest.raises(git_sync.GitSyncFailed) as caught:
            p.pull([RemoteRef("registry.json")])
    finally:
        registry.chmod(0o644)

    assert str(caught.value).startswith(
        f"Git Sync isn't allowed to read registry.json in its working clone at "
        f"{tmp_path / 'clone'}. Fix its permissions. {RETRIES} Details: "
    ), caught.value


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads an unreadable folder anyway")
def test_a_listing_of_a_folder_the_clone_cannot_read_says_so_rather_than_leaving_it_out(
    remote, tmp_path, c_locale
):
    p = _provider(remote, tmp_path)
    assert p.push([SyncObject("machines/a/seq-0001/x.jsonl", b"1")]).outcome == "delivered"
    folder = tmp_path / "clone" / "machines" / "a"
    folder.chmod(0o000)
    try:
        with pytest.raises(git_sync.GitSyncFailed) as caught:
            p.list_remote()
    finally:
        folder.chmod(0o755)

    assert str(caught.value).startswith(
        f"Git Sync isn't allowed to read machines/a in its working clone at {tmp_path / 'clone'}. "
        f"Fix its permissions. {RETRIES} Details: "
    ), caught.value


# ── Access token: a token for an https remote, kept out of the clone and every command line ──
#
# The only way to give Git Sync a token was to write it into Git remote URL: git kept it in the
# working clone's .git/config and passed it on its command line, where anyone on this machine can
# read it, and git asked the owner's own credential helpers too. Driven for real here: a bare
# repository served through ``git http-backend`` behind Basic auth on 127.0.0.1, and the owner's
# helper a stand-in that records what git asks of it — never this machine's own keychain.

SIGN_IN = "sync-user"
ACCESS = "pc-fixture-access-token-4b8e"
OWNERS = "pc-fixture-owners-password-9d1c"
_PROXIES = ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY")


class _TokenHost:
    """A bare repository at ``/sync.git`` served by ``git http-backend``, answering only a request
    signed in as ``user`` with ``password``. ``signed_in`` records each request's Authorization."""

    def __init__(self, root: Path, user: str, password: str) -> None:
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(root / "sync.git")],
                       check=True)
        want = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self.bare = root / "sync.git"
        self.signed_in: list[str | None] = []
        host = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # the test output is not a request log
                pass

            def _answer(self) -> None:
                auth = self.headers.get("Authorization")
                host.signed_in.append(auth)
                if auth != want:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="sync"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                try:
                    self._backend()
                except Exception:  # noqa: BLE001 — answer, so no git waits on a dead request
                    self.send_response(500)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

            def _backend(self) -> None:
                path, _, query = self.path.partition("?")
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                env = {
                    "PATH": os.environ["PATH"],
                    "HOME": str(root),
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_PROJECT_ROOT": str(root),
                    "GIT_HTTP_EXPORT_ALL": "1",
                    "PATH_INFO": path,
                    "QUERY_STRING": query,
                    "REQUEST_METHOD": self.command,
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    "CONTENT_LENGTH": str(len(body)),
                    "HTTP_CONTENT_ENCODING": self.headers.get("Content-Encoding", ""),
                    "REMOTE_USER": user,
                    "REMOTE_ADDR": "127.0.0.1",
                }
                out = subprocess.run(
                    ["git", "http-backend"], input=body, env=env, capture_output=True, check=True,
                    timeout=60,
                ).stdout
                cut = min(i for i in (out.find(b"\r\n\r\n"), out.find(b"\n\n")) if i != -1)
                head, rest = out[:cut].decode(), out[cut:].lstrip(b"\r\n")
                status, headers = 200, []
                for line in head.splitlines():
                    key, _, value = line.partition(":")
                    if key.lower() == "status":
                        status = int(value.split()[0])
                    elif key:
                        headers.append((key, value.strip()))
                self.send_response(status)
                for key, value in headers:
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(rest)))
                self.end_headers()
                self.wfile.write(rest)

            do_GET = do_POST = _answer

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/sync.git"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def web_home(tmp_path, monkeypatch):
    """A scratch HOME with an empty git configuration, no proxy between git and 127.0.0.1, and
    the owner's own credential helper a stand-in that records each thing git asks of it and
    answers ``get`` with the owner's password — the wrong one for the host. Returns a reader of
    what it was asked."""
    from personalclaw.net import git as net_git

    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for name in _PROXIES:
        monkeypatch.delenv(name, raising=False)
    record = tmp_path / "owners-helper-was-asked"
    helper = tmp_path / "owners-helper.sh"
    helper.write_text(
        "#!/bin/sh\n"
        "cat >/dev/null\n"
        f'echo "$1" >> "{record}"\n'
        f'[ "$1" = get ] && printf "username={SIGN_IN}\\npassword={OWNERS}\\n"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    monkeypatch.setattr(net_git, "_owner_auth_settings", lambda: [f"credential.helper={helper}"])
    return lambda: record.read_text(encoding="utf-8").split() if record.exists() else []


@pytest.fixture
def token_host(tmp_path, web_home):
    served = _TokenHost(tmp_path / "served", SIGN_IN, ACCESS)
    yield served
    served.close()


def _recorded(monkeypatch) -> list[tuple[list[str], dict]]:
    """Every process Git Sync starts, as the argv and environment it starts it with."""
    real = git_sync.subprocess.run
    runs: list[tuple[list[str], dict]] = []

    def _run(argv, *args, **kwargs):
        runs.append((list(argv), dict(kwargs.get("env") or {})))
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(git_sync.subprocess, "run", _run)
    return runs


def _files_holding(folder: Path, secret: str) -> list[str]:
    """Each file under ``folder`` whose bytes hold ``secret``."""
    return sorted(
        str(p.relative_to(folder))
        for p in folder.rglob("*")
        if p.is_file() and secret.encode() in p.read_bytes()
    )


def _web_provider(url: str, tmp_path, **settings) -> GitSyncProvider:
    """The transport as its settings build it (``create_provider``, which reads Access token and
    User name from them)."""
    return create_provider({"repo_url": url, "local_clone": str(tmp_path / "clone"), **settings})


def test_access_token_signs_git_sync_in_and_is_kept_nowhere(
    token_host, web_home, tmp_path, monkeypatch, c_locale
):
    """A clone, a push, a catch-up and the probe, each signed in with Access token: no command
    line git-sync starts holds it, no file under the working clone's .git does, and no credential
    helper of the owner's is asked for a sign-in or told to keep one."""
    runs = _recorded(monkeypatch)
    p = _web_provider(token_host.url, tmp_path, token=ACCESS, username=SIGN_IN)

    pushed = p.push([SyncObject("machines/a/seq-0001/x.jsonl", b"1")])
    listed = [ref.key for ref in p.list_remote()]
    swapped = p.cas_registry(None, b'{"seq": 1}')
    probed = p.test()

    assert pushed.outcome == "delivered", pushed.detail
    assert listed == ["machines/a/seq-0001/x.jsonl"]
    assert swapped is True
    assert probed.ok is True, probed.detail
    signed = "Basic " + base64.b64encode(f"{SIGN_IN}:{ACCESS}".encode()).decode()
    assert signed in token_host.signed_in, token_host.signed_in
    assert web_home() == [], "the owner's credential helper was asked, or told to keep the token"
    assert runs, "no git ran"
    assert not [argv for argv, _env in runs if any(ACCESS in part for part in argv)]
    assert _files_holding(tmp_path / "clone" / ".git", ACCESS) == []
    shown = _git(str(tmp_path / "clone"), "config", "--get", "remote.origin.url").stdout.strip()
    assert shown == token_host.url


def test_an_empty_user_name_signs_in_as_x_access_token(tmp_path, web_home, c_locale):
    """git asks for a user name with a token and fails without one, since nobody is there to
    type it: an empty User name signs in as x-access-token."""
    host = _TokenHost(tmp_path / "served", "x-access-token", ACCESS)
    try:
        r = _web_provider(host.url, tmp_path, token=ACCESS).push([SyncObject("k", b"v")])
    finally:
        host.close()

    assert r.outcome == "delivered", r.detail
    assert web_home() == []


def test_without_access_token_the_owners_own_sign_in_is_what_git_uses(
    token_host, web_home, tmp_path, c_locale
):
    """A control, so the silence of the owner's helper above means something: with no Access
    token it is the helper git asks, and its password is the wrong one for this host."""
    r = _web_provider(token_host.url, tmp_path).push([SyncObject("k", b"v")])

    assert r.outcome == "transient", r.detail
    assert web_home()[:1] == ["get"], web_home()
    assert "didn't accept this machine's credentials" in r.detail, r.detail


def test_access_token_is_never_handed_to_an_ssh_remote(remote, tmp_path, monkeypatch, c_locale):
    """A control: ssh signs in with the owner's key, so neither the token nor its helper reaches
    a git that talks to an ssh remote."""
    runs = _recorded(monkeypatch)

    r = create_provider(
        {"repo_url": remote, "local_clone": str(tmp_path / "clone"), "token": ACCESS}
    ).push([SyncObject("k", b"v")])

    assert r.outcome == "delivered", r.detail
    assert runs
    assert not [env for _argv, env in runs if ACCESS in env.values()]
    assert not [argv for argv, _env in runs if any("PERSONALCLAW_GIT_TOKEN" in a for a in argv)]


def _no_git(monkeypatch) -> list[list[str]]:
    """Every git Git Sync starts, none of them run: a check that no git starts at all."""
    started: list[list[str]] = []

    def _run(argv, *args, **kwargs):
        started.append(list(argv))
        return subprocess.CompletedProcess(argv, 128, stdout="", stderr="fatal: not run here")

    monkeypatch.setattr(git_sync.subprocess, "run", _run)
    return started


@pytest.mark.parametrize(
    "url",
    [
        f"https://sync-user:{TOKEN}@git.example.com/owner/state.git",
        f"https://{TOKEN}@git.example.com/owner/state.git",
        f"http://sync-user:{TOKEN}@127.0.0.1:8080/owner/state.git",
    ],
    ids=["user-and-token", "token-only", "http"],
)
def test_an_http_url_with_a_credential_written_into_it_is_refused_without_showing_it(
    tmp_path, monkeypatch, url
):
    """git kept a credential written into Git remote URL in the working clone's .git/config,
    and passed it on its command line. The URL is refused before git runs, at every entry
    point, and the sentence names the URL without it."""
    started = _no_git(monkeypatch)
    p = _web_provider(url, tmp_path)
    says = (
        "Git Sync won't use a user name or token written into Git remote URL: git keeps it in "
        "the working clone's .git/config and shows it on its command line to anyone on this "
        f"machine. Set Git remote URL {ON_CARD} to the address without it "
        f"({git_sync._shown(url)}), and put the token in Access token — and the user name in "
        "User name, if your host wants one."
    )

    _refused_everywhere(p, says)

    assert TOKEN not in says
    assert started == [], "git ran for a URL with a credential written into it"
    assert not (tmp_path / "clone").exists()


def test_access_token_is_never_sent_over_plain_http_to_another_machine(tmp_path, monkeypatch):
    """Plain http carries the token unencrypted, so only an http remote on this machine is ever
    given it."""
    started = _no_git(monkeypatch)
    p = _web_provider("http://git.example.com/owner/state.git", tmp_path, token=ACCESS)

    _refused_everywhere(
        p,
        "Git Sync won't send Access token over http, which would carry it unencrypted to "
        f"git.example.com. Set Git remote URL {ON_CARD} to the remote's https address.",
    )

    assert started == []


@pytest.mark.parametrize("field", ["token", "username"])
def test_a_token_or_user_name_git_would_read_as_two_answers_is_refused(
    tmp_path, monkeypatch, field
):
    """git's credential protocol is a line per answer, so a line break inside one is refused
    before git runs, and the value is not said back."""
    started = _no_git(monkeypatch)
    settings = {"token": ACCESS, field: "pc-fixture-first-line\npc-fixture-second-line"}
    p = _web_provider("https://git.example.com/owner/state.git", tmp_path, **settings)
    label = {"token": "Access token", "username": "User name"}[field]

    _refused_everywhere(
        p,
        f"{label} {ON_CARD} has a line break or a NUL in it, which git would read as more than "
        "one answer. Enter it again, on its own.",
    )

    assert started == []


def _legacy_web_clone(host: _TokenHost, clone: Path, *, marked: bool) -> str:
    """A working clone an earlier Git Sync made from a URL with the token written into it: made,
    committed into under the transport's identity, pushed and caught up the way it did. Copies
    of the URL are put in its fetch record and its log, as an older git wrote them; git today
    writes it whole only into .git/config. Returns that URL."""
    leaky = f"http://{SIGN_IN}:{ACCESS}@127.0.0.1:{host.port}/sync.git"
    # No credential helper of any kind for this git, the system's included: one that worked
    # would be told to keep the token — in this machine's own keychain.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"}
    git = ["git", "-c", "credential.helper="]
    mark = ["-c", f"{MARK}=true"] if marked else []
    subprocess.run([*git, "clone", "-q", *mark, leaky, str(clone)], check=True, env=env,
                   capture_output=True, timeout=60)
    (clone / "machines" / "a").mkdir(parents=True)
    (clone / "machines" / "a" / "x.jsonl").write_bytes(b"1")
    identity = ["-c", "user.name=PersonalClaw Sync", "-c", "user.email=sync@personalclaw.local"]
    for step in (
        ["checkout", "-q", "-B", "main"],
        ["add", "-A"],
        [*identity, "commit", "-qm", "sync: 1 objects"],
        ["push", "-q", "origin", "main"],
        ["fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main"],
    ):
        subprocess.run([*git, "-C", str(clone), *step], check=True, env=env,
                       capture_output=True, timeout=60)
    git_dir = clone / ".git"
    (git_dir / "FETCH_HEAD").write_text(f"0000\t\tbranch 'main' of {leaky}\n", encoding="utf-8")
    with open(git_dir / "logs" / "HEAD", "a", encoding="utf-8") as fh:
        who = "PersonalClaw Sync <sync@personalclaw.local>"
        fh.write(f"0000 0000 {who} 0 +0000\tclone: from {leaky}\n")
    assert _files_holding(git_dir, ACCESS), "the clone this test is about holds no token"
    return leaky


@pytest.mark.parametrize("marked", [True, False], ids=["marked", "unmarked"])
def test_a_clone_made_from_a_url_with_the_token_in_it_is_rewritten_without_it(
    token_host, web_home, tmp_path, c_locale, marked
):
    """Git remote URL set again without the token, and the token put in Access token. The
    clone's origin still carried it, so the clone read as one of another remote and was cloned
    afresh — without a sign-in — while every copy of the token stayed where it was."""
    clone = tmp_path / "clone"
    _legacy_web_clone(token_host, clone, marked=marked)

    r = _web_provider(token_host.url, tmp_path, token=ACCESS, username=SIGN_IN).push(
        [SyncObject("machines/a/seq-0002/x.jsonl", b"2")]
    )

    assert r.outcome == "delivered", r.detail
    assert _git(str(clone), "config", "--get", "remote.origin.url").stdout.strip() == token_host.url
    assert _git(str(clone), "config", "--get", MARK).stdout.strip() == "true"
    assert _files_holding(clone / ".git", ACCESS) == []
    assert (clone / "machines" / "a" / "x.jsonl").read_bytes() == b"1", "the clone was replaced"
    assert web_home() == []
    tip = subprocess.run(["git", "-C", str(token_host.bare), "ls-tree", "-r", "--name-only",
                          "main"], check=True, capture_output=True, text=True).stdout.split()
    assert "machines/a/seq-0002/x.jsonl" in tip


# ── a password written into an ssh Git remote URL ────────────────────────────────────────

SSH_PASS = "pc-fixture-ssh-pass-7c2a"


def _password_refusal(url: str) -> str:
    if "://" in url:
        how = (
            "git keeps it in the working clone's .git/config and hands it to ssh on its command "
            "line, where anyone on this machine can read it, and ssh never signs in with it"
        )
    else:
        how = (
            "git keeps it in the working clone's .git/config, and it reads "
            "user:password@host:path as a host named after the user name, so it hands the "
            "password to ssh on its command line, where anyone on this machine can read it, to "
            "send to that host"
        )
    return (
        f"Git Sync won't use a password written into Git remote URL: {how}. Take the password out "
        f"of Git remote URL {ON_CARD}: leave the user name before the @, or nothing before the "
        f"host ({git_sync._shown(url)}), and sign in with your ssh key."
    )


@pytest.mark.parametrize(
    "url",
    [
        f"ssh://{SIGN_IN}:{SSH_PASS}@git.example.com/owner/state.git",
        f"git+ssh://{SIGN_IN}:{SSH_PASS}@git.example.com:2222/owner/state.git",
        f"ssh+git://:{SSH_PASS}@git.example.com/owner/state.git",
        f"{SIGN_IN}:{SSH_PASS}@git.example.com:owner/state.git",
        f"{SIGN_IN}:{SSH_PASS}@git.example.com:/srv/git/state.git",
        f":{SSH_PASS}@git.example.com:owner/state.git",
    ],
    ids=[
        "ssh", "git+ssh-with-a-port", "no-user-name", "scp-like", "scp-like-absolute-path",
        "scp-like-no-user-name",
    ],
)
def test_an_ssh_url_with_a_password_written_into_it_is_refused_without_showing_it(
    tmp_path, monkeypatch, caplog, url
):
    """git kept a password written into an ssh Git remote URL in the working clone's
    .git/config, and handed it to ssh on its command line, where anyone on this machine can read
    it: as part of the login name, which ssh never signs in with, or, written scp-like, as part
    of the path it asked a host named after the user name for. Refused before git runs, at every
    entry point, as an https URL with a credential in it is."""
    started = _no_git(monkeypatch)
    p = _provider(url, tmp_path)
    says = _password_refusal(url)

    with caplog.at_level(logging.DEBUG):
        _refused_everywhere(p, says)
        probed = p.test()

    assert SSH_PASS not in says
    for text in (repr(p), str(p), repr(probed.extra), *(r.getMessage() for r in caplog.records)):
        assert SSH_PASS not in text, text
    assert started == [], "git ran for an ssh URL with a password written into it"
    assert not (tmp_path / "clone").exists()


def _scp_like(bare) -> str:
    """The scp-like address, ``user@host:path``, that reaches the repository *bare* through the
    ssh stand-in (``ssh_url``), which runs what git asks the host for on this machine."""
    return f"{SIGN_IN}@example.invalid:{os.path.realpath(bare)}"


@pytest.mark.parametrize("written", ["ssh", "scp-like"])
def test_an_ssh_url_with_only_a_user_name_is_not_refused(
    remote, tmp_path, c_locale, written
):
    """A control: ``ssh://user@host/…``, or ``user@host:path``, is how an ssh remote names its
    login, and nothing in it is a credential."""
    if written == "ssh":
        url = remote.replace("ssh://", f"ssh://{SIGN_IN}@", 1)
    else:
        url = _scp_like(tmp_path / "remote.git")

    r = _provider(url, tmp_path).push([SyncObject("k", b"v")])

    assert r.outcome == "delivered", r.detail
    origin = _git(str(tmp_path / "clone"), "config", "--get", "remote.origin.url").stdout.strip()
    assert origin == url


@pytest.mark.parametrize("written", ["ssh", "scp-like"])
@pytest.mark.parametrize("marked", [True, False], ids=["marked", "unmarked"])
def test_a_clone_made_from_an_ssh_url_with_a_password_is_rewritten_without_it(
    remote, tmp_path, c_locale, marked, written
):
    """Git remote URL set again without the password. The clone's origin still carried it, so
    the clone read as one of another remote and was replaced, and a copy of the password in it
    was only as safe as the folder's removal. Now its origin is rewritten, every copy taken out,
    and the clone kept. An scp-like clone is made from the address without the password and then
    given it, as the clone of an owner who once wrote it there would hold it: git would take
    ``user:password@host:path`` for a host named ``user``, which nothing here answers."""
    clone = tmp_path / "clone"
    mark = ["-c", f"{MARK}=true"] if marked else []
    if written == "ssh":
        clean = remote.replace("ssh://", f"ssh://{SIGN_IN}@", 1)
        leaky = remote.replace("ssh://", f"ssh://{SIGN_IN}:{SSH_PASS}@", 1)
        made_from = leaky
    else:
        clean = _scp_like(tmp_path / "remote.git")
        leaky = clean.replace(f"{SIGN_IN}@", f"{SIGN_IN}:{SSH_PASS}@", 1)
        made_from = clean
    subprocess.run(["git", "clone", "-q", *mark, made_from, str(clone)], check=True,
                   capture_output=True, timeout=60)
    (clone / "machines" / "a").mkdir(parents=True)
    (clone / "machines" / "a" / "x.jsonl").write_bytes(b"1")
    identity = ["-c", "user.name=PersonalClaw Sync", "-c", "user.email=sync@personalclaw.local"]
    for step in (
        ["checkout", "-q", "-B", "main"],
        ["add", "-A"],
        [*identity, "commit", "-qm", "sync: 1 objects"],
        ["push", "-q", "origin", "main"],
        ["fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main"],
        ["remote", "set-url", "origin", leaky],
    ):
        subprocess.run(["git", "-C", str(clone), *step], check=True, capture_output=True,
                       timeout=60)
    git_dir = clone / ".git"
    (git_dir / "FETCH_HEAD").write_text(f"0000\t\tbranch 'main' of {leaky}\n", encoding="utf-8")
    with open(git_dir / "logs" / "HEAD", "a", encoding="utf-8") as fh:
        who = "PersonalClaw Sync <sync@personalclaw.local>"
        fh.write(f"0000 0000 {who} 0 +0000\tclone: from {leaky}\n")
    (git_dir / "pc-fixture-kept").write_text("only in the clone the transport found\n")
    assert _files_holding(git_dir, SSH_PASS), "the clone this test is about holds no password"

    r = _provider(clean, tmp_path).push([SyncObject("machines/a/seq-0002/x.jsonl", b"2")])

    assert r.outcome == "delivered", r.detail
    assert _git(str(clone), "config", "--get", "remote.origin.url").stdout.strip() == clean
    assert _git(str(clone), "config", "--get", MARK).stdout.strip() == "true"
    assert (git_dir / "pc-fixture-kept").exists(), "the clone was replaced, not rewritten"
    assert _files_holding(tmp_path, SSH_PASS) == []
    tip = subprocess.run(["git", "-C", str(tmp_path / "remote.git"), "ls-tree", "-r",
                          "--name-only", "main"], check=True, capture_output=True,
                         text=True).stdout.split()
    assert "machines/a/seq-0002/x.jsonl" in tip


# ── a Branch the remote doesn't have yet ─────────────────────────────────────────────────


def _remote_with_a_readme(tmp_path, ssh_url):
    """A remote whose only branch, ``main``, holds a README someone else wrote."""
    bare = tmp_path / "remote.git"
    url = ssh_url(_bare(bare))
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", url, str(seed)], check=True, capture_output=True, text=True)
    (seed / "README.md").write_text("# a project\n", encoding="utf-8")
    _git(str(seed), "add", "-A")
    _git(str(seed), "commit", "-m", "readme")
    _git(str(seed), "push", "-q", "origin", "HEAD:main")
    return bare, url


def _starts_empty(tmp_path, ssh_url) -> None:
    bare, url = _remote_with_a_readme(tmp_path, ssh_url)
    main = subprocess.run(["git", "-C", str(bare), "rev-parse", "main"], check=True,
                          capture_output=True, text=True).stdout
    p = GitSyncProvider(repo_url=url, local_clone=str(tmp_path / "clone"), branch="sync")

    listed = [ref.key for ref in p.list_remote()]
    pushed = p.push([SyncObject("machines/a/seq-0001/x.jsonl", b"1")])

    assert listed == [], "the default branch's files were listed as sync objects"
    assert pushed.outcome == "delivered", pushed.detail
    on_sync = subprocess.run(["git", "-C", str(bare), "ls-tree", "-r", "--name-only", "sync"],
                             check=True, capture_output=True, text=True).stdout.split()
    assert on_sync == ["machines/a/seq-0001/x.jsonl"]
    assert subprocess.run(["git", "-C", str(bare), "rev-parse", "main"], check=True,
                          capture_output=True, text=True).stdout == main


def test_a_branch_the_remote_lacks_starts_empty_not_from_its_default_branch(
    tmp_path, ssh_url, c_locale
):
    """``checkout -B sync`` started Branch from the branch the clone came checked out on: its
    README was listed as a sync object, and pushed on ``sync``."""
    _starts_empty(tmp_path, ssh_url)


def test_a_branch_the_remote_lacks_starts_empty_on_a_git_without_switch(
    tmp_path, ssh_url, monkeypatch, c_locale
):
    """A git before 2.23 has no ``git switch``: the orphan is made the older way, and the files
    the clone came with are taken out of its index and working tree just the same."""
    real_run = GitSyncProvider._run

    def _no_switch(self, args, check=True):
        if git_sync._subcommand(args) == "switch":
            return subprocess.CompletedProcess(
                ["git", *args], 1, stdout="",
                stderr="git: 'switch' is not a git command. See 'git --help'.",
            )
        return real_run(self, args, check=check)

    monkeypatch.setattr(GitSyncProvider, "_run", _no_switch)

    _starts_empty(tmp_path, ssh_url)


def test_starting_a_branch_on_a_git_without_switch_removes_nothing_through_a_link(
    tmp_path, ssh_url, monkeypatch, c_locale
):
    """The older way takes the files the clone came with out of its working tree by their paths
    in git's index. A folder on the way made a link since the checkout led the removal out of the
    clone: played by a link put in its place just before the files are taken out."""
    bare = tmp_path / "remote.git"
    url = ssh_url(_bare(bare))
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", url, str(seed)], check=True, capture_output=True, text=True)
    (seed / "docs").mkdir()
    (seed / "docs" / "guide.md").write_text("# a guide\n", encoding="utf-8")
    _git(str(seed), "add", "-A")
    _git(str(seed), "commit", "-m", "a guide")
    _git(str(seed), "push", "-q", "origin", "HEAD:main")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "guide.md").write_text("a file of this machine's\n", encoding="utf-8")
    clone = tmp_path / "clone"
    real_run = GitSyncProvider._run

    def _no_switch_and_a_link(self, args, check=True):
        if git_sync._subcommand(args) == "switch":
            return subprocess.CompletedProcess(
                ["git", *args], 1, stdout="",
                stderr="git: 'switch' is not a git command. See 'git --help'.",
            )
        ran = real_run(self, args, check=check)
        if git_sync._subcommand(args) == "read-tree":
            shutil.rmtree(clone / "docs")
            (clone / "docs").symlink_to(elsewhere, target_is_directory=True)
        return ran

    monkeypatch.setattr(GitSyncProvider, "_run", _no_switch_and_a_link)
    p = GitSyncProvider(repo_url=url, local_clone=str(clone), branch="sync")

    # What the listing says of the link is another test's; this one is about what was removed.
    with contextlib.suppress(KeysRefused, git_sync.GitSyncFailed):
        p.list_remote()

    assert (elsewhere / "guide.md").exists(), "a file outside the clone was removed through a link"


# ── the ownership look, once per state of the clone ──────────────────────────────────────


def _count_subcommand(monkeypatch, name: str) -> list[list[str]]:
    """Every ``git <name>`` the transport runs."""
    real_run = GitSyncProvider._run
    ran: list[list[str]] = []

    def _counting(self, args, check=True):
        if git_sync._subcommand(args) == name:
            ran.append(args)
        return real_run(self, args, check=check)

    monkeypatch.setattr(GitSyncProvider, "_run", _counting)
    return ran


def test_a_refused_folders_history_is_walked_once_until_a_commit_lands(
    tmp_path, ssh_url, monkeypatch, c_locale
):
    """A folder refused for the commits in it was walked (``git log --all``) on every call —
    every push, listing, read and probe, each cycle — however long its history."""
    folder, _own_bare, own = _owners_repository(tmp_path, ssh_url)
    walks = _count_subcommand(monkeypatch, "log")
    p = GitSyncProvider(repo_url=own, local_clone=str(folder))

    for _ in range(2):
        assert p.test().ok is False
        assert p.push([SyncObject("k", b"v")]).outcome == "permanent"
    assert len(walks) == 1, "the history was walked again with nothing changed"

    (folder / "later.md").write_text("another note\n", encoding="utf-8")
    _git(str(folder), "add", "later.md")
    _git(str(folder), "commit", "-m", "another note")

    assert p.test().ok is False
    assert len(walks) == 2, "a new commit wasn't looked at"


def test_the_ownership_look_sees_a_new_file_and_a_config_change_without_a_new_walk(
    remote, tmp_path, monkeypatch, c_locale
):
    """What the walk can't see — a file made by hand, a setting of the clone's own — is read
    on every look, so each changes the answer as it happens."""
    p = _provider(remote, tmp_path)
    assert p.push([SyncObject("machines/a/seq-0001/x.jsonl", b"1")]).outcome == "delivered"
    clone = tmp_path / "clone"
    _unmark(str(clone))  # as an older Git Sync made it
    walks = _count_subcommand(monkeypatch, "log")

    assert (p._standing(), p._standing()) == ("adopt", "adopt")
    (clone / "notes.md").write_text("made by hand\n", encoding="utf-8")
    assert p._standing() == "refuse"
    (clone / "notes.md").unlink()
    _git(str(clone), "config", "remote.origin.pushurl", "ssh://example.invalid/elsewhere.git")
    assert p._standing() == "replace"

    assert len(walks) == 1, walks


# ── a key that leaves the working clone ──────────────────────────────────────────────────
#
# git checks out a link another machine committed as a link, to any file on this machine. A key
# was joined onto the clone and opened as it came, following whatever was there: a link another
# machine committed read a file of this machine's as that machine's object, a registry that is a
# link had the swap written over the file it names, a folder on the way that is a link took this
# machine's objects anywhere its user may write, and a key with ``../`` in it read or wrote
# anywhere at all.

#: A file of this machine's, outside the working clone.
OUTSIDE = b"a file of this machine's, outside the working clone\n"
NOT_A_PATH = "is not a path a sync may use in the working clone"
LEADS_OUT = "leads out of the working clone through a link"
A_LINK = "is a link in the working clone, which Git Sync doesn't follow"
PEERS = "machines/b/seq-0001/"


@pytest.fixture
def outside(tmp_path):
    """A file of this machine's and a folder, beside the working clone, outside it."""
    (tmp_path / "outside.txt").write_bytes(OUTSIDE)
    (tmp_path / "elsewhere").mkdir()
    return tmp_path


def _a_peer_commits(remote: str, tmp_path, links: dict[str, Path]) -> None:
    """Another machine's commit on the remote's main, as its Git Sync makes one: one object of
    its own, and a link at each of *links*, to where it names."""
    peer = tmp_path / "peer"
    subprocess.run(["git", "clone", "-q", remote, str(peer)], check=True, capture_output=True,
                   timeout=60)
    (peer / "machines" / "b").mkdir(parents=True, exist_ok=True)
    (peer / "machines" / "b" / "own.jsonl").write_bytes(b"b's own\n")
    for rel, target in links.items():
        path = peer.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target)
    identity = ["-c", "user.name=PersonalClaw Sync", "-c", "user.email=sync@personalclaw.local"]
    for step in (
        ["checkout", "-q", "-B", "main"],
        ["add", "-A"],
        [*identity, "commit", "-qm", "sync: peer"],
        ["push", "-q", "origin", "main"],
    ):
        subprocess.run(["git", "-C", str(peer), *step], check=True, capture_output=True,
                       timeout=60)


def _cloned(p: GitSyncProvider) -> GitSyncProvider:
    """*p*, once its working clone is made and caught up: a read never makes one, so a read of a
    transport that hasn't listed yet reads nothing at all. Listed where no link is."""
    assert [ref.key for ref in p.list_remote("machines/b/own")] == ["machines/b/own.jsonl"]
    return p


def _keys_refused(call) -> dict[str, str]:
    """What *call* refused (``KeysRefused.refused``); fails when it refused nothing."""
    with pytest.raises(KeysRefused) as refusal:
        call()
    assert OUTSIDE.decode().strip() not in str(refusal.value)
    return refusal.value.refused


def test_a_link_another_machine_committed_is_not_read(remote, outside, c_locale):
    key = f"{PEERS}tasks/entities.jsonl"
    _a_peer_commits(remote, outside, {key: outside / "outside.txt"})
    p = _provider(remote, outside)

    assert _keys_refused(lambda: p.list_remote(PEERS)) == {key: A_LINK}
    assert (outside / "clone" / PEERS / "tasks" / "entities.jsonl").is_symlink(), (
        "the catch-up never checked the link out: the refusals are vacuous"
    )
    assert _keys_refused(lambda: p.pull([RemoteRef(key)])) == {key: LEADS_OUT}


def test_a_committed_link_to_a_file_inside_the_clone_is_not_followed_either(
    remote, outside, c_locale
):
    key = f"{PEERS}x.jsonl"
    _a_peer_commits(remote, outside, {key: Path("..") / ".." / "b" / "own.jsonl"})
    p = _cloned(_provider(remote, outside))

    assert _keys_refused(lambda: p.pull([RemoteRef(key)])) == {key: A_LINK}


def test_a_folder_on_the_way_that_is_a_committed_link_is_refused(remote, outside, c_locale):
    _a_peer_commits(remote, outside, {"machines/c": outside / "elsewhere"})
    p = _provider(remote, outside)

    assert _keys_refused(lambda: p.list_remote("machines/c/seq-0001/")) == {"machines/c": A_LINK}
    assert [r.key for r in p.list_remote("machines/b/")] == ["machines/b/own.jsonl"], (
        "a listing elsewhere is held up by the link"
    )


def test_a_push_through_a_committed_link_writes_nothing_outside(remote, outside, c_locale):
    key = "machines/a/seq-0001/x.jsonl"
    _a_peer_commits(remote, outside, {"machines/a": outside / "elsewhere"})
    p = _provider(remote, outside)
    before = _refs(outside / "remote.git")

    assert _keys_refused(lambda: p.push([SyncObject(key, b"mine")])) == {key: LEADS_OUT}
    assert list((outside / "elsewhere").iterdir()) == [], "an object was written through it"
    assert _refs(outside / "remote.git") == before, "a refused push reached the remote"


def test_a_registry_that_is_a_committed_link_is_neither_read_nor_swapped(
    remote, outside, c_locale
):
    _a_peer_commits(remote, outside, {"registry.json": outside / "outside.txt"})
    p = _provider(remote, outside)

    assert _keys_refused(lambda: p.cas_registry(_sha(OUTSIDE), b"{}")) == {
        "registry.json": LEADS_OUT
    }
    assert (outside / "outside.txt").read_bytes() == OUTSIDE, "the swap wrote over the file"


def test_a_key_that_climbs_out_of_the_clone_is_neither_read_nor_written(
    remote, outside, c_locale
):
    p = _provider(remote, outside)
    assert p.push([SyncObject("machines/a/seq-0001/x.jsonl", b"1")]).outcome == "delivered"

    assert _keys_refused(lambda: p.pull([RemoteRef("../outside.txt")])) == {
        "../outside.txt": NOT_A_PATH
    }
    assert _keys_refused(lambda: p.push([SyncObject("../planted.txt", b"x")])) == {
        "../planted.txt": NOT_A_PATH
    }
    assert not (outside / "planted.txt").exists()


def test_a_link_made_after_the_key_was_looked_at_is_not_followed(
    remote, outside, c_locale, monkeypatch
):
    """The key is looked at, then opened: a link put there in between is still not followed.
    Played by a look that finds nothing wrong with it."""
    read, written = f"{PEERS}x.jsonl", "machines/a/seq-0001/x.jsonl"
    links = {read: outside / "outside.txt", written: outside / "planted.txt"}
    _a_peer_commits(remote, outside, links)
    monkeypatch.setattr(
        GitSyncProvider, "_path", lambda self, k: (os.path.join(self._clone, *k.split("/")), "")
    )
    p = _cloned(_provider(remote, outside))

    assert _keys_refused(lambda: p.pull([RemoteRef(read)])) == {read: A_LINK}
    assert _keys_refused(lambda: p.push([SyncObject(written, b"mine")])) == {written: A_LINK}
    assert not (outside / "planted.txt").exists(), "the push wrote through the link"


def test_the_refusal_says_which_key_why_and_what_to_do(remote, outside, c_locale):
    key = f"{PEERS}tasks/entities.jsonl"
    _a_peer_commits(remote, outside, {key: outside / "outside.txt"})

    with pytest.raises(KeysRefused) as refusal:
        _cloned(_provider(remote, outside)).pull([RemoteRef(key)])

    assert str(refusal.value) == (
        f"Git Sync won't read a key in {outside / 'clone'}: {key} {LEADS_OUT}. Remove the link "
        "from the git remote's branch: git checks it out as a link, and Git Sync follows none "
        "out of the clone."
    )
