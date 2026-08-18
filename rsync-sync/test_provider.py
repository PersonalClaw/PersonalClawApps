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

from personalclaw.sdk.sync import RemoteRef, SyncObject

HAVE_RSYNC = shutil.which("rsync") is not None
needs_rsync = pytest.mark.skipif(not HAVE_RSYNC, reason="rsync binary not available")


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
            "host; rm -rf /",
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
            "/srv/sync\nrm -rf /",
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
        p = create_provider(
            {"host": "host; rm -rf /", "path": "/srv/sync", "staging_dir": str(tmp_path)}
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
        p = RsyncSyncProvider(
            path=str(tmp_path / "does-not-exist"), staging_dir=str(tmp_path / "s")
        )
        r = p.test()
        assert r.ok is False
        assert "unreachable" in r.detail


class TestFailureHandling:
    def test_a_timeout_is_transient_not_permanent(self, tmp_path, monkeypatch):
        """A hung transfer must not make the outbox discard the objects."""

        def slow(*a, **k):
            raise subprocess.TimeoutExpired(cmd="rsync", timeout=1)

        monkeypatch.setattr(provider_mod.subprocess, "run", slow)
        p = create_provider({"path": str(tmp_path / "t"), "staging_dir": str(tmp_path / "s")})
        res = p.push([SyncObject(key="k", data=b"v")])
        assert res.outcome == "transient"
        assert "timed out" in res.detail
        assert p.list_remote() == []
        assert p.cas_registry(None, b"{}") is False
        assert p.test().ok is False

    def test_a_missing_rsync_binary_is_permanent(self, tmp_path, monkeypatch):
        def missing(*a, **k):
            raise FileNotFoundError("no rsync")

        monkeypatch.setattr(provider_mod.subprocess, "run", missing)
        p = create_provider({"path": str(tmp_path / "t"), "staging_dir": str(tmp_path / "s")})
        res = p.push([SyncObject(key="k", data=b"v")])
        assert res.outcome == "permanent"
        assert "cannot run rsync" in res.detail

    @pytest.mark.parametrize(
        "code,expected",
        [(1, "permanent"), (2, "permanent"), (4, "permanent"),
         (10, "transient"), (23, "transient"), (30, "transient"), (255, "transient")],
    )
    def test_exit_codes_map_to_the_right_verdict(self, code, expected):
        assert provider_mod._outcome_for_rsync(code) == expected

    def test_a_failed_push_reports_the_rsync_error_line(self, tmp_path, monkeypatch):
        def failing(argv, **k):
            return subprocess.CompletedProcess(
                argv, 23, stdout="", stderr="rsync: link_stat failed: No such file\n"
            )

        monkeypatch.setattr(provider_mod.subprocess, "run", failing)
        p = create_provider({"path": str(tmp_path / "t"), "staging_dir": str(tmp_path / "s")})
        res = p.push([SyncObject(key="k", data=b"v")])
        assert res.outcome == "transient"
        assert "link_stat failed" in res.detail


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
        # No network permission: this transport speaks through ssh/rsync, not the HTTP
        # egress chokepoint, so claiming `network` would overstate what it reaches.
        assert "network" not in manifest.get("permissions", {})

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
