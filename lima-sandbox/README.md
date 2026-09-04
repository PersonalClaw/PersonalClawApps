# Lima Sandbox

The **isolated (virtual-machine) execution-isolation tier**, backed by
[Lima](https://lima-vm.io) — Linux virtual machines on macOS and Linux. Instead of running an
agent-influenced command on the host behind a path filter, this tier launches it **inside a
Lima instance** through `limactl shell`, so the virtual machine — not a host path filter — is
the isolation boundary.

**Lima Sandbox** is a **sandbox provider**: it implements the `personalclaw.sdk.sandbox`
`SandboxProvider` contract and registers under the `sandbox` provider type once installed and
enabled, alongside the built-in `none` tier. A spawn site (a workflow stage, a subagent, a
terminal opened "inside the run's sandbox") resolves it by name and launches through it.

## What this is

A standalone PersonalClaw app bundle — a self-contained directory:

- `app.json` — the manifest (`provider.type: "sandbox"`, `capabilities: ["isolated"]`, and the
  Lima instance settings).
- `provider.py` — the implementation, exposed via `create_provider`.
- `test_provider.py` — the app's own tests, driven against a stubbed `limactl` (no Lima
  install, no network).

It imports only the PersonalClaw **SDK** (`personalclaw.sdk.sandbox`), never core internals, so
core can evolve without breaking it. It shells out to the `limactl` binary via `subprocess`.

## Prerequisites

Lima is a **user-provided prerequisite** — this app drives an instance, it does not create one:

1. Install Lima: `brew install lima` (macOS) or see the
   [Lima install guide](https://lima-vm.io/docs/installation/) (Linux).
2. Create and start an instance whose name matches the `instance` setting below:
   `limactl start --name personalclaw`.

Until both hold, the tier reports itself **unavailable with a reason** (see *Availability &
degradation*) rather than failing a launch.

## Settings

| Key | Label | Notes |
|---|---|---|
| `instance` | Lima instance name | The `limactl` instance a run executes inside (default `personalclaw`). Both the availability probe and every launch target this instance. |
| `cpus` | vCPUs | Advisory record of the instance's CPU allocation (default `2`). Lima owns the running instance's real limits. |
| `memory` | Memory | Advisory record of the instance's memory allocation (default `4GiB`). |
| `disk` | Disk | Advisory record of the instance's disk allocation (default `20GiB`). |
| `template` | Lima template | The template the instance was created from (default `default`). Recorded for the degradation dialog; creating the instance stays a user step. |
| `probe_ttl_secs` | Availability probe TTL (seconds) | How long a probe of the instance's Running/Stopped state is cached (default `30`). |

## How it works

- **Availability & degradation.** `available()` probes whether `limactl` is on `PATH` and the
  configured instance is *Running*, and caches the answer for `probe_ttl_secs`. Stopping the
  instance flips the tier to **greyed-out-with-a-reason within one probe TTL** — the reason
  (`unavailable_reason`) is what the degradation dialog shows: `limactl` missing, the instance
  absent, or the instance not running, each with the command to fix it.
- **Host↔guest path translation.** Lima's default mount exposes the host home inside the guest,
  so `host_to_guest()` / `guest_to_host()` map a host working directory to the path it resolves
  to in the virtual machine (identity outside the mounted home subtree). A launch's `cwd` is
  translated to a guest `--workdir` at exec time, so a terminal opened "inside the sandbox"
  lands in the same project directory.
- **The launch.** `wrap(spec, argv)` returns a handle whose `argv` is
  `limactl shell <instance> -- <argv>` — inspectable before spawning. `exec()` launches it (the
  virtual machine owns the workload's resource limits; the host only sees the thin `limactl`
  client), and `cleanup()` is a no-op because the VM tier creates no host-side temp state.

## Relationship to `backend.sandbox` (EXECUTION-ISOLATION EI-4)

This app supplies the **provider** — the isolated tier itself. It is the counterpart to the
core-side launcher work in EI-4 §1.3(4), which lets a *consumer* app run its own backend in a
sandbox tier: that app declares `backend.sandbox` to name the tier, and the core launcher maps
its `permissions.network` → the sandbox `egress_tier` and `permissions.storage` → the sandbox
`allowed_write_paths` when it builds the launch policy. Those consumer-side manifest mappings
live in core (they are read by the core app-launcher, not by this bundle); installing
**Lima Sandbox** is what makes the *isolated* tier those launches can target actually exist on
the machine.

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**Lima Sandbox** — the install runs through the security scanner and lifecycle exactly like any
other app. (Or `POST /api/apps {"source": ".../apps/lima-sandbox"}`.) Enable it, point
`instance` at your running Lima instance, and the isolated tier becomes selectable wherever a
sandbox tier is chosen.

## Security posture

- **The virtual machine is the boundary.** A command runs inside the Lima guest, not on the
  host — a stronger isolation than the host path-sandbox the `none` tier applies. The guest sees
  only what Lima mounts into it.
- **No host-side residue.** The tier writes no temp profile or launcher on the host; `cleanup()`
  has nothing to remove.
- **Your Lima instance, your limits.** CPU/memory/disk are the instance's own (set when you
  `limactl start`); the settings here are an advisory record for the degradation dialog, not a
  second enforcement path.

## License

MIT — see `LICENSE`.
