"""Git-sync transport tests — real ``git`` against a bare remote on this machine, no network.

Every case points the provider at a ``git init --bare`` repo under ``tmp_path`` and a fresh
working clone, so the tests exercise the real subprocess path with no credentials and no
network. The transport's git refuses a remote at a local path, so the remote is reached the way
an owner reaches one: over ssh, with an ssh command of the owner's own (``core.sshCommand`` in a
scratch ``HOME``), here a stand-in that runs the server's side of git on this machine.

Covers the insert-only push contract (verified by cloning the remote fresh), the empty-remote
first-machine case, list_remote's .git exclusion + prefix filtering + temp-file exclusion,
pull's drop-on-vanish, the registry compare-and-swap (present/absent/mismatch + round-trip),
the push-rejection → transient classification, two-machine convergence at the transport level,
and the reachability probe.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess

import pytest

import provider as git_sync
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


def test_a_push_turned_away_by_a_moved_remote_says_so_and_that_it_retries(remote, tmp_path, c_locale):
    """The first line of git's stderr ("To …/remote.git") used to be the whole message."""
    a = _provider(remote, tmp_path, name="a")
    a.push([SyncObject("base", b"0")])
    b = _provider(remote, tmp_path, name="b")
    b.list_remote()
    _git(str(tmp_path / "b"), "commit", "--allow-empty", "-m", "b-local")
    a.push([SyncObject("moved", b"1")])

    r = b.push([SyncObject("bnew", b"2")])

    assert r.outcome == "transient"
    assert r.detail.startswith(
        "Git Sync's push was turned away because the git remote has commits this machine "
        f"hasn't pulled — another machine pushed first. {RETRIES} Details: "
    ), r.detail


def test_a_push_the_remote_rules_refuse_names_the_branch(remote, tmp_path, c_locale):
    """A remote whose own hook declines the push. The hook's first stderr line used to be the
    whole message, and nothing said it wasn't a race: git's "[remote rejected]" reads as one."""
    bare = str(tmp_path / "remote.git")
    hook = os.path.join(bare, "hooks", "pre-receive")
    with open(hook, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\necho 'pushes to this branch are not accepted' >&2\nexit 1\n")
    os.chmod(hook, 0o755)
    # A machine-wide core.hooksPath (a system gitconfig can set one) would run its hooks in
    # place of the remote's own, and the push would land; the remote names its own directory.
    subprocess.run(["git", "-C", bare, "config", "core.hooksPath", os.path.join(bare, "hooks")],
                   check=True, capture_output=True, text=True)

    r = _provider(remote, tmp_path).push([SyncObject("k", b"v")])

    assert r.detail.startswith(
        "Git Sync couldn't push to the git remote: its rules don't let this machine push to "
        f"branch 'main'. Allow that on the remote, or set Branch {ON_CARD} to one that does."
    ), r.detail
    assert "Details: " in r.detail


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
