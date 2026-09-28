# Git Sync

Sync PersonalClaw state between your machines through a **git remote you own**. The
transport keeps a local working clone and moves durability shard objects as files it
commits and pushes, so `git log -p` over those shards is a human-diffable audit history of
what the assistant knows — that readable history is the whole point of this transport.

**Git Sync** is a **sync transport** — it implements the `personalclaw.sdk.sync`
`SyncTransportProvider` contract and becomes selectable as `durability.sync_transport`
once installed and enabled.

## What this is

A standalone PersonalClaw app bundle (part of the core/app workspace split). It ships as a
self-contained directory:

- `app.json` — the manifest (`provider.type: "sync"` + `implementation`).
- `provider.py` — the implementation, exposed via `create_provider`.
- `test_provider.py` — the app's own tests, driven against a real git remote on this machine,
  reached over ssh through a stand-in ssh command.

It imports only the PersonalClaw **SDK** (never core internals), so core can evolve without
breaking it:

- `personalclaw.sdk.sync`
- `personalclaw.sdk.git`

The transport moves bytes only. The merge, the machine-seq registry contents, and the
outbox retry loop all live above it in the core durability layer. It shells out to `git`
via `subprocess`; the durability **service** invokes it — never an agent — so it adds no
new agent command surface.

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**Git Sync** — the install runs through the security scanner and lifecycle exactly like any
other app. (Or [install it from a shell](../docs/third-party-install.md#installing-from-a-shell).) Then set
`durability.sync_transport` to `git-sync` and configure the settings below.

## Settings

| Key | Label | Notes |
|---|---|---|
| `repo_url` | Git remote URL | The ssh or https URL of a git remote you own (both machines point at the same one). A remote at a local path is refused. Leave empty to configure later — the transport stays idle until set. |
| `local_clone` | Local working clone | Where the working clone lives on this machine (default `~/.personalclaw/sync/git-sync`). Supports `~` and `$VARS`. Cloned on first use, reused after. |
| `branch` | Branch | The branch to sync on (default `main`). Both machines must use the same branch. |

## Configuring two machines

1. Install and enable **Git Sync** on each machine.
2. Set `repo_url` on both machines to the **same** git remote you own, and `branch` to the
   same branch. Each machine may keep its `local_clone` wherever it likes.
3. Set `durability.sync_transport` to `git-sync` on both.

Each machine writes its own shard objects under `machines/<id>/…` and reads the others'.
Because every object is insert-only and keyed by content path, the repo converges no matter
which machine syncs first — this satisfies the durability layer's two-machine convergence
criterion (two machines sharing one remote reach the same merged state).

The first machine to sync against a **brand-new empty remote** is not an error: the initial
clone of an empty repo succeeds and the first push publishes the branch.

## How it works

- **Insert-only, idempotent.** Each shard object is written to `<clone>/<key>` exactly
  once. A re-push of an existing key is skipped, never overwritten, so the git history stays
  append-only per object and the sync cycle can retry freely after a lost race.
- **Pull before push.** Every push first `git pull --ff-only`s so it carries others' objects
  and does not conflict; a pull failure against a fresh/empty remote is fine.
- **Registry compare-and-swap rides git.** The single shared `registry.json` is swapped only
  when the caller's expected hash matches what the pulled clone holds; the write is then
  committed and pushed, and **git's own push rejection is the compare-and-swap** — if the
  remote moved under us the push is rejected and the caller re-pulls and retries. No
  hand-rolled lock.
- **Transient vs permanent.** A push rejected because the remote moved is reported
  `transient` (retry). A push the remote refuses for a bad URL or denied auth is `permanent`
  (retry will not fix it), while a failure before the push — the first clone, a step in the
  working clone — is `transient`. A clean run is `delivered`. Every failure says what went
  wrong and what to do — the remote didn't accept this machine's credentials, no repository
  is visible at that URL, the working clone can't be written, git isn't installed — with
  git's own message after it.
- **Deterministic committer.** The transport's automated commits use a fixed identity
  (`PersonalClaw Sync <sync@personalclaw.local>`) set via `git -c` flags, so a sync commit
  never depends on — or pollutes — ambient git config and names no real person.

## Security posture

- **Your git credentials, your remote.** The transport uses whatever git credentials the
  machine already has for the remote you point it at: your SSH agent, and the ssh command and
  credential helpers in your own git configuration. It reads and writes only that repo; the
  remote's own access controls are the trust boundary.
- **The working clone's own configuration runs nothing.** Anything that can write the clone
  can write its `.git`, so git runs with the settings that stop a repository's hooks,
  file-system monitor, ssh command and credential helpers from running
  (`personalclaw.sdk.git.git_argv`), and with PersonalClaw's child environment: none of the
  gateway's secrets, and the SSH agent only for a command that talks to the remote.
- **The repo holds shard objects only.** Secrets are excluded upstream by the durability
  layer before anything reaches a transport, so this app never sees `.env`, API keys, or the
  credential store — it cannot sync what it is never handed.
- **No encryption, on purpose.** Unlike third-party-storage transports, git-sync keeps the
  shards plaintext so `git log -p` stays human-readable — the readable history is the value.
  Point it at a remote whose access you control; anyone who can read the repo has the state.

## License

MIT — see `LICENSE`.
