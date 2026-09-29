# Rsync Sync

Sync PersonalClaw state between your machines with `rsync` over ssh, to any host you can
already log into — a home server, a NAS, a VPS. If you have an ssh key and a directory, you
have a sync remote.

This is a **transport**: it moves durability shard objects and nothing else. The merge, the
machine-seq registry, the conflict review queue and the outbox all live in PersonalClaw core,
above it.

## Why you might pick this over the other transports

| | |
|---|---|
| `git-sync` | You want a human-readable `git log -p` audit history. Stays plaintext, by design. |
| `dir-sync` | You already have a synced folder (iCloud, Dropbox, Syncthing) and want zero setup. |
| `s3-sync` | You want an object store and are willing to hold a bucket + key pair. |
| **`rsync-sync`** | You already have ssh access to a box you trust, and you want efficient delta transfers without adding a cloud account. |

## Setup

1. **Pick a host and a directory.** `ssh you@nas.local` should already work, using a key —
   not a password (see below). Create the sync root, e.g. `mkdir -p /srv/personalclaw-sync`.
   Rsync Sync never creates it: a sync root that isn't there stops the sync with that as its
   error. A folder missing under a disk or share that isn't mounted looks just like one not
   made yet, and making it would quietly move the sync onto the local disk.
2. **Fill in the settings** (the Rsync Sync card in Settings → Providers):
   - **SSH host** — `nas.local` or `backup@nas.local`. Leave empty to rsync to a local or
     mounted path instead.
   - **Sync root path** — the absolute path **on the target**, e.g. `/srv/personalclaw-sync`.
   - **SSH port** / **SSH identity file** — only if they differ from your ssh defaults.
   - **Local working directory** — where the local mirror lives (see Performance).
3. **Mark the sync root**: run `personalclaw setup --app rsync-sync` on any one machine. It
   asks whether the disk or share the sync root is on is mounted, then puts a
   `.personalclaw-sync-root` file in it, and nothing else. Rsync Sync syncs only with a folder
   that has one, because a share that isn't mounted can leave an empty folder where the sync
   root was, which looks just like a new sync root: written, it would quietly take the sync
   onto the local disk. The marker is in the sync root, so every machine sees it.
4. **Point every machine at the same host + path**, and use the same passphrase on each
   (see Encryption).
5. Hit **Test connection**. It runs one recursive listing, which exercises ssh, the host key,
   authentication and the path in a single command, and finds the marker.

`rsync` must be installed on **both** ends. It usually already is.

### A sync root from release 0.1.5 or earlier

It has no marker yet, so syncing stops once, on every machine, with "The sync root path … has
no .personalclaw-sync-root file in it". Check that its disk or share is mounted, then run
`personalclaw setup --app rsync-sync` once, on any machine, and sync again.

### Authentication must be non-interactive

The transport runs ssh with `BatchMode=yes`. That means **key-based auth only**: ssh will
never prompt, because a prompt inside a background sync job hangs until the timeout instead
of failing with a readable reason. Use an ssh agent, or an unencrypted key dedicated to this
host.

rsync and the ssh it starts get PersonalClaw's child environment, not the gateway's, which
holds every secret saved in PersonalClaw: the child allowlist plus your SSH agent's socket
(`personalclaw.sdk.util.child_process_env(ssh_agent=True)`), and nothing else.

**Host-key checking is left at your own ssh default, deliberately.** This app does not pass
`StrictHostKeyChecking=no` or point `UserKnownHostsFile` at `/dev/null` to make first contact
"just work" — that would accept any key from any host claiming to be yours, which is the
man-in-the-middle this transport must not open. Connect once by hand (`ssh you@nas.local`) to
record the host key, then sync.

## Encryption

Shard encryption is applied by PersonalClaw core **above** this transport, and it is **ON by
default for `rsync-sync`**. Set the passphrase once per machine (all machines must share it):

```
personalclaw credentials set PERSONALCLAW_SYNC_PASSPHRASE
```

Routing metadata — `registry.json`, the salt object, and the machine/seq key paths — stays
plaintext so listings and the registry compare-and-swap work without the key. Shard
*contents* do not. A missing salt with encryption on is a hard setup error, never a silent
fallback to plaintext, and a forgotten passphrase costs the remote copies rather than your
data (the local home stays authoritative).

Secrets (`.env`, `.local_secret`, `sel_hmac.key`, `telemetry_salt`, and anything the state
inventory marks `secret=True`) are excluded upstream and never reach any transport.

## How a write finds the sync root

Every write checks that the sync root is there and holds its marker, as part of the write
itself, so a disk or share that goes away after an earlier look is never written in its
absence. Over ssh, rsync's own `--rsync-path` option starts the host's rsync only once the
host's shell finds both, in the same command. On this machine, the sync root is opened and
checked just before rsync starts, and held open until it's done, so an ordinary unmount of its
disk is refused as busy while rsync writes. A listing takes a folder without the marker for no
sync root at all, rather than one with nothing in it yet.

## How commands are run

Every invocation is an **argv list with no shell**, bounded by a timeout, with `--` before
the path operands. The host and path are validated against strict character sets first:

- a host may contain only letters, digits, `.`, `-`, `_` and one optional `user@`;
- a path may not begin with `-` (rsync would read it as an option) and may not contain `:`
  (rsync would read it as a `host:path` or `host::module` daemon spec).

A rejected value leaves the transport visibly unconfigured — it never runs a partially valid
command. Without these rules a host of `-e/bin/sh` or a path of `--delete` is remote code
execution, because rsync parses its own operands.

## Two rsync behaviours worth knowing

**An update can be silently skipped.** rsync's quick check compares size and mtime, so a file
whose new content is the *same length* and is written within the same clock second is not
transferred — and rsync still exits 0. This was measured with a realistic registry change:
`{"seq":19}` → `{"seq":20}` did not transfer. Shard objects are immune (they are insert-only
and never rewritten), but `registry.json` is rewritten every cycle, so registry writes use
`--ignore-times` and are then read back and verified.

**There is no compare-and-swap.** rsync has a create-only primitive (`--ignore-existing`,
whose `--itemize-changes` output reports whether the file was really created) but nothing
conditional for an overwrite. So the registry swap is *verify → write → read back*, and it
reports a lost race whenever the registry it finds isn't the one it expected — before its
write, or after it, when it should be its own.

That bias is deliberate. Core's CAS loop re-pulls, re-merges peers' entries and retries when
a swap reports a lost race, so a false one costs one extra round trip — while a false success
would silently discard another machine's registration. An rsync run that fails during the swap
is no race: it stops the sync with that as its error, since no re-pull would fix it. A residual
race remains: two machines swapping the registry in the same instant can lose one update,
which the loser re-applies on its next cycle. If you need a genuinely atomic registry swap, use
`s3-sync`, which has conditional writes.

## Performance

- **Pushes** stage into a throwaway directory and go up in **one** rsync invocation, so a
  cycle is one ssh connection, not one per object.
- **Pulls** come down into a persistent local **mirror** under the working directory. That is
  what makes them incremental: rsync transfers only what changed since the last pull. Deleting
  the mirror is safe — the next pull just re-transfers.
- Objects are insert-only, so re-pushing is nearly free (`--ignore-existing` skips them).

## Housekeeping

This transport never deletes anything on the target. Old `machines/*/seq-*/` prefixes are
superseded once every machine has consumed them, so prune them yourself when the sync root
grows — a periodic `find /srv/personalclaw-sync/machines -type d -name 'seq-*' -mtime +90`
review on the host is enough.

## Troubleshooting

A failed sync or connection test says what went wrong and what to do — the host key isn't
trusted yet, the host turned down the ssh login, the sync root path doesn't exist there or
isn't marked, rsync isn't installed on one end, a run went past **Command timeout**, this
machine's **Local working directory** can't be written — with rsync's, ssh's or the
filesystem's own words after it. A listing, a pull or a registry swap whose rsync run fails
stops the sync with that as its error, rather than reading as a target with nothing on it or a
swap another machine won — and a **Sync root path** that doesn't exist is such a failure too,
on every machine, the first one included: nothing is pushed until it is mounted, created and
marked, or the setting corrected. Files that vanish from the target while a pull copies —
another machine rewriting the registry — are not a failure: what arrived is used.

| Symptom | Cause |
|---|---|
| "The sync root path … doesn't exist" | The disk or share it lives on isn't mounted, the folder was never made, or **Sync root path** is mistyped. Mount it; create it if it's the folder you meant (`mkdir -p` the path, on the host for an SSH target) and mark it with `personalclaw setup --app rsync-sync`; or correct the setting. Then sync again. |
| "The sync root path … has no .personalclaw-sync-root file in it" | The folder there isn't marked as the sync root: its disk or share isn't mounted, and what's there is the empty folder it leaves, or no machine has marked it yet (a new sync root, or one from release 0.1.5 or earlier). Mount it, then run `personalclaw setup --app rsync-sync`, which asks before it marks the folder. |
| Times out on every cycle | With `BatchMode=yes` set, a timeout means the host is unreachable or the key is not accepted — **not** that it asked for a password. Try the same `ssh` by hand. |
| `Host key verification failed` | Expected on first contact. Connect once by hand to record the key; the app will not accept an unknown key for you. |
| PersonalClaw couldn't start rsync on this machine | rsync is not installed here, or is not on the `PATH` PersonalClaw runs with. |
| Misconfigured, with a character complaint | The host or path contains something that rsync could read as an option or a daemon spec. Use a plain hostname and a plain absolute path. |
| Sync stalls, registry never updates | Two machines racing the registry, or a target whose clock is far from this machine's. Check `durability.sync_interval_secs` and the host's time. |

## Layout on the target

```
<path>/.personalclaw-sync-root                          # the marker setup puts there
<path>/registry.json                                    # shared, plaintext
<path>/encryption-salt                                  # first-write-wins, plaintext
<path>/machines/<machine-id>/seq-<n>/<domain>/<file>     # shard objects (encrypted by default)
```

## Network

Reaches only the host you set in **SSH host**, over ssh. With **SSH host** empty, it syncs to **Sync root path** on this machine and reaches nothing.

## License

MIT — see `LICENSE`.
