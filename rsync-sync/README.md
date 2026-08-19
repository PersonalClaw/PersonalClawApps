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
2. **Fill in the settings** (Settings → Apps → Rsync Sync):
   - **SSH host** — `nas.local` or `backup@nas.local`. Leave empty to rsync to a local or
     mounted path instead.
   - **Sync root path** — the absolute path **on the target**, e.g. `/srv/personalclaw-sync`.
   - **SSH port** / **SSH identity file** — only if they differ from your ssh defaults.
   - **Local working directory** — where the local mirror lives (see Performance).
3. **Point every machine at the same host + path**, and use the same passphrase on each
   (see Encryption).
4. Hit **Test connection**. It runs one recursive listing, which exercises ssh, the host key,
   authentication and the path in a single command.

`rsync` must be installed on **both** ends. It usually already is.

### Authentication must be non-interactive

The transport runs ssh with `BatchMode=yes`. That means **key-based auth only**: ssh will
never prompt, because a prompt inside a background sync job hangs until the timeout instead
of failing with a readable reason. Use an ssh agent, or an unencrypted key dedicated to this
host.

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
reports failure whenever it cannot prove its own bytes landed.

That bias is deliberate. Core's CAS loop re-pulls, re-merges peers' entries and retries when
a swap reports failure, so a false failure costs one extra round trip — while a false success
would silently discard another machine's registration. A residual race remains: two machines
swapping the registry in the same instant can lose one update, which the loser re-applies on
its next cycle. If you need a genuinely atomic registry swap, use `s3-sync`, which has
conditional writes.

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

| Symptom | Cause |
|---|---|
| Times out on every cycle | With `BatchMode=yes` set, a timeout means the host is unreachable or the key is not accepted — **not** that it asked for a password. Try the same `ssh` by hand. |
| `Host key verification failed` | Expected on first contact. Connect once by hand to record the key; the app will not accept an unknown key for you. |
| `cannot run rsync` | rsync is not on this machine's `PATH`. |
| Misconfigured, with a character complaint | The host or path contains something that rsync could read as an option or a daemon spec. Use a plain hostname and a plain absolute path. |
| Sync stalls, registry never updates | Two machines racing the registry, or a target whose clock is far from this machine's. Check `durability.sync_interval_secs` and the host's time. |

## Layout on the target

```
<path>/registry.json                                    # shared, plaintext
<path>/encryption-salt                                  # first-write-wins, plaintext
<path>/machines/<machine-id>/seq-<n>/<domain>/<file>     # shard objects (encrypted by default)
```

## License

Apache-2.0. See `LICENSE`.
