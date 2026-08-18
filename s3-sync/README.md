# S3 Sync

Sync PersonalClaw state between your machines through an S3-compatible object store you
own — AWS S3, a self-hosted MinIO, a NAS gateway, or anything that speaks the S3 API.

This is a **transport**: it moves durability shard objects and nothing else. The merge, the
machine-seq registry, the conflict review queue and the outbox all live in PersonalClaw
core, above it.

## Why you might pick this over `dir-sync` or `git-sync`

| | |
|---|---|
| `git-sync` | You want a human-readable `git log -p` audit history of what the assistant knows. Stays plaintext, by design. |
| `dir-sync` | You already have a synced folder (iCloud, Dropbox, Syncthing) and want zero credentials. |
| **`s3-sync`** | You want a real remote that neither machine has to be online at the same time for, and you are willing to hold a bucket + a key pair. |

## Setup

1. **Create a private bucket.** Do not enable public access. Versioning is optional but
   recommended — it turns an accidental deletion into a recoverable one.
2. **Create a least-privilege identity** for it. This app needs exactly four actions,
   scoped to your bucket and (if you set one) your prefix:
   `s3:PutObject`, `s3:GetObject`, `s3:ListBucket`, and nothing else. It never deletes, never
   changes an ACL, and never touches another bucket. Do not reuse an admin key.
3. **Fill in the settings** (Settings → Apps → S3 Sync):
   - **Endpoint URL** — scheme included, e.g. `https://s3.us-east-1.amazonaws.com`, or
     `http://nas.local:9000` for a self-hosted MinIO.
   - **Bucket**, and optionally a **key prefix** to confine the sync root.
   - **Region** — must match the bucket's region on AWS; any value works on MinIO.
   - **Access key ID** / **Secret access key**.
4. **Point every machine at the same bucket + prefix**, and use the same passphrase on each
   (see Encryption).
5. Hit **Test connection**. It performs one zero-key `ListObjectsV2`, which exercises DNS,
   the egress pin, TLS, the signature and the bucket policy in a single request.

Instead of storing the keys in settings you can export
`PERSONALCLAW_S3_ACCESS_KEY_ID` / `PERSONALCLAW_S3_SECRET_ACCESS_KEY`
(and `PERSONALCLAW_S3_SESSION_TOKEN` for temporary STS credentials).

**These env names are app-scoped on purpose.** This transport deliberately does *not* read
`AWS_ACCESS_KEY_ID`, `AWS_PROFILE`, or the instance-role/metadata credential chain. A
personal sync transport that silently adopted whatever AWS identity happened to be in your
shell could write your assistant's state into a company or production account you never
meant to touch. Configure it explicitly, or it stays idle.

## Encryption

Shard encryption is applied by PersonalClaw core **above** this transport, and it is **ON by
default for `s3-sync`** — an object store is storage you do not fully control, so the store
should never see plaintext.

Set the passphrase once per machine (all machines must share it):

```
personalclaw credentials set PERSONALCLAW_SYNC_PASSPHRASE
```

Routing metadata — `registry.json`, the salt object, and the machine/seq key paths — stays
plaintext on purpose, so `list_remote` and the registry compare-and-swap work on a machine
that does not have the key. Shard *contents* do not.

Two consequences worth knowing before you start:

- **A missing salt object is a hard setup error, not a silent fallback to plaintext.** If
  encryption is on and the sync root has no salt, sync refuses rather than uploading
  readable state.
- **A forgotten passphrase costs the remote copies, not your data.** The local home stays
  authoritative. Recovery is a fresh sync root. There is no passphrase reset, because
  there is no escrow — that is the point.

Secrets (`.env`, `.local_secret`, `sel_hmac.key`, `telemetry_salt`, and anything else the
state inventory marks `secret=True`) are excluded upstream and never reach any transport,
encrypted or not.

## What the store must support

**Conditional writes** (`If-None-Match: *` and `If-Match: <etag>`). AWS S3 and current MinIO
both do. The transport uses them for two things:

- **Insert-only pushes.** Every object PUT carries `If-None-Match: *`, so a retried push is
  a `412` (counted as skipped) rather than an overwrite. That is one round trip and has no
  race, unlike a HEAD-then-PUT.
- **The registry compare-and-swap.** `registry.json` is swapped only if its current ETag
  still matches what this machine read.

If your store does *not* implement conditional writes, the registry CAS **refuses** rather
than falling back to an unconditional PUT. Sync will visibly stall instead of silently
discarding another machine's registration. Use `dir-sync` or `git-sync` on such a store.

## How requests are made

Every request is signed with **AWS Signature V4** (computed in-process from the standard
library — this app has no `boto3`/`botocore` dependency) and travels through PersonalClaw's
guarded egress chokepoint, `sdk.net.fetch`, under a policy derived by
`sync_egress_policy(endpoint)`. That policy:

- **pins the single endpoint host** you configured — no other host is reachable, including
  a redirect target, and including the cloud metadata services, which are denied outright;
- carries your `security.egress` posture (deny lists still apply, and a deny outranks the pin);
- raises the response body cap rather than removing it.

Because egress is pinned to the endpoint host, addressing is **path-style**
(`<endpoint>/<bucket>/<key>`) rather than virtual-host style — the latter puts the bucket in
the hostname, which is not the host that was pinned.

A truncated response body is treated as an integrity failure and dropped, never merged as a
short shard.

## Layout in the bucket

```
<prefix>/registry.json                                  # shared, plaintext, CAS-guarded
<prefix>/encryption-salt                                # first-write-wins, plaintext
<prefix>/machines/<machine-id>/seq-<n>/<domain>/<file>   # shard objects (encrypted by default)
```

Objects are insert-only and idempotent on their key. Nothing here is ever overwritten except
`registry.json`, and only under a matching ETag.

## Costs and housekeeping

Sync writes one small object per shard per cycle, so the cost driver is request count, not
storage. Two knobs help: raise `durability.sync_interval_secs` to sync less often, and set a
bucket **lifecycle rule** to expire old `machines/*/seq-*/` prefixes — old sequences are
superseded once every machine has consumed them. This app never deletes, so expiry is yours
to configure.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `access denied (HTTP 403)` | Wrong key, a bucket policy that does not grant the four actions, or a **region mismatch** — SigV4 binds the signature to the region, so a wrong region reads as an auth failure. |
| `bucket ... not found` | Typo in the bucket, or an endpoint in a different region than the bucket. |
| Egress refusal in the logs | The endpoint host does not match the pin, or it is on your `security.egress` deny list. Redirects off the pinned host are refused by design. |
| Sync stalls with no error | Registry CAS is losing every race — usually a store without conditional-write support. |
| `HTTP 501` on push | Same cause: the store rejected the conditional header. |

## License

Apache-2.0. See `LICENSE`.
