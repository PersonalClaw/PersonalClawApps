# Folder Sync

Sync PersonalClaw state between your machines through a shared/synced folder — a
cloud-sync mount, an NFS share, or a mounted USB drive. No credentials, no server:
point two machines at the same folder and durability converges.

**Folder Sync** is a **sync transport** — it implements the
`personalclaw.sdk.sync` `SyncTransportProvider` contract and becomes selectable as
`durability.sync_transport` once installed and enabled.

## What this is

A standalone PersonalClaw app bundle (part of the core/app workspace split). It ships
as a self-contained directory:

- `app.json` — the manifest (`provider.type: "sync"` + `implementation`).
- `provider.py` — the implementation, exposed via `create_provider`.
- `test_provider.py` — the app's own tests.

It imports only the PersonalClaw **SDK** (never core internals), so core can evolve
without breaking it:

- `personalclaw.sdk.sync`

The transport moves bytes only. The merge, the machine-seq registry contents, and the
outbox retry loop all live above it in the core durability layer.

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**Folder Sync** — the install runs through the security scanner and lifecycle exactly
like any other app. (Or `POST /api/apps {"source": ".../apps/dir-sync"}`.) Then set
`durability.sync_transport` to `dir-sync` and configure the sync folder below.

## Settings

| Key | Label | Notes |
|---|---|---|
| `root` | Sync folder | Absolute path to the shared/synced folder both machines point at (e.g. `~/synced/personalclaw-sync` or `/Volumes/usb/pc-sync`). Supports `~` and `$VARS`. Leave empty to configure later — the transport stays idle until set. |

## Configuring two machines

1. Install and enable **Folder Sync** on each machine.
2. Set `root` on both machines to the **same** synced folder — the same cloud-sync
   folder, the same NFS mount point, or the same USB volume path. The two paths only need
   to resolve to the same underlying folder; they may be spelled differently per machine.
3. Set `durability.sync_transport` to `dir-sync` on both.

Each machine writes its own shard objects under `machines/<id>/…` and reads the others'.
Because every object is insert-only and keyed by content path, the folder converges no
matter which machine syncs first — this satisfies the durability layer's two-machine
convergence criterion (two machines sharing one folder reach the same merged state).

## How it works

- **Insert-only, idempotent.** Each shard object is written to `<root>/<key>` exactly
  once. A re-push of an existing key is skipped, never overwritten, so the sync cycle can
  retry freely after a lost race.
- **Atomic writes.** Objects are written to a `.tmp-` file in the same directory, then
  `os.replace`d into place, so a reader never sees a half-written object and `list_remote`
  excludes the temp files.
- **Registry compare-and-swap.** A synced folder has no cross-process atomic CAS, so the
  single shared `registry.json` is guarded by a rename-based lock: the transport creates a
  `.registry.lock` directory (`os.mkdir` is atomic on POSIX and on the network filesystems
  people sync through), compares the current registry hash under the lock, writes only on a
  match, and always releases the lock. A held lock is reported as a lost race, and the
  caller re-pulls and retries.

## Security posture

- **No credentials leave the machine.** There is no account, token, or server — the
  transport only reads and writes files in the folder you choose. Whatever protects that
  folder (your disk, your cloud-sync account, your network share) is the only trust
  boundary.
- **The folder holds shard objects only.** Secrets are excluded upstream by the durability
  layer before anything reaches a transport, so this app never sees `.env`, API keys, or
  the credential store — it cannot sync what it is never handed.
- **Anyone with the folder has the state.** The synced folder is as sensitive as the
  PersonalClaw state it carries; share it only with machines and people you trust, exactly
  as you would the app's data directory.

## License

MIT — see `LICENSE`.
