"""Lima sandbox provider — the isolated (virtual-machine) isolation tier (EXECUTION-ISOLATION EI-4 §2).

A ``sandbox`` provider owns the seam between "here is a command to launch" and "here is a
running child process" (``personalclaw.sdk.sandbox.SandboxProvider``). The in-core ``none``
provider composes the host path-sandbox with resource ceilings and adds no further isolation;
this provider is the stronger **isolated** tier: it launches the command INSIDE a Lima virtual
machine through ``limactl shell``, so the virtual machine — not a host path filter — is the
boundary.

Three things the isolated tier owes its callers, all implemented here:

* **A cached availability probe.** :meth:`LimaSandboxProvider.available` (and the richer
  :meth:`~LimaSandboxProvider.status`) probe whether ``limactl`` exists and the configured
  instance is *Running*, caching the answer for ``probe_ttl`` seconds. Stopping the instance
  greys the tier out — with a human reason for the degradation dialog — within one TTL.
* **Host↔guest path translation.** Lima's default mount makes the host home available inside
  the guest, so :meth:`~LimaSandboxProvider.host_to_guest` /
  :meth:`~LimaSandboxProvider.guest_to_host` map a host working directory to the path it
  resolves to in the virtual machine, and a launch's ``cwd`` is translated to a guest
  ``--workdir`` at exec time.
* **The two-phase launch contract.** :meth:`~LimaSandboxProvider.wrap` produces a
  :class:`LimaSandboxHandle` whose ``argv`` a caller can inspect before spawning, whose
  :meth:`~LimaSandboxHandle.exec` launches it, and whose :meth:`~LimaSandboxHandle.cleanup`
  releases any temp state (the virtual-machine tier creates none, so it is a no-op).

Imports go through ``personalclaw.sdk.sandbox`` ONLY — never a core internal — so core can
evolve without breaking this bundle. It shells out to the ``limactl`` binary via ``subprocess``;
no subprocess error is ever allowed to raise out of a method.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from typing import Any

from personalclaw.sdk.sandbox import SandboxHandle, SandboxProvider, SandboxSpec

#: The Lima command-line binary. Absent from PATH → the tier is unavailable, never a crash.
_LIMACTL = "limactl"

#: Ceiling for a single ``limactl`` invocation. A hung probe is treated as unavailable rather
#: than being allowed to block a spawn.
_PROBE_TIMEOUT = 15

#: Default availability-probe cache lifetime. SC3: a stopped instance greys the tier out
#: within one probe TTL, so this bounds how stale the doctor's green/red dot can be.
_DEFAULT_PROBE_TTL = 30.0


class LimaSandboxHandle(SandboxHandle):
    """A command wrapped to run inside a Lima instance via ``limactl shell``.

    ``argv`` is the inspectable base launch (``limactl shell <instance> -- <argv>``); a host
    ``cwd`` passed to :meth:`exec` is translated to a guest ``--workdir`` at launch time so the
    host process never chdirs to a path that only exists inside the virtual machine.
    """

    def __init__(self, argv: list[str], translate_cwd: Any) -> None:
        self._argv = list(argv)
        # Callable host-path -> guest-path; injected so the handle needs no back-reference to
        # the provider and the translation is unit-testable in isolation.
        self._translate_cwd = translate_cwd

    @property
    def argv(self) -> list[str]:
        return list(self._argv)

    def _exec_argv(self, cwd: Any) -> list[str]:
        """The launch argv for a given host ``cwd`` — pure and synchronous so the workdir
        splice is unit-tested without spawning ``limactl``.

        A ``--workdir <guest>`` flag is inserted right after ``limactl shell`` (a flag to the
        ``shell`` subcommand must precede the instance name); a falsy ``cwd`` leaves the base
        argv untouched.
        """
        argv = list(self._argv)
        if cwd:
            guest = self._translate_cwd(str(cwd))
            argv[2:2] = ["--workdir", guest]
        return argv

    async def exec(self, **kwargs: object) -> asyncio.subprocess.Process:
        # A host cwd is translated to a guest --workdir, NOT passed to the host limactl client
        # (that guest path need not exist on the host). Every other kwarg
        # (stdin/stdout/stderr/env/start_new_session/limit …) passes straight through. Host
        # resource ceilings are not applied: the command runs in the virtual machine, which
        # owns its own limits — the host only sees the thin limactl client.
        cwd = kwargs.pop("cwd", None)
        argv = self._exec_argv(cwd)
        return await asyncio.create_subprocess_exec(*argv, **kwargs)

    def cleanup(self) -> None:
        # The virtual-machine tier creates no host-side temp profile or launcher script, so
        # there is nothing to remove. Idempotent no-op, honoring the SandboxHandle contract
        # (safe to call more than once, never raises).
        return None


class LimaSandboxProvider(SandboxProvider):
    """The isolated tier: launches commands inside a Lima virtual machine.

    Not always available (unlike the ``none`` builtin): :meth:`available` probes for the
    ``limactl`` binary and a *Running* instance and caches the answer, so the Store/doctor dot
    and the terminal sandbox picker reflect a stopped instance within one probe TTL.
    """

    name = "lima-sandbox"
    display_name = "Lima VM (isolated)"

    def __init__(
        self,
        instance: str = "personalclaw",
        cpus: int = 2,
        memory: str = "4GiB",
        disk: str = "20GiB",
        template: str = "default",
        probe_ttl: float = _DEFAULT_PROBE_TTL,
        guest_home: str | None = None,
    ) -> None:
        self._instance = instance or "personalclaw"
        self._cpus = int(cpus)
        self._memory = str(memory)
        self._disk = str(disk)
        self._template = str(template)
        self._probe_ttl = float(probe_ttl)
        # Lima's default mount exposes the host home INSIDE the guest at the same path, so the
        # translation is identity within the home subtree unless a non-default guest home is
        # configured. Stored as prefixes the translation helpers rewrite between.
        self._host_home = os.path.expanduser("~")
        self._guest_home = guest_home or self._host_home
        # Cached probe state: (last-probe monotonic time, available, human reason). The
        # sentinel is None — not 0.0 — because time.monotonic() is small on a freshly booted
        # host, and a numeric sentinel would let the first status() call skip the probe.
        self._probe_at: float | None = None
        self._available = False
        self._reason = "not probed yet"

    # ── limactl seam (a single method, so tests substitute the binary cleanly) ──────────

    def _run_limactl(self, args: list[str]) -> subprocess.CompletedProcess:
        """Run ``limactl <args>`` with output captured and a hard timeout; never checked."""
        return subprocess.run(
            [_LIMACTL, *args],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
            check=False,
        )

    def _probe(self) -> tuple[bool, str]:
        """Probe availability now: ``(available, reason)``. Never raises.

        Unavailable — with a reason fit for the degradation dialog — when ``limactl`` is not on
        PATH, the instance does not exist, or the instance is not *Running*.
        """
        if shutil.which(_LIMACTL) is None:
            return False, "limactl not found on PATH — install Lima (https://lima-vm.io)"
        try:
            cp = self._run_limactl(["list", self._instance, "--format", "{{.Status}}"])
        except (subprocess.SubprocessError, OSError) as exc:
            return False, f"limactl probe failed: {exc}"
        status = (cp.stdout or "").strip()
        if cp.returncode != 0 or not status:
            return (
                False,
                f"Lima instance {self._instance!r} does not exist — "
                f"create it with `limactl start`",
            )
        if status.lower() != "running":
            return (
                False,
                f"Lima instance {self._instance!r} is {status} — "
                f"start it with `limactl start {self._instance}`",
            )
        return True, f"Lima instance {self._instance!r} is running"

    def status(self, force: bool = False) -> tuple[bool, str]:
        """Cached ``(available, reason)``. Re-probes at most once per ``probe_ttl`` seconds
        (``force=True`` re-probes now). This is what makes a stopped instance grey out within
        one TTL rather than on every spawn."""
        now = time.monotonic()
        if force or self._probe_at is None or (now - self._probe_at) >= self._probe_ttl:
            self._available, self._reason = self._probe()
            self._probe_at = now
        return self._available, self._reason

    def available(self) -> bool:
        """Whether this tier can run right now (the doctor's green/red dot)."""
        return self.status()[0]

    @property
    def unavailable_reason(self) -> str:
        """The human reason behind the most recent :meth:`status`/:meth:`available` answer —
        what the degradation dialog shows when the tier is greyed out."""
        return self._reason

    # ── host ↔ guest path translation ──────────────────────────────────────────────────

    def host_to_guest(self, path: str) -> str:
        """Map a host path to where it resolves inside the guest.

        Within the mounted home subtree the host prefix is rewritten to the guest home;
        outside it the path is returned unchanged (Lima mounts the tree at the same location by
        default, so identity is the correct fallback rather than a guess)."""
        resolved = os.path.abspath(os.path.expanduser(str(path)))
        if self._host_home and resolved.startswith(self._host_home):
            return self._guest_home + resolved[len(self._host_home):]
        return resolved

    def guest_to_host(self, path: str) -> str:
        """The inverse of :meth:`host_to_guest` for a guest path under the guest home."""
        guest = str(path)
        if self._guest_home and guest.startswith(self._guest_home):
            return self._host_home + guest[len(self._guest_home):]
        return guest

    # ── SandboxProvider contract ─────────────────────────────────────────────────────────

    def wrap(self, spec: SandboxSpec, argv: list[str]) -> LimaSandboxHandle:
        """Wrap *argv* to run inside the Lima instance: ``limactl shell <instance> -- <argv>``.

        The virtual machine is the isolation boundary, so the host OS path-sandbox level
        (``spec.mode``) is not re-applied on the host side; ``spec`` still travels with the
        launch for callers that inspect it. A host ``cwd`` becomes a guest ``--workdir`` at
        :meth:`LimaSandboxHandle.exec` time.
        """
        wrapped = [_LIMACTL, "shell", self._instance, "--", *list(argv)]
        return LimaSandboxHandle(wrapped, self.host_to_guest)


def create_provider(config: dict[str, Any] | None = None) -> LimaSandboxProvider:
    """Extension factory — builds the Lima sandbox provider from user settings."""
    config = config or {}
    return LimaSandboxProvider(
        instance=str(config.get("instance", "") or "personalclaw"),
        cpus=int(config.get("cpus", 2) or 2),
        memory=str(config.get("memory", "") or "4GiB"),
        disk=str(config.get("disk", "") or "20GiB"),
        template=str(config.get("template", "") or "default"),
        probe_ttl=float(config.get("probe_ttl_secs", _DEFAULT_PROBE_TTL) or _DEFAULT_PROBE_TTL),
    )
