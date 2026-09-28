# GitHub Notifications Inbox

Your GitHub notifications, as inbox items you can triage in PersonalClaw: review requests,
mentions, CI failures and issue activity. It only reads, and GitHub filters by time, so history
never floods in.

## What this is

A PersonalClaw **inbox source** (`provider.type: "inbox"`). It implements
`MessageSourceProvider` from `personalclaw.sdk.inbox`, and the inbox service polls it like any
other source. Each notification thread becomes one inbox item, and a `mention` reason is marked
as a mention.

It is deliberately **read-only**. GitHub notifications are not a conversation surface, so
replying and reacting return `False` rather than pretending: you answer on the pull request or
issue itself.

## Settings

| Key | Default | Meaning |
| --- | --- | --- |
| `token` | *(unset)* | A GitHub personal access token that can read your notifications. **Required**: notifications belong to an account, so without a token nothing is read. Stored as a secret and never shown again. |
| `api_base` | `https://api.github.com` | Override for GitHub Enterprise. |
| `timeout_secs` | `20` | Seconds to wait for each GitHub response. |

The inbox's **watched channels** for this source, when set, are a repository allow-list
(`owner/name`), and notifications from any other repository are dropped.

## Network egress

Every request goes through PersonalClaw's egress guard (`personalclaw.sdk.net.fetch`, under the
connector policy plus your **Settings → Security → Network egress** rules). That means public
hosts only, the resolved address pinned, every redirect re-checked, and each request recorded
in the security event log. The app opens no socket of its own. A GitHub Enterprise server on
your own network is reachable only once you allow its host there. The token travels in an
`Authorization` header, never in a URL.

## Behaviour worth knowing

- **Checkpoint = high-water mark.** Each poll remembers the newest `updated_at` it saw and
  passes it as `?since=` next time, so GitHub does the filtering. The first successful poll
  plants the mark even when it finds nothing.
- **Degrade to empty.** With no token, the network down, an API error or an egress refusal, a
  poll logs a warning and returns nothing, with its checkpoint unchanged. An inbox source never
  takes the inbox service down with it.

## Install

From the **Store**, install **GitHub Notifications Inbox**, enable it, and set the token in its
settings. The install dialog shows that it uses the network.

## Run the tests

```bash
./scripts/test-bundles inbox-github-notifications
```

No network: the behaviour tests stub the fetch seam, and the egress tests replace the SDK's
guarded fetch.

## License

MIT — see `LICENSE`.
