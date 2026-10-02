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
(2.12 or newer: PersonalClaw will not run an older one) via `subprocess`; the durability
**service** invokes it — never an agent — so it adds no new agent command surface.

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**Git Sync** — the install runs through the security scanner and lifecycle exactly like any
other app. (Or [install it from a shell](../docs/third-party-install.md#installing-from-a-shell).) Then set
`durability.sync_transport` to `git-sync` and configure the settings below.

## Settings

| Key | Label | Notes |
|---|---|---|
| `repo_url` | Git remote URL | The ssh or https URL of a git remote you own (both machines point at the same one). A remote at a local path is refused, and so is an https URL with a user name or token written into it (put the token in Access token), or an ssh one — written as a URL or scp-like — with a password (ssh signs in with your key). Leave empty to configure later — the transport stays idle until set. |
| `local_clone` | Local working clone | Where the working clone lives on this machine (default `~/.personalclaw/sync/git-sync`). Supports `~` and `$VARS`. Cloned on first use, reused after. |
| `branch` | Branch | The branch to sync on (default `main`). Both machines must use the same branch. A branch the remote doesn't have yet starts empty. |
| `token` | Access token | For an https remote that needs a token to sign in (sensitive: kept in PersonalClaw's credential store). Handed to git by its own credential helper, never written into the working clone or onto a command line; only an https remote is given it, or an http one on this machine. Leave empty for ssh. |
| `username` | User name | The user name to sign in with alongside Access token, if your host wants one. Empty signs in as `x-access-token`, which a host that checks only the token accepts. Leave empty for ssh. |

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
- **Every copy stays.** A sync sends this machine's records as one whole copy
  (`machines/<id>/seq-<n>/`), and only when they changed since the copy before. Git Sync removes
  none of them — the repository's history is the record of what the assistant knew, which is the
  point of this transport — and Settings → Backups → Sync says so. Unencrypted, git stores a file
  it has seen before once, so a copy costs the repository what changed in it; encrypted, every
  copy is new bytes, and costs its whole size.
- **Catch up before every step.** Every push, read and registry swap first fetches the
  remote and replays this machine's unpushed commits on top of it (`git rebase`), so the
  clone carries everyone's objects. A remote with nothing on the branch yet has nothing to
  catch up with. Any other catch-up that can't be made stops a read or a registry swap and
  says why, since the clone would be served as the remote it has fallen behind; a push goes
  on, and says what went wrong in its own words.
- **A lost race is caught up in the same push.** When another machine pushes between this
  one's catch-up and its push, git turns the push away; Git Sync catches up again and pushes
  once more (up to three tries in all), so the push lands rather than leaving the clone
  holding a commit the remote lacks. A key the remote gained in the meantime keeps the
  remote's copy and counts as skipped — the same insert-only rule as every push.
- **Registry compare-and-swap rides git.** The single shared `registry.json` is swapped only
  when the caller's expected hash matches what the caught-up clone holds; the write is then
  committed and pushed, and **git's own push rejection is the compare-and-swap** — if the
  remote moved under us the push is rejected, the write is dropped from the clone, and the
  caller re-reads the remote's registry and retries. No hand-rolled lock.
- **What can conflict.** Shard objects never do: each key is written once, by the machine
  whose id it carries. A real conflict takes something outside the transport — a commit made
  by hand in the working clone that the remote also changed, or a key that is a file on one
  side and a folder on the other. The push then says which path, and that you can run
  `git pull --rebase origin <branch>` in the working clone to resolve it, or delete the clone
  so Git Sync clones the remote afresh.
- **Transient vs permanent.** A push still turned away after its tries (another push first,
  the branch moved under it, or the branch's ref lock taken), one that can't reach the remote,
  a branch the remote couldn't update for a reason of its own, and a failure before the push —
  the first clone, a step in the working clone, a lock file an interrupted git left there —
  are `transient` (the next sync tries again). A push the remote refuses for a bad URL, denied
  auth, its own rules (a hook, branch protection), a branch checked out in its working tree
  (point at a bare repository instead), a shallow working clone, a branch it hides from
  pushes, objects its own repository is missing (a damaged repository: run `git fsck` there),
  a repository it can't store the objects or the branch in, a branch name clashing with one it
  has (`sync` beside `sync/main`), or a branch name it won't take — and a real conflict — are
  `permanent` (retrying will not fix them). A Branch that git doesn't accept as a branch name
  is refused before git runs. A clean run is `delivered`. Every failure says what went wrong
  and what to do — the remote didn't accept this machine's credentials, no repository is
  visible at that URL, the working clone can't be written, which lock file is in the way, git
  isn't installed — with git's own message after it.
- **A listing, a read or a registry swap that fails says so.** The sync cycle's listing is
  empty only when there is nothing there yet: no Git remote URL set, settings git can't use
  (the push and **Test connection** say why), or a remote with nothing on the branch. A clone that
  can't be made, a catch-up that can't be made, a folder Git Sync won't sync through, a file
  in the working clone that can't be read — each raises what stopped it, and the cycle reports
  that sentence. A read drops only an object the clone doesn't have. A registry swap answers
  "lost the race" only when another machine's push got there first or the registry isn't the
  one expected; one the remote refuses for any other reason says why, and its write is taken
  back from the clone.
- **A changed Git remote URL.** The working clone always syncs with Git remote URL. When the
  setting changes — or the clone's own `.git/config` points it anywhere else — Git Sync clones
  Git remote URL into a new folder beside the old clone and swaps it in once that clone has
  succeeded, with the old folder's permissions; until one does, it fetches from and pushes to
  neither remote. It swaps out a clone of its own whenever everything in it that the old remote
  doesn't have is Git Sync's own work: a commit the old remote had, such as a hosting service's
  first README, doesn't hold the swap back, and neither does one on the new remote. A clone
  holding anything else — a commit made there by hand that the old remote never got, a file
  made there — is left as it is, and the sync says to set Local working clone to a new folder.
- **Deterministic committer.** The transport's automated commits use a fixed identity
  (`PersonalClaw Sync <sync@personalclaw.local>`) set via `git -c` flags, so a sync commit
  never depends on — or pollutes — ambient git config and names no real person.

## Security posture

- **Your git credentials, your remote.** The transport uses whatever git credentials the
  machine already has for the remote you point it at: your SSH agent, and the ssh command and
  credential helpers in your own git configuration. It reads and writes only that repo; the
  remote's own access controls are the trust boundary.
- **An https token lives in Access token, and nowhere else.** An http(s) Git remote URL with a
  user name or token written into it is refused before git runs, since git would keep it in the
  working clone's `.git/config` and pass it on its command line, where anyone on this machine can
  read it; the refusal names the URL without it and says to put the token in **Access token**.
  Access token is kept in PersonalClaw's credential store and signs git in through one credential
  helper of its own, which reads it from that git command's environment: it is on no command line
  and in no file, and none of your own credential helpers is asked for this remote or told to
  keep the token. Only an https remote is given it — or an http one on this machine; plain http
  to anywhere else would carry it unencrypted, so that is refused — and an ssh remote never is.
  A working clone made earlier from a URL with a token in it has its origin rewritten without
  it, and every copy of the token taken out of its `.git`.
- **A password in an ssh Git remote URL is refused too.** git would keep
  `ssh://user:password@host/…` in `.git/config` and hand the password to ssh on its command line
  as part of the login name, where anyone on this machine can read it, and ssh never signs in
  with it. Written scp-like, `user:password@host:path`, git reads the user name as the host and
  hands the password to ssh as part of the path, to send to that host. Take the password out and
  sign in with your ssh key; a working clone made from such a URL has its origin rewritten with
  only the user name, and every copy of the password taken out of its `.git`.
- **A credential in Git remote URL is never shown.** A URL can carry a user name and password,
  or a token (`https://<token>@host/…`). **Test connection**'s success, and every failure's detail
  and error, name the URL without it — and without any query or fragment, or anything written
  before the host of an scp-like `user@host:path` — so the credential reaches neither the sync
  job's result nor its audit record, whichever way the URL is written.
- **Git Sync commits only into a clone of its own.** Every working clone it makes is marked as
  its own in the clone's git configuration (`personalclaw.gitSyncClone`). A folder at Local
  working clone that holds commits or files Git Sync didn't make — a repository of your own,
  even one whose remote is Git remote URL — is never committed into, pushed from, fetched into
  or checked out; it is only looked at, and left byte-for-byte as it was. The sync says to set
  Local working clone to a new folder. A clone an older Git Sync made, before clones were
  marked, is taken as its own only when every commit in it is Git Sync's and nothing else is
  uncommitted there; one whose remote holds a commit someone else made (a hosting service's
  first README) is left alone the same way, and syncing resumes in a new folder.
- **The working clone's own configuration runs nothing.** Anything that can write the clone
  can write its `.git`, so git runs with the settings that stop a repository's hooks,
  file-system monitor, ssh command and credential helpers from running
  (`personalclaw.sdk.git.git_argv`), and with PersonalClaw's child environment: none of the
  gateway's secrets, and the SSH agent only for a command that talks to the remote. Nor does it
  send a push anywhere but Git remote URL: an origin, push URL, second URL or url rewrite set
  there gets the clone replaced by a fresh clone of Git remote URL — or, when it holds work Git
  Sync didn't make, left alone and not synced through.
- **Nothing outside the working clone is read or written.** git checks out a link another
  machine committed as a link, to any file on this machine, so Git Sync follows none out of the
  clone: a key whose path leaves it, through a link or with `..`, is refused before anything is
  read or written; a link at the key itself is refused even when it points inside the clone; and
  a listing follows no link, and names each one where it looks. The sync report names each
  refused key, and a change from another machine that holds one is not taken in. Remove the link
  from the remote's branch.
- **The repo holds shard objects only.** Secrets are excluded upstream by the durability
  layer before anything reaches a transport, so this app never sees `.env`, API keys, or the
  credential store — it cannot sync what it is never handed.
- **No encryption, on purpose.** Unlike third-party-storage transports, git-sync keeps the
  shards plaintext so `git log -p` stays human-readable — the readable history is the value.
  Point it at a remote whose access you control; anyone who can read the repo has the state.

## What it starts

Install consent names this before anything installs (the manifest's `launches`):

- It starts the `git` program installed on this machine, as you and outside PersonalClaw: it
  clones your remote into the working clone you set, commits there, and pulls and pushes. It
  runs with your own git sign-in and settings (or, with an Access token set, that token), and
  with the working clone's own git settings, which can add commands of their own for it to
  run.

## Network

Reaches only the git remote you set in **Git remote URL**, over HTTPS or SSH.

## License

MIT — see `LICENSE`.
