"""Lima sandbox provider tests — no real ``limactl``, no vendor SDK, no network.

Every case substitutes the one ``limactl`` seam (``_run_limactl``) and ``shutil.which`` so the
availability probe, its cache/degradation behaviour, and the launch wrapping are exercised for
real without a Lima install present. Covers: available/unavailable classification (binary
missing, instance absent, instance stopped, instance running), the probe cache + re-probe +
running→stopped degradation, host↔guest path translation, the ``limactl shell`` wrap, the
exec-time ``--workdir`` splice with the host ``cwd`` stripped, the cleanup no-op, and the
factory's defaults/config + SDK contract conformance.
"""

from __future__ import annotations

import asyncio
import subprocess

from personalclaw.sdk.sandbox import SandboxHandle, SandboxProvider, SandboxSpec
from provider import LimaSandboxHandle, LimaSandboxProvider, create_provider


def _cp(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["limactl"], returncode=returncode, stdout=stdout, stderr=""
    )


def _running(monkeypatch, provider: LimaSandboxProvider, status: str = "Running") -> list:
    """Point *provider* at a fake limactl reporting *status*; return the recorded call list."""
    monkeypatch.setattr("provider.shutil.which", lambda name: "/usr/bin/limactl")
    calls: list = []

    def fake_run(args):
        calls.append(list(args))
        return _cp(stdout=status)

    monkeypatch.setattr(provider, "_run_limactl", fake_run)
    return calls


# ── availability classification ────────────────────────────────────────────────────────


def test_available_when_instance_running(monkeypatch):
    p = LimaSandboxProvider(instance="pc-test", probe_ttl=1000.0)
    _running(monkeypatch, p, "Running")
    assert p.available() is True
    assert "running" in p.unavailable_reason.lower()


def test_unavailable_when_limactl_missing(monkeypatch):
    p = LimaSandboxProvider(instance="pc-test", probe_ttl=1000.0)
    monkeypatch.setattr("provider.shutil.which", lambda name: None)
    ok, reason = p.status()
    assert ok is False
    assert "limactl not found" in reason


def test_unavailable_when_instance_stopped(monkeypatch):
    p = LimaSandboxProvider(instance="pc-test", probe_ttl=1000.0)
    _running(monkeypatch, p, "Stopped")
    ok, reason = p.status()
    assert ok is False
    assert "Stopped" in reason and "pc-test" in reason


def test_unavailable_when_instance_absent(monkeypatch):
    p = LimaSandboxProvider(instance="pc-test", probe_ttl=1000.0)
    monkeypatch.setattr("provider.shutil.which", lambda name: "/usr/bin/limactl")
    monkeypatch.setattr(p, "_run_limactl", lambda args: _cp(stdout="", returncode=1))
    ok, reason = p.status()
    assert ok is False
    assert "does not exist" in reason


def test_probe_never_raises_on_subprocess_error(monkeypatch):
    p = LimaSandboxProvider(instance="pc-test", probe_ttl=1000.0)
    monkeypatch.setattr("provider.shutil.which", lambda name: "/usr/bin/limactl")

    def boom(args):
        raise OSError("spawn failed")

    monkeypatch.setattr(p, "_run_limactl", boom)
    ok, reason = p.status()
    assert ok is False
    assert "limactl probe failed" in reason


# ── probe cache + degradation (SC3) ──────────────────────────────────────────────────────


def test_probe_is_cached_within_ttl(monkeypatch):
    p = LimaSandboxProvider(instance="pc-test", probe_ttl=1000.0)
    calls = _running(monkeypatch, p, "Running")
    assert p.available() is True
    assert p.available() is True
    assert len(calls) == 1  # second call served from cache


def test_probe_reprobes_after_ttl(monkeypatch):
    p = LimaSandboxProvider(instance="pc-test", probe_ttl=0.0)
    calls = _running(monkeypatch, p, "Running")
    p.available()
    p.available()
    assert len(calls) == 2  # ttl 0 → every call re-probes


def test_running_instance_degrades_to_stopped(monkeypatch):
    p = LimaSandboxProvider(instance="pc-test", probe_ttl=1000.0)
    monkeypatch.setattr("provider.shutil.which", lambda name: "/usr/bin/limactl")
    state = {"status": "Running"}
    monkeypatch.setattr(p, "_run_limactl", lambda args: _cp(stdout=state["status"]))
    assert p.available() is True
    # Instance stops out from under us; a forced re-probe greys the tier out with a reason.
    state["status"] = "Stopped"
    ok, reason = p.status(force=True)
    assert ok is False
    assert "Stopped" in reason


# ── host ↔ guest path translation ────────────────────────────────────────────────────────


def test_path_translation_round_trips_within_home(monkeypatch):
    p = LimaSandboxProvider(instance="pc-test", guest_home="/home/alice.linux")
    p._host_home = "/Users/alice"  # pin the host prefix so the test is platform-independent
    guest = p.host_to_guest("/Users/alice/proj/app")
    assert guest == "/home/alice.linux/proj/app"
    assert p.guest_to_host(guest) == "/Users/alice/proj/app"


def test_path_translation_identity_outside_home():
    p = LimaSandboxProvider(instance="pc-test", guest_home="/home/alice.linux")
    p._host_home = "/Users/alice"
    assert p.host_to_guest("/opt/shared/data") == "/opt/shared/data"


# ── wrap + exec ──────────────────────────────────────────────────────────────────────────


def test_wrap_builds_limactl_shell_argv():
    p = LimaSandboxProvider(instance="pc-test")
    handle = p.wrap(SandboxSpec(), ["echo", "hi"])
    assert isinstance(handle, LimaSandboxHandle)
    assert handle.argv == ["limactl", "shell", "pc-test", "--", "echo", "hi"]


def test_exec_argv_splices_translated_workdir():
    p = LimaSandboxProvider(instance="pc-test", guest_home="/home/alice.linux")
    p._host_home = "/Users/alice"
    handle = p.wrap(SandboxSpec(), ["echo", "hi"])
    argv = handle._exec_argv("/Users/alice/proj")
    assert argv == [
        "limactl", "shell", "--workdir", "/home/alice.linux/proj",
        "pc-test", "--", "echo", "hi",
    ]
    # No cwd → base argv unchanged.
    assert handle._exec_argv(None) == ["limactl", "shell", "pc-test", "--", "echo", "hi"]


def test_exec_translates_cwd_and_strips_host_cwd(monkeypatch):
    p = LimaSandboxProvider(instance="pc-test", guest_home="/home/alice.linux")
    p._host_home = "/Users/alice"
    handle = p.wrap(SandboxSpec(), ["echo", "hi"])
    captured: dict = {}

    async def fake_exec(*argv, **kwargs):
        captured["argv"] = list(argv)
        captured["kwargs"] = kwargs
        return "PROC"

    monkeypatch.setattr("provider.asyncio.create_subprocess_exec", fake_exec)
    proc = asyncio.run(handle.exec(cwd="/Users/alice/proj", stdout=asyncio.subprocess.PIPE))
    assert proc == "PROC"
    assert "cwd" not in captured["kwargs"]  # host cwd translated away, never passed through
    assert "--workdir" in captured["argv"]
    assert "/home/alice.linux/proj" in captured["argv"]
    assert captured["kwargs"]["stdout"] == asyncio.subprocess.PIPE


def test_cleanup_is_noop_and_idempotent():
    p = LimaSandboxProvider(instance="pc-test")
    handle = p.wrap(SandboxSpec(), ["true"])
    assert handle.cleanup() is None
    assert handle.cleanup() is None  # safe to call more than once


# ── factory + contract conformance ───────────────────────────────────────────────────────


def test_create_provider_defaults():
    p = create_provider(None)
    assert isinstance(p, SandboxProvider)
    assert p.name == "lima-sandbox"
    assert p.display_name == "Lima VM (isolated)"
    assert p._instance == "personalclaw"
    assert p._probe_ttl == 30.0


def test_create_provider_reads_config():
    p = create_provider(
        {"instance": "my-vm", "cpus": 4, "memory": "8GiB", "disk": "40GiB",
         "template": "ubuntu", "probe_ttl_secs": 5}
    )
    assert p._instance == "my-vm"
    assert p._cpus == 4
    assert p._memory == "8GiB"
    assert p._disk == "40GiB"
    assert p._template == "ubuntu"
    assert p._probe_ttl == 5.0
