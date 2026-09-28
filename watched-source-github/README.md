# GitHub Repo Watcher

Fires your automations when a GitHub repository you watch **ships a release** or **opens an
issue**. A newly watched repository starts from what exists now, so its history never floods in
as "new".

## What this is

A PersonalClaw **trigger source** (`provider.type: "trigger_source"`). It implements
`TriggerSourceProvider` from `personalclaw.sdk.trigger_source`: once enabled it runs its own
watch loop and emits typed events, and a `kind: event` trigger bound to one of them runs
whatever the trigger says (a saved prompt, a workflow, a notification, any other action).

It is deliberately **not**:

- **`issue-radar`** — that is a tool your agent calls to triage a repository's open issues on
  request. This app only notices that something new appeared, and hands it to your automations.
- **`git-repo`** — that indexes a repository's content into your knowledge library. This app
  reads no code at all.

## Events it emits

Core namespaces every event under this app's name, so bind triggers to:

| Trigger event | Fires when | `meta` |
| --- | --- | --- |
| `app:watched-source-github:new_release` | a watched repository publishes a new release | `repo`, `tag`, `url` |
| `app:watched-source-github:new_issue` | a new issue opens (pull requests are filtered out) | `repo`, `number`, `author`, `url` |

## Settings

| Key | Default | Meaning |
| --- | --- | --- |
| `repos` | *(none)* | Comma-separated `owner/name` list to watch. An entry that is not `owner/name` is skipped and reported by `personalclaw doctor`. |
| `token` | *(unset)* | A GitHub personal access token: needed for private repositories, and it raises the anonymous rate limit. Stored as a secret and never shown again. |
| `poll_interval_secs` | `300` | Seconds between polls (floored at 60). |
| `api_base` | `https://api.github.com` | Override for GitHub Enterprise. |
| `timeout_secs` | `20` | Seconds to wait for each GitHub response. |

## Network egress

Every request goes through PersonalClaw's egress guard (`personalclaw.sdk.net.fetch`, under the
connector policy plus your **Settings → Security → Network egress** rules). That means public
hosts only, the resolved address pinned, every redirect re-checked, and each request recorded
in the security event log. The app opens no socket of its own. A GitHub Enterprise server on
your own network is reachable only once you allow its host there. The token travels in an
`Authorization` header, never in a URL.

## Behaviour worth knowing

- **Push contract, self-owned loop.** Core never polls a source. Enabling the app starts its
  watch loop, and disabling it stops the loop.
- **First observation plants a high-water mark.** The first poll of each repository records
  what exists and emits nothing. The marks live in memory, so after a restart the first poll
  plants them again: a release or issue that appeared while the gateway was down is not
  replayed.
- **Degrade to silence.** A network, API or egress failure logs a warning and skips that round.
  The loop survives, and a watcher never takes the trigger bus down with it.

## Install

From the **Store**, install **GitHub Repo Watcher** and enable it, then add repositories in its
settings. The install dialog shows that it uses the network.

## Run the tests

```bash
./scripts/test-bundles watched-source-github
```

No network: the behaviour tests stub the fetch seam, and the egress tests replace the SDK's
guarded fetch.

## License

MIT — see `LICENSE`.
