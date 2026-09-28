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
conflicts no rule settles, two-machine convergence at the transport level, and the reachability
probe.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import provider as git_sync
from provider import GitSyncProvider, create_provider
from personalclaw.sdk.sync import RemoteRef, SyncObject

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

    The owner's ssh command, in a scratch ``HOME`` both the tests' git and the transport's read,
    is a stand-in: it records the SSH agent socket and the planted secret it was handed, then
    runs the git command a server would. It runs that command with an environment of its own,
    as a login on the server gets one, so none of the client's git settings reach the server's
    git: a hook the remote runs is the remote's."""
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
    pushed = p.push([SyncObject("k", b"v")])
    assert (pushed.outcome, pushed.detail) == ("permanent", res.detail)
    assert p.list_remote() == [] and p.cas_registry(None, b"{}") is False
    assert not (tmp_path / "c").exists(), "git ran for a refused remote"


def test_a_refusal_git_reports_is_said_in_the_transports_words(remote, tmp_path):
    """A clone whose remote was changed to a local path after it was made (the clone's own
    `.git/config` is what git reads) is refused by git. The detail says why, and what to use
    instead, in PersonalClaw's words first; git's own follow as the detail."""
    p = _provider(remote, tmp_path)
    p.push([SyncObject("first.jsonl", b"one")])
    local = tmp_path / "elsewhere.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(local)], check=True,
                   capture_output=True, text=True)
    _git(str(tmp_path / "clone"), "remote", "set-url", "origin", str(local))

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
    monkeypatch.setenv("HOME", str(tmp_path))
    p = create_provider(None)
    assert p._repo_url == ""
    assert p._branch == "main"
    assert p._clone == str(tmp_path / ".personalclaw" / "sync" / "git-sync")
    assert p.test().ok is False


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
    with open(os.path.join(os.environ["HOME"], ".gitconfig"), "a", encoding="utf-8") as fh:
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
