"""Tests for the rsync-sync transport.

The suite is split by what each half can actually prove:

* **Behaviour is driven against a REAL ``rsync``** into a local target — push, list, pull,
  insert-only, and the registry compare-and-swap all run the real binary and assert on the
  bytes that landed. That is where the transport's data-integrity properties live, including
  the measured "rsync silently skips a same-length rewrite" defect.
* **The ssh leg is proved at the argv level**, because a live ssh host is not available in a
  test. Every injection vector and the exact ``-e`` string are asserted on the command the
  transport would run, with ``shell=False`` pinned — which is the part that would be a
  remote-code-execution bug if it were wrong.

What is NOT covered here is a real transfer to a real ssh host; see the README.
"""

import errno
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
from typing import Any

import pytest

import provider as provider_mod
from provider import (
    RsyncConfigError,
    RsyncSyncProvider,
    create_provider,
    validate_host,
    validate_remote_path,
)

from personalclaw.sdk.net import sentence_with_detail
from personalclaw.sdk.sync import RemoteRef, SyncObject

HAVE_RSYNC = shutil.which("rsync") is not None
needs_rsync = pytest.mark.skipif(not HAVE_RSYNC, reason="rsync binary not available")

#: Where a failure's sentence sends the user to fix a setting, and what a retried one adds.
ON_CARD = "on the Rsync Sync card in Settings → Providers"
RETRIES = "Sync tries again on its next run."


def _answering(code: int, stderr: str):
    """A ``subprocess.run`` stand-in: rsync ran, printed ``stderr`` and exited ``code``."""

    def _run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, code, stdout="", stderr=stderr)

    return _run


def _no_root(path: str, where: str) -> str:
    """What a sync root path that isn't there says — a folder this transport never creates, since
    a share that isn't mounted looks just like one — with the step for each likely cause: the
    share, a folder not made yet, and a mistyped setting."""
    return (
        f"The sync root path {path} doesn't exist {where}. If it's on a disk or share that isn't "
        f"mounted, mount it; if it's the folder you meant, create it (mkdir -p {path}); otherwise "
        f"set Sync root path {ON_CARD} to the right folder. Then sync again."
    )


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Never touch the real home or the real workspace."""
    home = tmp_path / "home"
    ws = tmp_path / "ws"
    home.mkdir()
    ws.mkdir()
    monkeypatch.setenv("PERSONALCLAW_HOME", str(home))
    monkeypatch.setenv("PERSONALCLAW_WORKSPACE", str(ws))
    yield home


@pytest.fixture
def target(tmp_path):
    """A local directory standing in for the sync root."""
    d = tmp_path / "target"
    d.mkdir()
    return d


@pytest.fixture
def local(tmp_path, target):
    """A provider rsyncing to a local path — real rsync, no ssh."""
    return RsyncSyncProvider(
        path=str(target), staging_dir=str(tmp_path / "staging"), timeout_secs=60
    )


def test_rsync_is_available_so_the_behaviour_suite_is_not_vacuous():
    """A missing rsync must read as a RED, not as a quiet row of skips.

    Every behaviour test below is ``skipif``-gated on the binary, and a suite of skips is
    indistinguishable from a suite of passes in a CI summary. This test is the vacuity floor
    for the whole file.
    """
    assert HAVE_RSYNC, (
        "rsync is not installed, so every behavioural assertion in this file was skipped — "
        "install rsync in the test environment rather than trusting this suite"
    )


# ── 1. injection resistance (the ssh leg, proved on the command) ──────────────────────


class TestArgumentInjection:
    @pytest.mark.parametrize(
        "host",
        [
            "-e/bin/sh",
            "--rsh=/bin/sh",
            "-oProxyCommand=curl evil.example.com",
            # `id`, and deliberately not a root delete: what this case asserts is that the
            # `;` is refused, so the trailing command is interchangeable — as the two
            # entries right below it already show. A literal recursive root `rm` in a file
            # that also holds an execution sink is a non-overridable DANGEROUS finding for
            # core's scanner (rule `destructive_root`) and would make this bundle
            # UNINSTALLABLE. The scanner reads comments too, so don't spell it out here.
            "host; id",
            "host`whoami`",
            "host$(id)",
            "host with space",
            "host'quote",
            'host"quote',
            "host\nsecond",
            "host:module",
            "host::daemon",
        ],
    )
    def test_a_dangerous_host_is_refused(self, host):
        with pytest.raises(RsyncConfigError):
            validate_host(host)

    @pytest.mark.parametrize(
        "path",
        [
            "-rf",
            "--delete",
            "/srv/sync:evil",
            "host:/srv/sync",
            # `id`, and deliberately not a root delete: this case asserts the embedded
            # NEWLINE is refused as a control character, so the trailing command is
            # interchangeable. Same scanner reason as the host list above.
            "/srv/sync\nid",
            "/srv/\x00sync",
        ],
    )
    def test_a_dangerous_path_is_refused(self, path):
        with pytest.raises(RsyncConfigError):
            validate_remote_path(path)

    def test_safe_values_are_accepted(self):
        """Vacuity floor: the validators must not simply reject everything."""
        assert validate_host("nas.local") == "nas.local"
        assert validate_host("backup@nas.local") == "backup@nas.local"
        assert validate_host("192.168.1.10") == "192.168.1.10"
        assert validate_host("") == ""
        assert validate_remote_path("/srv/personalclaw-sync") == "/srv/personalclaw-sync"

    def test_a_refused_setting_leaves_the_provider_unconfigured_and_inert(self, tmp_path):
        """A bad value must not raise out of the factory (the Store has to render it) and
        must not let a single command run either."""
        p = create_provider(
            {"host": "-e/bin/sh", "path": "/srv/sync", "staging_dir": str(tmp_path)}
        )
        assert p.configured is False
        assert "may not begin with '-'" in p._unconfigured_detail()
        assert p.push([SyncObject(key="k", data=b"v")]).outcome == "transient"
        assert p.list_remote() == []
        assert p.pull([RemoteRef(key="k")]) == []
        assert p.cas_registry(None, b"{}") is False
        assert p.test().ok is False

    def test_a_refused_host_never_reaches_a_subprocess(self, tmp_path, monkeypatch):
        calls: list[Any] = []
        monkeypatch.setattr(
            provider_mod.subprocess, "run", lambda *a, **k: calls.append((a, k))
        )
        # `id`, and deliberately not a root delete: the assertion is that a host containing
        # `;` never reaches a subprocess, not what follows the `;`. Same scanner reason as
        # the refusal lists above.
        p = create_provider(
            {"host": "host; id", "path": "/srv/sync", "staging_dir": str(tmp_path)}
        )
        p.push([SyncObject(key="k", data=b"v")])
        p.list_remote()
        p.test()
        assert calls == [], "a rejected host still reached a subprocess"

    def test_an_ssh_key_path_with_a_space_is_refused(self, tmp_path):
        """The identity path is embedded in the single string rsync hands to the remote
        shell, and rsync splits that string itself — so whitespace is an injection point."""
        p = create_provider(
            {
                "host": "nas.local",
                "path": "/srv/sync",
                "ssh_key": "/home/u/my key -oProxyCommand=x",
                "staging_dir": str(tmp_path),
            }
        )
        assert p.configured is False
        assert "no spaces or quotes" in p._unconfigured_detail()


class TestCommandConstruction:
    def _capture(self, monkeypatch, provider) -> list[list[str]]:
        seen: list[list[str]] = []
        kwargs_seen: list[dict] = []

        def fake_run(argv, **kw):
            seen.append(list(argv))
            kwargs_seen.append(kw)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(provider_mod.subprocess, "run", fake_run)
        provider._captured_kwargs = kwargs_seen  # type: ignore[attr-defined]
        return seen

    def test_every_invocation_is_argv_without_a_shell(self, tmp_path, monkeypatch):
        p = create_provider(
            {"host": "nas.local", "path": "/srv/sync", "staging_dir": str(tmp_path)}
        )
        seen = self._capture(monkeypatch, p)
        p.push([SyncObject(key="k", data=b"v")])
        p.list_remote()
        p.pull([RemoteRef(key="k")])
        p.test()
        assert seen, "no command was built — the test proved nothing"
        for argv in seen:
            assert isinstance(argv, list), "a command was built as a string (shell risk)"
            assert argv[0] == "rsync"
            assert "--" in argv, "a command omitted the end-of-options separator"
        for kw in p._captured_kwargs:  # type: ignore[attr-defined]
            assert kw.get("shell") is False, "shell=True was used"
            assert kw.get("timeout"), "an unbounded command could wedge the sync job"

    def test_rsync_gets_the_ssh_agent_and_none_of_the_gateways_secrets(
        self, tmp_path, monkeypatch
    ):
        """ssh signs in to the owner's host through their SSH agent, so rsync carries the agent's
        socket. It carries nothing else of the gateway's, whose environment holds every secret
        saved in PersonalClaw."""
        monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/example-agent.sock")
        monkeypatch.setenv("EXAMPLE_SERVICE_API_TOKEN", "example-secret-token-4d1e9c")
        p = create_provider(
            {"host": "nas.example.invalid", "path": "/srv/sync", "staging_dir": str(tmp_path)}
        )
        seen = self._capture(monkeypatch, p)
        p.list_remote()
        p.test()
        assert seen, "no command was built — the test proved nothing"
        for kw in p._captured_kwargs:  # type: ignore[attr-defined]
            env = kw.get("env")
            assert env is not None, "rsync inherited the gateway's whole environment"
            assert env.get("SSH_AUTH_SOCK") == "/tmp/example-agent.sock", sorted(env)
            assert "EXAMPLE_SERVICE_API_TOKEN" not in env

    def test_path_operands_come_after_the_end_of_options_separator(
        self, tmp_path, monkeypatch
    ):
        p = create_provider(
            {"host": "nas.local", "path": "/srv/sync", "staging_dir": str(tmp_path)}
        )
        seen = self._capture(monkeypatch, p)
        p.list_remote()
        argv = seen[0]
        sep = argv.index("--")
        # Everything before the separator is a flag or a flag's VALUE (``-e`` takes one);
        # everything after is a path operand. The point of ``--`` is that an operand can
        # never be re-read as an option, however it begins.
        head = argv[1:sep]
        for i, a in enumerate(head):
            if i > 0 and head[i - 1] == "-e":
                continue  # the remote-shell string, not a flag
            assert a.startswith("-"), f"{a!r} appears before -- but is not an option"
        assert argv[sep + 1] == "nas.local:/srv/sync/"
        assert argv[sep + 2:] == [] or not argv[sep + 2].startswith("-")

    def test_the_remote_shell_sets_batchmode_and_does_not_weaken_host_key_checking(
        self, tmp_path, monkeypatch
    ):
        p = create_provider(
            {
                "host": "backup@nas.local",
                "path": "/srv/sync",
                "port": 2222,
                "ssh_key": "~/.ssh/id_sync",
                "staging_dir": str(tmp_path),
            }
        )
        seen = self._capture(monkeypatch, p)
        p.list_remote()
        argv = seen[0]
        rsh = argv[argv.index("-e") + 1]
        assert "BatchMode=yes" in rsh, "without BatchMode a key prompt hangs the sync job"
        assert "-p 2222" in rsh
        assert "-i " in rsh and "id_sync" in rsh
        # The security floor: never accept an unknown host key silently.
        for banned in ("StrictHostKeyChecking=no", "StrictHostKeyChecking=accept-new",
                       "UserKnownHostsFile=/dev/null"):
            assert banned not in rsh, f"{banned} would open a man-in-the-middle"

    def test_no_remote_shell_argument_for_a_local_target(self, tmp_path, monkeypatch):
        p = create_provider({"path": str(tmp_path / "t"), "staging_dir": str(tmp_path / "s")})
        seen = self._capture(monkeypatch, p)
        p.list_remote()
        assert "-e" not in seen[0]

    def test_default_port_is_not_passed_explicitly(self, tmp_path, monkeypatch):
        p = create_provider(
            {"host": "nas.local", "path": "/srv/sync", "port": 22, "staging_dir": str(tmp_path)}
        )
        seen = self._capture(monkeypatch, p)
        p.list_remote()
        rsh = seen[0][seen[0].index("-e") + 1]
        assert "-p" not in rsh

    def test_a_remote_path_is_not_expanded_against_the_local_environment(self, tmp_path):
        """``~`` and ``$VARS`` in a REMOTE path must stay literal — expanding them here
        would silently point at a directory on the wrong machine."""
        p = create_provider(
            {"host": "nas.local", "path": "~/sync", "staging_dir": str(tmp_path)}
        )
        assert p._path == "~/sync"
        assert p._target() == "nas.local:~/sync/"
        # …but a LOCAL path is expanded, because there is only one machine involved.
        local = create_provider({"path": "~/pcsync", "staging_dir": str(tmp_path)})
        assert local._path == os.path.expanduser("~/pcsync")


# ── 2. behaviour, driven against a real rsync ────────────────────────────────────────


@needs_rsync
class TestRoundTrip:
    def test_push_list_pull_round_trips_bytes_exactly(self, local, target):
        objects = [
            SyncObject(key="machines/A/seq-0001/tasks/tasks.jsonl", data=b'{"id":"t1"}\n'),
            SyncObject(key="machines/A/seq-0001/memory/memory.jsonl", data=b'{"id":"m1"}\n'),
            SyncObject(key="machines/B/seq-0003/tasks/tasks.jsonl", data=b"\x00\x01\x02binary"),
        ]
        # VACUITY FLOOR: a round-trip assertion over zero objects passes forever.
        assert len(objects) >= 3

        res = local.push(objects)
        assert res.outcome == "delivered", res.detail
        assert res.pushed == 3, res.detail
        assert res.skipped == 0

        # The bytes really are on the target, at the right paths.
        for o in objects:
            assert (target / o.key).read_bytes() == o.data

        refs = local.list_remote()
        assert {r.key for r in refs} == {o.key for o in objects}
        assert all(r.size > 0 for r in refs)
        assert all(r.fingerprint for r in refs), "every ref needs a change fingerprint"

        pulled = local.pull(refs)
        got = {o.key: o.data for o in pulled}
        assert len(got) == 3
        for o in objects:
            assert got[o.key] == o.data, f"{o.key} did not round-trip byte-for-byte"

    def test_list_remote_reports_no_directories(self, local, target):
        local.push([SyncObject(key="machines/A/seq-0001/tasks.jsonl", data=b"x")])
        refs = local.list_remote()
        assert [r.key for r in refs] == ["machines/A/seq-0001/tasks.jsonl"]
        # A directory counted as an object would inflate every push and pull count.
        assert not any(r.key.endswith("/") for r in refs)
        assert "machines" not in {r.key for r in refs}

    def test_list_remote_honours_a_prefix(self, local):
        local.push(
            [
                SyncObject(key="machines/A/x", data=b"a"),
                SyncObject(key="machines/B/y", data=b"b"),
            ]
        )
        assert [r.key for r in local.list_remote("machines/A/")] == ["machines/A/x"]

    def test_list_remote_on_an_empty_target_is_empty_not_an_error(self, local):
        assert local.list_remote() == []

    def test_a_missing_ref_is_dropped_not_raised(self, local):
        local.push([SyncObject(key="present", data=b"v")])
        out = local.pull([RemoteRef(key="present"), RemoteRef(key="vanished")])
        assert [o.key for o in out] == ["present"]

    def test_pull_is_incremental_through_a_persistent_mirror(self, local, target):
        local.push([SyncObject(key="a", data=b"one")])
        assert local.pull([RemoteRef(key="a")])[0].data == b"one"
        # The mirror persists between pulls — that is what makes rsync worth using here.
        assert os.path.isdir(local._mirror)
        assert (pathlib.Path(local._mirror) / "a").read_bytes() == b"one"

    def test_a_traversing_key_cannot_read_outside_the_mirror(self, local, tmp_path):
        """A ref key is remote-supplied; it must not be able to name a local file."""
        local.push([SyncObject(key="a", data=b"one")])
        secret = tmp_path / "outside.txt"
        secret.write_bytes(b"NOT-A-SHARD")
        out = local.pull([RemoteRef(key="../../outside.txt"), RemoteRef(key="a")])
        assert [o.key for o in out] == ["a"]
        assert all(b"NOT-A-SHARD" not in o.data for o in out)


@needs_rsync
class TestInsertOnly:
    def test_a_retried_push_is_skipped_not_overwritten(self, local, target):
        key = "machines/A/seq-0001/tasks.jsonl"
        assert local.push([SyncObject(key=key, data=b"original")]).pushed == 1

        again = local.push([SyncObject(key=key, data=b"tampered")])
        assert again.outcome == "delivered"
        assert again.pushed == 0, "insert-only was violated"
        assert again.skipped == 1
        assert (target / key).read_bytes() == b"original"

    def test_a_mixed_push_counts_only_the_new_objects(self, local):
        assert local.push([SyncObject(key="a", data=b"1")]).pushed == 1
        res = local.push(
            [SyncObject(key="a", data=b"1"), SyncObject(key="b", data=b"2")]
        )
        assert (res.pushed, res.skipped) == (1, 1), res.detail

    def test_an_empty_push_is_a_no_op(self, local):
        res = local.push([])
        assert res.outcome == "delivered" and res.pushed == 0


@needs_rsync
class TestRegistryCas:
    def test_create_only_succeeds_once_then_loses(self, local, target):
        assert local.cas_registry(None, b'{"machines":{}}') is True
        assert (target / "registry.json").read_bytes() == b'{"machines":{}}'
        # A second machine that also believes the registry is absent must LOSE.
        assert local.cas_registry(None, b'{"machines":{"B":1}}') is False
        assert (target / "registry.json").read_bytes() == b'{"machines":{}}'

    def test_swap_succeeds_on_the_expected_sha(self, local, target):
        first = b'{"machines":{"A":1}}'
        assert local.cas_registry(None, first) is True
        second = b'{"machines":{"A":1,"B":1}}'
        assert local.cas_registry(hashlib.sha256(first).hexdigest(), second) is True
        assert (target / "registry.json").read_bytes() == second

    def test_swap_refuses_on_a_stale_sha_and_does_not_clobber(self, local, target):
        first = b'{"machines":{"A":1}}'
        assert local.cas_registry(None, first) is True
        stale = hashlib.sha256(b"bytes the target never held").hexdigest()
        assert local.cas_registry(stale, b'{"machines":{"C":1}}') is False
        assert (target / "registry.json").read_bytes() == first

    def test_swap_refuses_when_the_registry_is_absent(self, local):
        assert local.cas_registry(hashlib.sha256(b"{}").hexdigest(), b"{}") is False

    def test_a_same_length_update_with_an_identical_mtime_still_lands(self, local, target, monkeypatch):
        """🔴 THE MEASURED DEFECT THIS TRANSPORT MOST NEEDED TO FIX.

        rsync's quick check compares size + mtime, so a rewrite that keeps the same byte
        length and the same mtime is not transferred at all — and rsync exits 0, so the write
        LOOKS successful. A registry going from ``{"seq":19}`` to ``{"seq":20}`` is exactly
        that shape: same length, different bytes.

        **The mtimes are FORCED equal rather than left to the clock.** A first version of
        this test just wrote twice in quick succession and passed even with ``--ignore-times``
        removed, because the two staging files happened to land in different integer seconds —
        the rail was real only when the race happened to align, which is no rail at all.
        Pinning both mtimes makes the quick-check condition hold every run.
        """
        pinned = 1_700_000_000  # any fixed epoch second, on both sides of the comparison
        first = b'{"seq":19}'
        second = b'{"seq":20}'
        assert len(second) == len(first), "the test must exercise a SAME-LENGTH change"

        assert local.cas_registry(None, first) is True
        reg = target / "registry.json"
        os.utime(reg, (pinned, pinned))

        real_run = provider_mod.RsyncSyncProvider._run

        def run_with_pinned_source_mtime(self, args):
            # Force every staged source file to the SAME mtime the target already has, so
            # size+mtime are identical and only --ignore-times can defeat the quick check.
            operands = args[args.index("--") + 1 :] if "--" in args else []
            for operand in operands:
                root = operand.rstrip("/")
                if os.path.isdir(root):
                    for dirpath, _dirs, files in os.walk(root):
                        for fn in files:
                            os.utime(os.path.join(dirpath, fn), (pinned, pinned))
            return real_run(self, args)

        monkeypatch.setattr(
            provider_mod.RsyncSyncProvider, "_run", run_with_pinned_source_mtime
        )
        # Sanity floor: the condition the defect needs must actually hold now.
        assert reg.stat().st_mtime == pinned

        assert local.cas_registry(hashlib.sha256(first).hexdigest(), second) is True
        assert reg.read_bytes() == second, (
            "rsync silently skipped a same-length, same-mtime registry rewrite"
        )

    def test_a_write_that_does_not_land_reports_false(self, local, monkeypatch):
        """The read-back verify is the safety net: reporting success for a write that did
        not land would silently discard a peer's registration.

        A ``False`` is the safe direction — core's CAS loop re-pulls, re-merges peers and
        retries — so the transport must bias here and never the other way.
        """
        first = b'{"machines":{"A":1}}'
        assert local.cas_registry(None, first) is True
        # Simulate rsync exiting 0 while transferring nothing (the quick-check defect).
        monkeypatch.setattr(
            provider_mod.RsyncSyncProvider, "_write_registry", lambda self, data: True
        )
        assert local.cas_registry(hashlib.sha256(first).hexdigest(), b'{"machines":{"Z":1}}') is False

    def test_registry_key_is_the_shared_plaintext_routing_key(self):
        from personalclaw.sdk.sync import is_routing_key

        assert is_routing_key(provider_mod._REGISTRY_KEY)


@needs_rsync
class TestConnection:
    def test_test_reports_a_reachable_local_root(self, local, target):
        r = local.test()
        assert r.ok is True
        assert str(target) in r.detail
        assert r.extra.get("local") is True

    def test_test_reports_a_missing_root(self, tmp_path):
        """rsync's "change_dir … failed: No such file or directory" used to follow a bare
        "unreachable"; now the probe says the folder isn't there, and to mount it or make it —
        a folder this transport never makes itself."""
        root = tmp_path / "does-not-exist"
        p = RsyncSyncProvider(path=str(root), staging_dir=str(tmp_path / "s"))
        r = p.test()
        assert r.ok is False
        assert r.detail.startswith(f"{_no_root(str(root), 'on this machine')} Details: "), r.detail


class TestFailureHandling:
    def test_a_timeout_is_transient_not_permanent(self, tmp_path, monkeypatch):
        """A hung transfer must not make the outbox discard the objects. "rsync timed out after
        300s" used to be the whole message, with nothing to check or change."""

        def slow(*a, **k):
            raise subprocess.TimeoutExpired(cmd="rsync", timeout=300)

        monkeypatch.setattr(provider_mod.subprocess, "run", slow)
        root = tmp_path / "t"
        p = create_provider({"path": str(root), "staging_dir": str(tmp_path / "s")})
        res = p.push([SyncObject(key="k", data=b"v")])
        says = (
            "rsync didn't finish within 300 seconds. If it keeps happening, check that the disk "
            f"holding the sync root path {root} is connected and responding, or raise Command "
            f"timeout {ON_CARD}."
        )
        words = "Details: Command 'rsync' timed out after 300 seconds"
        assert res.outcome == "transient"
        assert res.detail == f"{says} {RETRIES} {words}"
        # A listing and a registry swap raise it: they answered empty, and False, which read as
        # a target with nothing on it and a swap another machine won.
        for read in (p.list_remote, lambda: p.cas_registry(None, b"{}")):
            with pytest.raises(provider_mod.RsyncFailed) as caught:
                read()
            assert str(caught.value) == f"{says} {words}"
        assert p.test().ok is False
        assert p.test().detail == f"{says} {words}"

    def test_a_timeout_reaching_a_host_names_the_login_to_try(self, tmp_path, monkeypatch):
        def slow(*a, **k):
            raise subprocess.TimeoutExpired(cmd="rsync", timeout=300)

        monkeypatch.setattr(provider_mod.subprocess, "run", slow)
        p = create_provider({**REMOTE, "staging_dir": str(tmp_path)})

        res = p.push([SyncObject(key="k", data=b"v")])

        assert res.outcome == "transient"
        assert res.detail == (
            f"rsync didn't finish within 300 seconds. If it keeps happening, check that {SSH} "
            "logs in from a terminal here — a host that doesn't answer keeps rsync waiting — or, "
            f"for a slow link, raise Command timeout {ON_CARD}. {RETRIES} Details: Command "
            "'rsync' timed out after 300 seconds"
        )

    def test_a_missing_rsync_binary_is_permanent(self, tmp_path):
        """Driven through a real exec of a binary that cannot exist. "cannot run rsync:
        [Errno 2] No such file or directory: '…'" used to be the whole message."""
        p = RsyncSyncProvider(
            path=str(tmp_path / "t"),
            staging_dir=str(tmp_path / "s"),
            rsync_bin="/nonexistent/pc-fixture-rsync",
        )
        res = p.push([SyncObject(key="k", data=b"v")])
        assert res.outcome == "permanent"
        assert res.detail == (
            "Rsync Sync runs the rsync command, and PersonalClaw couldn't start it on this "
            "machine: it isn't installed, isn't on the PATH PersonalClaw runs with, or isn't "
            "executable. Install rsync where PersonalClaw can run it. Details: [Errno 2] No such "
            "file or directory: '/nonexistent/pc-fixture-rsync'"
        )
        assert p.test().detail == res.detail

    @pytest.mark.parametrize(
        "code,expected",
        [(1, "permanent"), (2, "permanent"), (4, "permanent"),
         (10, "transient"), (23, "transient"), (30, "transient"), (255, "transient")],
    )
    def test_exit_codes_map_to_the_right_verdict(self, code, expected):
        assert provider_mod._outcome_for_rsync(code) == expected

    def test_a_failed_push_reports_the_rsync_error_line(self, tmp_path, monkeypatch):
        """An error this transport has no words of its own for still says where to look, and
        keeps rsync's line after it — "rsync exit 23: <that line>" used to be the message."""
        monkeypatch.setattr(
            provider_mod.subprocess,
            "run",
            _answering(23, "rsync: link_stat failed: No such file\n"),
        )
        root = tmp_path / "t"
        p = create_provider({"path": str(root), "staging_dir": str(tmp_path / "s")})
        res = p.push([SyncObject(key="k", data=b"v")])
        assert res.outcome == "transient"
        assert res.detail == (
            f"rsync couldn't sync with the sync root path {root} (rsync exit 23). Check Sync root "
            f"path {ON_CARD}. {RETRIES} Details: rsync: link_stat failed: No such file"
        )


# ── the local working directory ──────────────────────────────────────────────────────
#
# The local half of a sync — the staging trees and the mirror under Local working directory —
# runs before rsync does. A folder there that refused a write escaped push, pull and cas_registry
# as a bare OSError, and the sync cycle relays what escapes as it is: "push: [Errno 13]
# Permission denied: '…'", naming neither the setting nor what to do.


def _blocked(tmp_path: pathlib.Path) -> tuple[RsyncSyncProvider, pathlib.Path, pathlib.Path]:
    """A provider whose Local working directory is a file — a real refusal, on any machine and
    as any user. Returns it, that path, and its sync root."""
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("a file where the working directory should be", encoding="utf-8")
    root = tmp_path / "target"
    root.mkdir(exist_ok=True)
    provider = RsyncSyncProvider(path=str(root), staging_dir=str(blocker), timeout_secs=60)
    return provider, blocker, root


def _in_the_way(workdir: pathlib.Path) -> str:
    return (
        f"Rsync Sync couldn't create its local working directory {workdir}, or a folder in it, "
        f"because a file is in the way. Move that file, or set Local working directory {ON_CARD} "
        "to another folder."
    )


class TestTheLocalWorkingDirectory:
    def test_a_push_it_refuses_says_what_to_set_and_that_it_retries(self, tmp_path):
        p, blocker, _root = _blocked(tmp_path)

        res = p.push([SyncObject(key="k", data=b"v")])

        assert res.outcome == "transient"
        assert res.detail.startswith(f"{_in_the_way(blocker)} {RETRIES} Details: "), res.detail
        assert str(blocker) in res.detail.split("Details: ", 1)[1]

    @pytest.mark.parametrize(
        ("code", "says"),
        [
            (
                errno.EACCES,
                lambda w: f"Rsync Sync isn't allowed to write to its local working directory {w}. "
                "Fix that folder's permissions, or set Local working directory "
                f"{ON_CARD} to a folder PersonalClaw can write to.",
            ),
            (
                errno.ENOSPC,
                lambda w: f"The disk holding Rsync Sync's local working directory {w} is full. "
                f"Free some space on it, or set Local working directory {ON_CARD} to a folder on "
                "another disk.",
            ),
            (
                errno.EROFS,
                lambda w: f"Rsync Sync's local working directory {w} is on a read-only disk. Set "
                f"Local working directory {ON_CARD} to a folder PersonalClaw can write to.",
            ),
        ],
        ids=["permission", "disk-full", "read-only"],
    )
    def test_a_folder_that_refuses_a_write_says_why(self, tmp_path, monkeypatch, code, says):
        workdir = tmp_path / "work"

        def _refuses(path, *args, **kwargs):
            raise OSError(code, os.strerror(code), str(path))

        monkeypatch.setattr(provider_mod.os, "makedirs", _refuses)
        p = RsyncSyncProvider(path=str(tmp_path / "t"), staging_dir=str(workdir), timeout_secs=60)

        res = p.push([SyncObject(key="k", data=b"v")])

        assert res.outcome == "transient"
        assert res.detail == (
            f"{says(workdir)} {RETRIES} Details: [Errno {code}] {os.strerror(code)}: '{workdir}'"
        )

    def test_a_pull_raises_what_to_set_rather_than_the_bare_error(self, tmp_path):
        """``pull`` has no outcome to carry a sentence, so it raises one — chained to the
        filesystem's own error — for the cycle to report."""
        p, blocker, _root = _blocked(tmp_path)

        with pytest.raises(OSError) as caught:
            p.pull([RemoteRef("registry.json")])

        assert str(caught.value).startswith(f"{_in_the_way(blocker)} Details: "), caught.value
        assert isinstance(caught.value.__cause__, OSError)

    @pytest.mark.parametrize(
        "expected", [None, hashlib.sha256(b"{}").hexdigest()], ids=["expect-absent", "expect-sha"]
    )
    def test_a_registry_swap_raises_rather_than_reading_as_a_lost_race(self, tmp_path, expected):
        """A ``False`` would send core round its compare-and-swap loop to no purpose, then report
        the swap lost to another machine."""
        p, blocker, _root = _blocked(tmp_path)

        with pytest.raises(OSError) as caught:
            p.cas_registry(expected, b"{}")

        assert str(caught.value).startswith(f"{_in_the_way(blocker)} Details: "), caught.value

    @needs_rsync
    def test_the_sync_cycle_reports_it_in_those_words(self, isolated_home, tmp_path, monkeypatch):
        p, blocker, root = _blocked(tmp_path)
        (root / "registry.json").write_bytes(b"{}")  # a registry for the cycle's read to fetch

        report = _run_cycle(p, isolated_home, monkeypatch, encrypt="off")

        assert report.ok is False
        assert report.error.startswith(f"pull: {_in_the_way(blocker)} Details: "), report.error


# ── what a failure says ─────────────────────────────────────────────────────────────
#
# Each failure below used to arrive as "rsync exit <code>: <rsync's first line>" — or, from the
# probe, "<target> unreachable (rsync exit <code>): …" — which names neither what is wrong nor
# what to do. The ssh leg is proved on ssh's and rsync's own words, since no live host exists
# in a test; each sample is the text they print for that trouble.

REMOTE = {
    "host": "backup@nas.example.com",
    "path": "/srv/sync",
    "port": 2222,
    "ssh_key": "/keys/id_sync",
}
SSH = "ssh -p 2222 -i /keys/id_sync backup@nas.example.com"

_REFUSALS = [
    (
        255,
        "Host key verification failed.\r\nrsync: connection unexpectedly closed (0 bytes "
        "received so far) [sender]\n",
        f"SSH on this machine doesn't trust nas.example.com's host key yet. Run {SSH} once from "
        "a terminal here to accept it.",
    ),
    (
        255,
        "@@@@@@@@@@@@@@@@\n@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @\n"
        "@@@@@@@@@@@@@@@@\nHost key verification failed.\r\n",
        "SSH refused nas.example.com because its host key has changed since this machine last "
        "trusted it. Only if you know why it changed, remove its old key from this machine's "
        f"known_hosts file, then run {SSH} once from a terminal here to accept the new one.",
    ),
    (
        255,
        "backup@nas.example.com: Permission denied (publickey,password).\r\nrsync: connection "
        "unexpectedly closed (0 bytes received so far) [sender]\n",
        f"nas.example.com turned down this machine's SSH login. Check that {SSH} logs in from a "
        "terminal here without asking for anything, or set SSH identity file "
        f"{ON_CARD} to a key nas.example.com accepts.",
    ),
    (
        255,
        "ssh: Could not resolve hostname nas.example.com: nodename nor servname provided, or "
        "not known\r\nrsync: connection unexpectedly closed (0 bytes received so far) [sender]\n",
        "nas.example.com couldn't be reached from this machine. Check that it is online, and that "
        f"SSH host and SSH port {ON_CARD} are right.",
    ),
    (
        127,
        "bash: rsync: command not found\nrsync: connection unexpectedly closed (0 bytes received "
        "so far) [sender]\nrsync error: remote command not found (code 127)\n",
        "nas.example.com couldn't run rsync — it isn't installed there, or isn't on the PATH its "
        "SSH logins get. Install rsync on nas.example.com.",
    ),
    (
        2,
        "protocol version mismatch -- is your shell clean?\n(see the rsync manpage for an "
        "explanation)\nrsync error: protocol incompatibility (code 2)\n",
        "Something on nas.example.com prints text when rsync logs in over SSH — a login message, "
        "or output from a shell startup file — and it garbles rsync's connection. Stop that "
        "output for non-interactive logins on nas.example.com.",
    ),
    (
        11,
        'rsync: [Receiver] mkdir "/srv/sync" failed: No such file or directory (2)\nrsync error: '
        "error in file IO (code 11)\n",
        _no_root("/srv/sync", "on nas.example.com"),
    ),
    (
        23,
        # rsync's own "Permission denied (13)" — a folder refusing a write, not a login refused.
        'rsync: [Receiver] mkstemp "/srv/sync/.k.Xy12Ab" failed: Permission denied (13)\n'
        "rsync error: some files/attrs were not transferred (code 23)\n",
        "The sync root path /srv/sync on nas.example.com doesn't let this machine's SSH login "
        f"read or write it. Fix that folder's permissions, or set Sync root path {ON_CARD} to "
        "one this machine's SSH login can write to.",
    ),
    (
        11,
        'rsync: [Receiver] write failed on "/srv/sync/k": No space left on device (28)\n'
        "rsync error: error in file IO (code 11)\n",
        "The disk holding the sync root path /srv/sync on nas.example.com is full. Free some "
        "space on it.",
    ),
    (
        12,
        "rsync: an error this transport has no words of its own for\n",
        f"rsync couldn't sync with nas.example.com (rsync exit 12). Check SSH host and Sync root "
        f"path {ON_CARD}, and that rsync is installed on both machines.",
    ),
]


class TestWhatAFailureSays:
    @pytest.mark.parametrize(("code", "stderr", "says"), _REFUSALS)
    def test_a_push_the_host_refuses_says_why_and_what_to_do(
        self, tmp_path, monkeypatch, code, stderr, says
    ):
        monkeypatch.setattr(provider_mod.subprocess, "run", _answering(code, stderr))
        p = create_provider({**REMOTE, "staging_dir": str(tmp_path)})

        res = p.push([SyncObject(key="k", data=b"v")])

        # The words change; the verdict the outbox acts on does not.
        assert res.outcome == provider_mod._outcome_for_rsync(code)
        retries = f" {RETRIES}" if res.outcome == "transient" else ""
        assert res.detail == f"{says}{retries} Details: {' '.join(stderr.split())}"

    @pytest.mark.parametrize(("code", "stderr", "says"), _REFUSALS)
    def test_a_probe_the_host_refuses_says_the_same(
        self, tmp_path, monkeypatch, code, stderr, says
    ):
        monkeypatch.setattr(provider_mod.subprocess, "run", _answering(code, stderr))
        p = create_provider({**REMOTE, "staging_dir": str(tmp_path)})

        res = p.test()

        assert res.ok is False
        assert res.detail == f"{says} Details: {' '.join(stderr.split())}"

    def test_a_retried_refusal_says_so_and_a_permanent_one_does_not(self, tmp_path, monkeypatch):
        """The retry sentence follows the outcome: only a ``transient`` push is tried again."""
        p = create_provider({**REMOTE, "staging_dir": str(tmp_path)})
        monkeypatch.setattr(
            provider_mod.subprocess, "run", _answering(12, "rsync: connection reset\n")
        )
        assert RETRIES in p.push([SyncObject(key="k", data=b"v")]).detail
        monkeypatch.setattr(provider_mod.subprocess, "run", _answering(2, "rsync: bad option\n"))
        permanent = p.push([SyncObject(key="k", data=b"v")])
        assert permanent.outcome == "permanent"
        assert RETRIES not in permanent.detail

    def test_a_local_folder_that_refuses_names_personalclaw(self, tmp_path, monkeypatch):
        root = tmp_path / "t"
        stderr = f'rsync: [Receiver] mkstemp "{root}/.k.Xy12Ab" failed: Permission denied (13)\n'
        monkeypatch.setattr(provider_mod.subprocess, "run", _answering(23, stderr))
        p = create_provider({"path": str(root), "staging_dir": str(tmp_path / "s")})

        res = p.push([SyncObject(key="k", data=b"v")])

        assert res.detail.startswith(
            f"The sync root path {root} on this machine doesn't let PersonalClaw read or write "
            f"it. Fix that folder's permissions, or set Sync root path {ON_CARD} to one "
            f"PersonalClaw can write to. {RETRIES} Details: "
        ), res.detail


# ── a pull whose rsync run fails ─────────────────────────────────────────────────────
#
# pull returned [] for every rsync run that failed — a timeout, rsync that couldn't start, an
# exit with an error — which reads as a remote with nothing on it. The sync cycle then took an
# empty registry, published as though this were the first machine, and reported its registry
# swap lost five times over, to no other machine. Now the run's failure is raised, said as what
# went wrong, and the cycle records it.


class TestAPullThatFails:
    @needs_rsync
    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads an unreadable file anyway")
    def test_the_sync_cycle_names_the_failure_not_a_lost_race(
        self, isolated_home, local, target, monkeypatch
    ):
        """Driven for real: the registry on the target can't be read by this account — another
        machine's login wrote it — so the listing works and the pull fails. The cycle reported
        "registry CAS lost after 5 attempts", with no error."""
        registry = target / "registry.json"
        registry.write_bytes(b"{}")
        registry.chmod(0o000)
        try:
            report = _run_cycle(local, isolated_home, monkeypatch, encrypt="off")
        finally:
            registry.chmod(0o644)

        assert report.ok is False
        assert report.error.startswith(
            f"pull: The sync root path {target} on this machine doesn't let PersonalClaw read or "
            f"write it. Fix that folder's permissions, or set Sync root path {ON_CARD} to one "
            "PersonalClaw can write to. Details: "
        ), report.error
        assert report.pushed is None, "the cycle pushed on after a read it couldn't make"

    def test_a_pull_that_times_out_raises_what_to_check(self, tmp_path, monkeypatch):
        def slow(*a, **k):
            raise subprocess.TimeoutExpired(cmd="rsync", timeout=300)

        monkeypatch.setattr(provider_mod.subprocess, "run", slow)
        p = create_provider({**REMOTE, "staging_dir": str(tmp_path)})

        with pytest.raises(Exception) as caught:
            p.pull([RemoteRef("registry.json")])

        assert str(caught.value) == (
            f"rsync didn't finish within 300 seconds. If it keeps happening, check that {SSH} "
            "logs in from a terminal here — a host that doesn't answer keeps rsync waiting — or, "
            f"for a slow link, raise Command timeout {ON_CARD}. Details: Command 'rsync' timed "
            "out after 300 seconds"
        )
        assert isinstance(caught.value.__cause__, subprocess.TimeoutExpired)

    def test_a_pull_rsync_cannot_start_for_raises_what_to_install(self, tmp_path):
        """Driven through a real exec of a binary that cannot exist."""
        p = RsyncSyncProvider(
            path=str(tmp_path / "t"),
            staging_dir=str(tmp_path / "s"),
            rsync_bin="/nonexistent/pc-fixture-rsync",
        )

        with pytest.raises(Exception) as caught:
            p.pull([RemoteRef("registry.json")])

        assert str(caught.value) == (
            "Rsync Sync runs the rsync command, and PersonalClaw couldn't start it on this "
            "machine: it isn't installed, isn't on the PATH PersonalClaw runs with, or isn't "
            "executable. Install rsync where PersonalClaw can run it. Details: [Errno 2] No such "
            "file or directory: '/nonexistent/pc-fixture-rsync'"
        )

    @pytest.mark.parametrize(("code", "stderr", "says"), _REFUSALS)
    def test_a_pull_the_host_refuses_raises_the_same_words(
        self, tmp_path, monkeypatch, code, stderr, says
    ):
        monkeypatch.setattr(provider_mod.subprocess, "run", _answering(code, stderr))
        p = create_provider({**REMOTE, "staging_dir": str(tmp_path)})

        with pytest.raises(Exception) as caught:
            p.pull([RemoteRef("registry.json")])

        assert str(caught.value) == f"{says} Details: {' '.join(stderr.split())}"

    @pytest.mark.parametrize(
        ("error", "says"),
        [
            (
                "mkstemp \"{mirror}/.registry.json.Xy12Ab\" failed: Permission denied (13)",
                "Rsync Sync isn't allowed to write to its local working directory {workdir}. Fix "
                "that folder's permissions, or set Local working directory {on_card} to a folder "
                "PersonalClaw can write to.",
            ),
            (
                "write failed on \"{mirror}/registry.json\": No space left on device (28)",
                "The disk holding Rsync Sync's local working directory {workdir} is full. Free "
                "some space on it, or set Local working directory {on_card} to a folder on "
                "another disk.",
            ),
            (
                "mkstemp \"{mirror}/.registry.json.Xy12Ab\" failed: Read-only file system (30)",
                "Rsync Sync's local working directory {workdir} is on a read-only disk. Set Local "
                "working directory {on_card} to a folder PersonalClaw can write to.",
            ),
        ],
        ids=["denied", "disk-full", "read-only"],
    )
    def test_a_pull_the_mirror_refuses_names_the_working_directory(
        self, tmp_path, monkeypatch, error, says
    ):
        """In a pull the receiving side is this machine's own mirror: its refusal is the Local
        working directory's, not the sync root's."""
        workdir = tmp_path / "work"
        p = create_provider({**REMOTE, "staging_dir": str(workdir)})
        stderr = f"rsync: [receiver] {error.format(mirror=p._mirror)}\n"
        monkeypatch.setattr(provider_mod.subprocess, "run", _answering(23, stderr))

        with pytest.raises(Exception) as caught:
            p.pull([RemoteRef("registry.json")])

        expected = says.format(workdir=workdir, on_card=ON_CARD)
        assert str(caught.value).startswith(f"{expected} Details: "), caught.value

    def test_files_that_vanished_mid_pull_leave_the_rest(self, tmp_path, monkeypatch):
        """rsync's exit 24: files that vanished while it copied, as when another machine
        rewrites the registry through a temporary file. What arrived is kept; a ref that didn't
        drops like any ref the target no longer has."""
        p = create_provider({"path": str(tmp_path / "t"), "staging_dir": str(tmp_path / "s")})
        mirror = pathlib.Path(p._mirror)
        mirror.mkdir(parents=True)
        (mirror / "registry.json").write_bytes(b'{"seq":1}')
        stderr = (
            'file has vanished: "/t/.registry.json.Xy12Ab"\nrsync warning: some files vanished '
            "before they could be transferred (code 24)\n"
        )
        monkeypatch.setattr(provider_mod.subprocess, "run", _answering(24, stderr))

        out = p.pull([RemoteRef("registry.json"), RemoteRef("gone.jsonl")])

        assert [(o.key, o.data) for o in out] == [("registry.json", b'{"seq":1}')]


# ── a listing or a registry swap whose rsync run fails ───────────────────────────────
#
# list_remote answered [] and the registry swap False for every rsync run that failed, which
# read as a target with nothing on it, or as a swap another machine won. Now the run's failure
# is raised, said as a push's is — a sync root path that isn't there among them, since this
# transport never creates one. What stays: a registry that isn't there, in a root that is, is
# nothing yet — or, when one was expected, a lost race.


@pytest.fixture
def over_ssh(tmp_path, monkeypatch):
    """``over_ssh(path)``: a provider for ``path`` on example.invalid, whose ssh is a stand-in
    that runs the host's side with this machine's own rsync — in a login home of its own, and
    through its shell, which resolves ``~`` and a relative path as a login's does. Returns that
    and the login home."""
    home = tmp_path / "login-home"
    home.mkdir()
    stand_in = tmp_path / "ssh-stand-in"
    stand_in.write_text(
        "#!/bin/sh\n"
        # ssh's own options, then the host; what follows is the command the host runs.
        "while [ $# -gt 0 ]; do\n"
        '  case "$1" in -o|-p|-i|-l) shift 2 ;; -*) shift ;; *) break ;; esac\n'
        "done\n"
        "shift\n"
        f'cd "{home}" && exec env -i PATH="$PATH" HOME="{home}" LC_ALL=C /bin/sh -c "$*"\n'
    )
    stand_in.chmod(0o755)
    monkeypatch.setattr(RsyncSyncProvider, "_rsh_arg", lambda self: ["-e", str(stand_in)])

    def make(path: str) -> RsyncSyncProvider:
        return RsyncSyncProvider(
            host="example.invalid", path=path, staging_dir=str(tmp_path / "s"), timeout_secs=60
        )

    return make, home


def _unreadable(where: str, who: str, path: str) -> str:
    """What a sync root path, or the registry in it, that the login can't read says."""
    return (
        f"The sync root path {path} {where} doesn't let {who} read or write it. Fix that folder's "
        f"permissions, or set Sync root path {ON_CARD} to one {who} can write to."
    )


class TestAListingThatFails:
    @needs_rsync
    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads an unreadable folder anyway")
    def test_a_root_that_isnt_there_is_a_failure_as_is_one_it_cannot_read(self, tmp_path):
        """A root that wasn't there listed as empty — the first machine's to create, even when
        it was a share that wasn't mounted."""
        root = tmp_path / "target"
        p = RsyncSyncProvider(path=str(root), staging_dir=str(tmp_path / "s"), timeout_secs=60)
        with pytest.raises(provider_mod.RsyncFailed) as caught:
            p.list_remote()
        says = _no_root(str(root), "on this machine")
        assert str(caught.value).startswith(f"{says} Details: "), caught.value

        root.mkdir()
        root.chmod(0o000)
        try:
            with pytest.raises(provider_mod.RsyncFailed) as caught:
                p.list_remote()
        finally:
            root.chmod(0o755)

        says = _unreadable("on this machine", "PersonalClaw", str(root))
        assert str(caught.value).startswith(f"{says} Details: "), caught.value

    @needs_rsync
    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads an unreadable folder anyway")
    def test_over_ssh_a_root_that_isnt_there_is_a_failure_however_it_is_written(
        self, tmp_path, over_ssh
    ):
        """A real rsync on both ends. The host's rsync names a folder under ``~``, or relative to
        the login's home, by its full path there; the sentence names it as the setting has it."""
        make, home = over_ssh
        for path in (str(tmp_path / "gone"), "~/gone", "gone"):
            with pytest.raises(provider_mod.RsyncFailed) as caught:
                make(path).list_remote()
            says = _no_root(path, "on example.invalid")
            assert str(caught.value).startswith(f"{says} Details: "), caught.value
        assert list(home.iterdir()) == [], "a listing made a folder on the host"

        locked = home / "locked"
        locked.mkdir()
        locked.chmod(0o000)
        try:
            with pytest.raises(provider_mod.RsyncFailed) as caught:
                make("~/locked").list_remote()
        finally:
            locked.chmod(0o755)

        says = _unreadable("on example.invalid", "this machine's SSH login", "~/locked")
        assert str(caught.value).startswith(f"{says} Details: "), caught.value

    def test_each_rsync_s_words_for_a_root_that_isnt_there_read_the_same(
        self, tmp_path, monkeypatch
    ):
        """GNU rsync (as CI's Ubuntu runs it) and openrsync (as macOS ships it) word a missing
        root differently — and GNU rsync on a host that speaks another language says it in that
        language, with the errno after. A line naming some other path is no missing root."""
        gnu = 'rsync: [sender] change_dir "{}" failed: {} (2)\n'
        for path, stderr in (
            ("/srv/sync", gnu.format("/srv/sync", "No such file or directory")),
            ("/srv/sync", gnu.format("/srv/sync", "Datei oder Verzeichnis nicht gefunden")),
            ("/srv/sync", "rsync(4242): error: /srv/sync/: (l)stat: No such file or directory\n"),
            ("~/sync", gnu.format("/home/user/sync", "No such file or directory")),
            ("sync", gnu.format("/home/user/sync", "No such file or directory")),
        ):
            monkeypatch.setattr(provider_mod.subprocess, "run", _answering(23, stderr))
            p = create_provider({**REMOTE, "path": path, "staging_dir": str(tmp_path)})
            with pytest.raises(provider_mod.RsyncFailed) as caught:
                p.list_remote()
            assert str(caught.value) == sentence_with_detail(
                _no_root(path, "on nas.example.com"), stderr
            )

        for stderr in (
            gnu.format("/srv/sync-old", "No such file or directory"),
            "Warning: Identity file /keys/id_sync not accessible: No such file or directory.\n"
            "backup@nas.example.com: Permission denied (publickey).\r\n",
        ):
            monkeypatch.setattr(provider_mod.subprocess, "run", _answering(255, stderr))
            p = create_provider({**REMOTE, "staging_dir": str(tmp_path)})
            with pytest.raises(provider_mod.RsyncFailed) as caught:
                p.list_remote()
            assert "doesn't exist" not in str(caught.value), caught.value

    @pytest.mark.parametrize(("code", "stderr", "says"), _REFUSALS)
    def test_a_listing_the_host_refuses_raises_the_same_words(
        self, tmp_path, monkeypatch, code, stderr, says
    ):
        monkeypatch.setattr(provider_mod.subprocess, "run", _answering(code, stderr))
        p = create_provider({**REMOTE, "staging_dir": str(tmp_path)})

        with pytest.raises(provider_mod.RsyncFailed) as caught:
            p.list_remote()

        assert str(caught.value) == f"{says} Details: {' '.join(stderr.split())}"

    def test_a_listing_rsync_cannot_start_for_raises_what_to_install(self, tmp_path):
        """Driven through a real exec of a binary that cannot exist."""
        p = RsyncSyncProvider(
            path=str(tmp_path / "t"),
            staging_dir=str(tmp_path / "s"),
            rsync_bin="/nonexistent/pc-fixture-rsync",
        )

        with pytest.raises(provider_mod.RsyncFailed) as caught:
            p.list_remote()

        assert str(caught.value) == (
            "Rsync Sync runs the rsync command, and PersonalClaw couldn't start it on this "
            "machine: it isn't installed, isn't on the PATH PersonalClaw runs with, or isn't "
            "executable. Install rsync where PersonalClaw can run it. Details: [Errno 2] No such "
            "file or directory: '/nonexistent/pc-fixture-rsync'"
        )


class TestARegistrySwapThatFails:
    @pytest.mark.parametrize("expected", [None, hashlib.sha256(b"{}").hexdigest()],
                             ids=["create", "swap"])
    @pytest.mark.parametrize(("code", "stderr", "says"), _REFUSALS)
    def test_a_swap_whose_rsync_run_fails_raises_rather_than_losing_the_race(
        self, tmp_path, monkeypatch, code, stderr, says, expected
    ):
        """A ``False`` sent core round its loop four more times, then reported the swap lost to
        another machine."""
        monkeypatch.setattr(provider_mod.subprocess, "run", _answering(code, stderr))
        p = create_provider({**REMOTE, "staging_dir": str(tmp_path)})

        with pytest.raises(provider_mod.RsyncFailed) as caught:
            p.cas_registry(expected, b'{"machines":{"B":1}}')

        assert str(caught.value) == f"{says} Details: {' '.join(stderr.split())}"

    def test_a_registry_not_there_in_a_root_that_is_is_still_a_lost_race(
        self, tmp_path, monkeypatch
    ):
        """In each rsync's words. rsync says a missing root the same way when it reads the
        registry, so the root's listing tells the two apart: a root that isn't there raises —
        it read as the race — and so does a run the host refused."""
        sha = hashlib.sha256(b"{}").hexdigest()
        root_missing = (
            'rsync: [sender] change_dir "/srv/sync" failed: No such file or directory (2)\n'
        )

        def answering(registry_stderr: str, root_stderr: str = ""):
            def _run(argv, **kwargs):
                if "--list-only" in argv:  # the listing of the root's top level
                    code = 23 if root_stderr else 0
                    listed = "" if root_stderr else "drwxr-xr-x  64 2026/09/28 12:00:00 .\n"
                    return subprocess.CompletedProcess(argv, code, listed, root_stderr)
                return subprocess.CompletedProcess(argv, 23, stdout="", stderr=registry_stderr)

            return _run

        for stderr in (
            'rsync: [sender] link_stat "/srv/sync/registry.json" failed: No such file or '
            "directory (2)\n",
            'rsync: [sender] link_stat "/srv/sync/registry.json" failed: Datei oder Verzeichnis '
            "nicht gefunden (2)\n",
            "rsync(4242): error: '/srv/sync/registry.json': (l)stat: No such file or directory\n",
        ):
            monkeypatch.setattr(provider_mod.subprocess, "run", answering(stderr))
            p = create_provider({**REMOTE, "staging_dir": str(tmp_path)})
            assert p.cas_registry(sha, b"{}") is False, stderr

            monkeypatch.setattr(provider_mod.subprocess, "run", answering(stderr, root_missing))
            with pytest.raises(provider_mod.RsyncFailed) as caught:
                p.cas_registry(sha, b"{}")
            assert str(caught.value) == sentence_with_detail(
                _no_root("/srv/sync", "on nas.example.com"), root_missing
            )

        monkeypatch.setattr(provider_mod.subprocess, "run", _answering(255, _REFUSALS[2][1]))
        with pytest.raises(provider_mod.RsyncFailed):
            create_provider({**REMOTE, "staging_dir": str(tmp_path)}).cas_registry(sha, b"{}")

    @needs_rsync
    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads an unreadable file anyway")
    def test_over_ssh_a_registry_not_there_is_a_lost_race_and_a_root_not_there_raises(
        self, tmp_path, over_ssh
    ):
        make, home = over_ssh
        sha = hashlib.sha256(b"{}").hexdigest()
        root = home / "sync"
        root.mkdir()
        assert make("~/sync").cas_registry(sha, b"{}") is False, "no registry there yet"
        with pytest.raises(provider_mod.RsyncFailed) as caught:
            make("~/gone").cas_registry(sha, b"{}")
        says = _no_root("~/gone", "on example.invalid")
        assert str(caught.value).startswith(f"{says} Details: "), caught.value
        assert not (home / "gone").exists()

        registry = root / "registry.json"
        registry.write_bytes(b"{}")
        registry.chmod(0o000)
        try:
            with pytest.raises(provider_mod.RsyncFailed) as caught:
                make("~/sync").cas_registry(sha, b'{"machines":{"B":1}}')
        finally:
            registry.chmod(0o644)

        says = _unreadable("on example.invalid", "this machine's SSH login", "~/sync")
        assert str(caught.value).startswith(f"{says} Details: "), caught.value
        assert registry.read_bytes() == b"{}"

    def test_an_error_naming_the_staging_folder_names_the_local_working_directory(
        self, tmp_path, monkeypatch
    ):
        """A registry read comes down into a staging folder under the Local working directory:
        its refusal is that folder's, not the sync root's."""
        workdir = tmp_path / "work"
        p = create_provider({**REMOTE, "staging_dir": str(workdir)})

        def fills_the_stage(argv, **kwargs):
            stage = argv[-1].rstrip("/")
            stderr = f'rsync: [receiver] write failed on "{stage}/registry.json": No space left '
            return subprocess.CompletedProcess(
                argv, 11, stdout="", stderr=f"{stderr}on device (28)\n"
            )

        monkeypatch.setattr(provider_mod.subprocess, "run", fills_the_stage)

        with pytest.raises(provider_mod.RsyncFailed) as caught:
            p.cas_registry(hashlib.sha256(b"{}").hexdigest(), b"{}")

        assert str(caught.value).startswith(
            f"The disk holding Rsync Sync's local working directory {workdir} is full. Free some "
            f"space on it, or set Local working directory {ON_CARD} to a folder on another disk. "
            "Details: "
        ), caught.value


@pytest.fixture
def unmounted(tmp_path):
    """An empty folder standing in for a share's mount point with nothing mounted on it, and the
    sync root path under it — ``/mnt/nas`` and ``/mnt/nas/pcsync``, say."""
    stub = tmp_path / "nas"
    stub.mkdir()
    return stub, stub / "pcsync"


def _under(over: str, root, tmp_path, request) -> tuple[RsyncSyncProvider, str]:
    """A provider for ``root`` on this machine, or on example.invalid through the ssh stand-in,
    and where its sentences say the root is."""
    if over == "ssh":
        make, _home = request.getfixturevalue("over_ssh")
        return make(str(root)), "on example.invalid"
    provider = RsyncSyncProvider(path=str(root), staging_dir=str(tmp_path / "s"), timeout_secs=60)
    return provider, "on this machine"


@needs_rsync
class TestTheSyncRootIsNeverCreated:
    """rsync makes a missing folder to write into, and a sync root missing under a share that
    isn't mounted looks just like one not made yet. The root listed as not there yet, and the
    first push made it under the empty mount point: the sync went onto the local disk, quietly,
    instead of onto the share."""

    @pytest.mark.parametrize("encrypt", ["on", "off"])
    @pytest.mark.parametrize("over", ["local", "ssh"])
    def test_a_sync_cycle_fails_and_makes_nothing(
        self, isolated_home, tmp_path, unmounted, request, monkeypatch, over, encrypt
    ):
        stub, root = unmounted
        p, where = _under(over, root, tmp_path, request)
        _seed_task(isolated_home, "task-a", "a row")

        report = _run_cycle(p, isolated_home, monkeypatch, encrypt=encrypt)

        assert report.ok is False
        says = _no_root(str(root), where)
        assert report.error.startswith(f"pull: {says} Details: "), report.error
        assert list(stub.iterdir()) == [], "a folder was made under the empty mount point"

    @pytest.mark.parametrize("over", ["local", "ssh"])
    def test_no_write_called_on_its_own_makes_the_root(
        self, tmp_path, unmounted, request, over
    ):
        """The cycle lists first, but a push — the salt's among them — and the registry's create
        and swap must not make the root when called without it."""
        stub, root = unmounted
        p, where = _under(over, root, tmp_path, request)
        says = _no_root(str(root), where)

        res = p.push([SyncObject(key="machines/A/seq-0001/tasks/tasks.jsonl", data=b"v")])

        assert res.outcome == "transient", "it clears once the root is mounted, made or corrected"
        assert res.detail.startswith(f"{says} {RETRIES} Details: "), res.detail
        for expected in (None, hashlib.sha256(b"{}").hexdigest()):
            with pytest.raises(provider_mod.RsyncFailed) as caught:
                p.cas_registry(expected, b"{}")
            assert str(caught.value).startswith(f"{says} Details: "), caught.value
        assert list(stub.iterdir()) == [], "a folder was made under the empty mount point"


class TestTheSyncCycleSaysWhatFailed:
    """Driven through core's real sync cycle against a real rsync. A listing that failed read as
    an empty target, and a registry swap that failed as one another machine won: the cycle
    published as the first machine, then reported "registry CAS lost after 5 attempts" — with
    no error, and ok."""

    @needs_rsync
    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads an unreadable folder anyway")
    def test_a_listing_it_cannot_make(self, isolated_home, local, target, monkeypatch):
        target.chmod(0o000)
        try:
            report = _run_cycle(local, isolated_home, monkeypatch, encrypt="off")
        finally:
            target.chmod(0o755)

        assert report.ok is False
        says = _unreadable("on this machine", "PersonalClaw", str(target))
        assert report.error.startswith(f"pull: {says} Details: "), report.error
        assert "CAS lost" not in repr(report)

    @needs_rsync
    def test_a_registry_write_the_target_refuses(
        self, isolated_home, tmp_path, target, monkeypatch
    ):
        """The target refuses the registry's creation — as a host whose sync root the login
        can't create files in does — played by an rsync that refuses that one run and hands
        every other to the real rsync."""
        refused = f'rsync: [Receiver] mkstemp "{target}/.registry.json.Xy12Ab" failed: Permission '
        fixture = tmp_path / "pc-fixture-rsync"
        fixture.write_text(
            "#!/bin/sh\n"
            'for arg in "$@"; do\n'
            '  case "$arg" in */reg-*/)\n'
            f"    echo '{refused}denied (13)' >&2\n"
            "    exit 23 ;;\n"
            "  esac\n"
            "done\n"
            f'exec "{shutil.which("rsync")}" "$@"\n'
        )
        fixture.chmod(0o755)
        p = RsyncSyncProvider(
            path=str(target), staging_dir=str(tmp_path / "s"), timeout_secs=60,
            rsync_bin=str(fixture),
        )
        _seed_task(isolated_home, "task-a", "a row")

        report = _run_cycle(p, isolated_home, monkeypatch, encrypt="off")

        assert report.ok is False
        says = _unreadable("on this machine", "PersonalClaw", str(target))
        assert report.error == "push: " + sentence_with_detail(says, f"{refused}denied (13)")
        assert "CAS lost" not in repr(report)
        assert list(target.glob("machines/*/seq-*/*")), "the shards landed before the swap"


# ── 3. output parsing (the two formats this transport depends on) ─────────────────────


class TestItemizeParsing:
    def test_only_transferred_files_are_counted(self):
        stdout = (
            ">f+++++++ registry.json\n"
            "cd+++++++ machines/\n"
            "cd+++++++ machines/A/\n"
            ">f+++++++ machines/A/seq-0001/tasks.jsonl\n"
            ">f....... machines/A/seq-0002/tasks.jsonl\n"
        )
        got = provider_mod._transferred_paths(stdout)
        assert got == {
            "registry.json",
            "machines/A/seq-0001/tasks.jsonl",
            "machines/A/seq-0002/tasks.jsonl",
        }
        # A directory line must never be counted as an object.
        assert not any(p.endswith("/") for p in got)

    def test_an_empty_itemize_means_nothing_transferred(self):
        assert provider_mod._transferred_paths("") == set()
        assert provider_mod._transferred_paths("cd+++++++ machines/\n") == set()

    def test_noise_lines_are_ignored(self):
        stdout = "sending incremental file list\n>f+++++++ a\n\ntotal size is 3\n"
        assert provider_mod._transferred_paths(stdout) == {"a"}


class TestListingParsing:
    def test_files_are_parsed_and_directories_dropped(self):
        stdout = (
            "drwxr-xr-x          128 2026/08/18 17:44:11 .\n"
            "-rw-r--r--            3 2026/08/18 17:44:11 registry.json\n"
            "drwxr-xr-x           96 2026/08/18 17:44:11 machines\n"
            "-rw-r--r--         1024 2026/08/18 17:44:12 machines/A/seq-0001/tasks.jsonl\n"
        )
        rows = provider_mod._parse_listing(stdout)
        assert [r[0] for r in rows] == ["registry.json", "machines/A/seq-0001/tasks.jsonl"]
        assert rows[1][1] == 1024
        assert rows[0][2] == "2026/08/18 17:44:11"

    def test_a_comma_grouped_size_is_parsed(self):
        stdout = "-rw-r--r--    1,048,576 2026/08/18 17:44:11 big.jsonl\n"
        rows = provider_mod._parse_listing(stdout)
        assert rows[0][1] == 1048576

    def test_garbage_is_ignored_not_raised(self):
        assert provider_mod._parse_listing("sending incremental file list\n") == []
        assert provider_mod._parse_listing("") == []


# ── 4. configuration + manifest parity ───────────────────────────────────────────────


class TestConfiguration:
    def test_unconfigured_is_transient_for_writes_and_empty_for_reads(self):
        p = create_provider({})
        assert p.configured is False
        assert p.push([SyncObject(key="k", data=b"v")]).outcome == "transient"
        assert p.list_remote() == []
        assert p.pull([RemoteRef(key="k")]) == []
        assert p.cas_registry(None, b"{}") is False
        assert p.test().ok is False
        assert "sync root path" in p._unconfigured_detail()

    def test_a_host_without_a_path_is_still_unconfigured(self, tmp_path):
        p = create_provider({"host": "nas.local", "staging_dir": str(tmp_path)})
        assert p.configured is False

    def test_target_shape_for_remote_and_local(self, tmp_path):
        remote = create_provider(
            {"host": "u@h", "path": "/srv/sync/", "staging_dir": str(tmp_path)}
        )
        assert remote._target() == "u@h:/srv/sync/"
        assert remote._target(trailing_slash=False) == "u@h:/srv/sync"
        local = create_provider({"path": "/tmp/x/", "staging_dir": str(tmp_path)})
        assert local._target() == "/tmp/x/"

    def test_timeout_is_always_positive(self, tmp_path):
        assert create_provider({"path": "/t", "timeout_secs": 0})._timeout == 300
        assert create_provider({"path": "/t", "timeout_secs": 5})._timeout == 5

    def test_provider_identity_matches_the_manifest(self):
        manifest = json.loads(
            (pathlib.Path(__file__).parent / "app.json").read_text(encoding="utf-8")
        )
        p = create_provider({})
        assert p.name == manifest["name"] == "rsync-sync"
        assert p.display_name == manifest["displayName"]
        assert manifest["provider"]["type"] == "sync"
        # Network: the rsync and ssh it starts reach the SSH host, so install consent says so
        # even though nothing goes through the HTTP egress chokepoint.
        assert manifest["permissions"]["network"] is True

    def test_every_manifest_setting_is_honoured_by_the_factory(self):
        manifest = json.loads(
            (pathlib.Path(__file__).parent / "app.json").read_text(encoding="utf-8")
        )
        props = manifest["provider"]["settingsSchema"]["properties"]
        assert set(props) == {
            "host", "path", "port", "ssh_key", "staging_dir", "timeout_secs",
        }
        # A declared setting nobody reads is a control that looks configurable and is not.
        src = pathlib.Path(provider_mod.__file__).read_text(encoding="utf-8")
        for name in props:
            assert f'config.get("{name}"' in src, f"{name} is declared but never read"

    def test_it_is_a_real_sync_transport_provider(self):
        from personalclaw.sdk.sync import SyncTransportProvider

        assert isinstance(create_provider({}), SyncTransportProvider)


# ── 5. success criterion 7, driven through THIS transport ─────────────────────────────
#
# "No shard, sync object, or export zip ever contains .env, .local_secret, sel_hmac.key, or
# telemetry_salt — adversarially verified against EVERY transport." Core proves it against a
# test-local folder transport; rsync-sync is a new transport, so the proof is re-run here on
# the bytes that actually landed on the target.


def _seed_task(home: pathlib.Path, tid: str, title: str) -> None:
    d = home / "tasks"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{tid}.json").write_text(json.dumps({"id": tid, "title": title}))


def _plant_secrets(home: pathlib.Path) -> list[str]:
    from personalclaw.durability import inventory as inv

    planted: list[str] = []
    for rel in inv.secret_paths():
        p = home / rel
        if p.suffix or "." in p.name:
            p.parent.mkdir(parents=True, exist_ok=True)
            # The prefix is assembled rather than written as one literal so a secret
            # scanner does not flag this canary as a real key on every contributor's
            # commit. The BYTES planted are what the scan needs to be realistic; the
            # source spelling is not.
            token = "sk-" + "ant-CANARY-" + rel.replace("/", "-")
            p.write_text(f"SECRET={token}\n")
            planted.append(token)
    assert planted, "no secret paths were planted — the scan would be vacuous"
    return planted


def _run_cycle(transport, home: pathlib.Path, monkeypatch, *, encrypt: str, self_id="A"):
    from personalclaw.durability import crypto as crypto_mod
    from personalclaw.durability.shards import machine_id
    from personalclaw.durability.sync_cycle import run_sync_cycle

    monkeypatch.setattr(crypto_mod, "load_passphrase", lambda: "a shared sync passphrase")
    machine_id(home)
    return run_sync_cycle(transport, home, self_id=self_id, now="t1", encrypt=encrypt)


@needs_rsync
class TestCriterion7SecretsNeverLeave:
    @pytest.mark.parametrize("encrypt", ["on", "off"])
    def test_no_secret_content_ever_reaches_the_target(
        self, isolated_home, local, target, monkeypatch, encrypt
    ):
        """Scanned on the bytes that LANDED, not on the exclusion list. Parametrized over
        encryption because the exclusion must hold independently of it."""
        home = isolated_home
        _seed_task(home, "task-a", "an ordinary row")
        planted = _plant_secrets(home)

        report = _run_cycle(local, home, monkeypatch, encrypt=encrypt)
        assert report.ok, report.error

        landed = [p for p in target.rglob("*") if p.is_file()]
        # VACUITY FLOORS: an empty target, or one with no shard objects, proves nothing.
        assert landed, "nothing was pushed — the scan would be vacuous"
        blob = b"".join(p.read_bytes() for p in landed)
        assert blob, "every pushed object was empty — the scan would be vacuous"
        rel_paths = [str(p.relative_to(target)) for p in landed]
        assert any("machines/" in r for r in rel_paths), "no shard object was pushed"

        for token in planted:
            assert token.encode() not in blob, f"{token} reached the target"
        for marker in (b".local_secret", b"sel_hmac.key", b"telemetry_salt"):
            assert marker not in blob, f"{marker!r} was named in a transported object"
        for marker in (".local_secret", "sel_hmac.key", "telemetry_salt"):
            assert marker not in " ".join(rel_paths), f"{marker} appeared as an object path"

    def test_the_canary_scan_can_actually_fail(
        self, isolated_home, local, target, monkeypatch
    ):
        """Proves the scan is capable of catching a leak rather than matching nothing."""
        home = isolated_home
        _seed_task(home, "task-a", "an ordinary row")
        planted = _plant_secrets(home)
        _seed_task(home, "task-leak", f"leaked {planted[0]}")
        _run_cycle(local, home, monkeypatch, encrypt="off")
        blob = b"".join(p.read_bytes() for p in target.rglob("*") if p.is_file())
        assert planted[0].encode() in blob, (
            "the scan could not see a canary that really did leave — it is vacuous"
        )

    def test_encryption_is_on_by_default_for_this_transport(
        self, isolated_home, local, target, monkeypatch
    ):
        """§4.4 leaves rsync-sync unnamed; core resolved the tie to ON. Pin the resolution
        here so the app and core cannot drift apart silently."""
        from personalclaw.durability.crypto import (
            DEFAULT_ENCRYPT_BY_TRANSPORT,
            encryption_enabled_for,
            is_ciphertext,
        )

        assert DEFAULT_ENCRYPT_BY_TRANSPORT["rsync-sync"] is True
        assert encryption_enabled_for("rsync-sync", "auto") is True

        home = isolated_home
        _seed_task(home, "task-a", "confidential-row-marker")
        assert _run_cycle(local, home, monkeypatch, encrypt="auto").ok
        shards = [
            p for p in target.rglob("*")
            if p.is_file() and "machines" in str(p.relative_to(target))
        ]
        assert shards, "no shard object was pushed — the proof would be vacuous"
        assert all(is_ciphertext(p.read_bytes()) for p in shards)
        blob = b"".join(p.read_bytes() for p in target.rglob("*") if p.is_file())
        assert b"confidential-row-marker" not in blob


def test_no_shell_true_anywhere_in_the_module():
    """A source-level floor: behaviour tests cover the paths they call, but a new method
    with ``shell=True`` would slip past them."""
    src = pathlib.Path(provider_mod.__file__).read_text(encoding="utf-8")
    assert "shell=True" not in src
    assert "os.system" not in src
    assert "shell=False" in src
    assert len(src) > 1000  # vacuity floor: we really read the module


def test_readme_documents_the_ssh_and_cas_limitations():
    readme = (pathlib.Path(__file__).parent / "README.md").read_text(encoding="utf-8")
    assert "compare-and-swap" in readme.lower()
    assert "ignore-times" in readme
    assert "BatchMode" in readme
    assert os.path.exists(pathlib.Path(__file__).parent / "LICENSE")
