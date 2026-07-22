# Installing Third-Party Apps

How to add app sources, what the install flow does, and what to expect from the
security gate. For building an app, see the
[app creation guide](app-creation-guide.md); for the machinery underneath, the
[platform architecture](platform-architecture.md).

## App sources

The Store lists installable apps from **sources**. Two kinds, persisted in
`~/.personalclaw/app-sources.json`:

```json
{
  "git":   ["https://github.com/someone/their-personalclaw-app"],
  "local": ["/path/to/a/directory/of/apps"]
}
```

- A **local source** is a directory *containing app subdirectories* (each with
  an `app.json`) — e.g. this repo's `apps/` tree, or wherever you cloned a
  third-party collection. The first-party `apps/` directory is an always-present,
  read-only default source; user-added local sources sit alongside it.
- A **git source** is a repository URL. The catalog lists it without cloning —
  the shallow clone (`--depth 1`, bounded) happens at install time, behind the
  scanner gate. `.git` metadata is stripped from the installed copy.

Manage sources from the Apps page → Store, or via the API:

```
GET/POST/DELETE /api/apps/sources          # git URLs
GET/POST/DELETE /api/apps/local-sources    # local directories
```

## Installing from the Store

1. Open the Apps page → **Store**. Every not-yet-installed app from native
   leftovers, first-party, and your sources is listed with its manifest
   metadata — including its **declared permissions and crons**, shown BEFORE
   install so you can review what you're granting (pay attention to
   `network: true`, which is disclosed but not technically enforced — see the
   [permission table](platform-architecture.md#permission-enforcement)).
2. Click **Install**. Equivalent API:

   ```
   POST /api/apps {"source": "<local path or git URL>", "confirm": false}
   ```

3. The pipeline runs: **quarantine staging → manifest validation → security
   scan → (consent) → pip deps → onInstall hook → registration → backend
   start**. Nothing from the source touches the live tree until the gate passes.

### The scan gate

Third-party content is scanned at the **community** trust tier (the full gate):

| Verdict | What happens |
|---|---|
| `clean` | Installs. |
| `warning` | Install stops (HTTP 409, `needs_consent: true`) with the findings listed. The UI shows a consent dialog; confirming re-submits with `confirm: true`. |
| `dangerous` | **Refused, terminally.** No consent flag overrides it. |

This gate is where the install pipeline sits in PersonalClaw's overall trust model
(the "install pipeline ↔ sources" boundary). For the full picture — including the
OWASP Agentic Top-10 mapping and an honest statement of what is *not* enforced
(e.g. an app's `network` permission is declaration-only) — see the core
[threat model](https://github.com/PersonalClaw/PersonalClaw/blob/main/docs/security/threat-model.md).
To report a security issue in an app bundle or the scanner, see [`SECURITY.md`](../SECURITY.md).

Setup hooks are real code execution — that's exactly why they only run after
the gate, and never auto-forced for an unattended/agent-initiated install.

### Other install outcomes

- `restart_required: true` — the app declared Python dependencies that were
  newly installed; restart the gateway so it can import them.
- `needs_client_install` (HTTP 200) — the app declares
  `installMode: "client"` or doesn't support the server's OS; the response
  carries a copy-paste one-liner to run on YOUR machine. Nothing was installed
  on the server, and the one-liner is never auto-executed.
- `already installed` — use update instead.

## What happens on update

Updates re-fetch the source and run the SAME scan gate (mutable content gets
re-scanned every time — a now-dangerous update never lands):

```
POST /api/apps/{name}/update {"source": "<path or URL>", "confirm": true}
```

The swap is atomic with rollback: your app's `data/` directory (its config and
state) is preserved into the new version; any failure — scan refusal, a failing
`onUpdate` hook — restores the previous version untouched. An update
interrupted by a crash is reconciled at the next gateway start.

There are **no automatic app-update checks** — updating an app is an explicit
per-app action through the Store. (The Updates settings page covers core
self-update only.)

## What happens on uninstall

- **Uninstall** (`DELETE /api/apps/{name}`) is a *deactivation*: providers
  deregister, the backend stops, MCP servers and crons drop — but files and
  `data/` stay on disk, so re-enabling is instant and lossless.
- **Force uninstall** (Advanced) actually removes the files, after running the
  app's `onUninstall` hook. Before it does, the dependency ledger classifies
  each shared dependency (removable / shared with another app /
  user-installed) so removing one app never breaks another.
- **Native apps** (the core-shipped baseline set) are locked on: disable and
  both uninstall flavors are refused; only their settings are editable.

## Where things live

| What | Path |
|---|---|
| Installed apps | `~/.personalclaw/apps/<name>/` |
| App state + config (survives updates) | `~/.personalclaw/apps/<name>/data/` |
| Sources list | `~/.personalclaw/app-sources.json` |
| Quarantine (transient) | `~/.personalclaw/apps/.quarantine/` |
| App-registered MCP servers | `~/.personalclaw/mcp.json` (namespaced `{app}:{server}`) |

## A worked example

`third-party-apps/demo-dashboard` in this workspace is a complete third-party
fixture (backend + UI + storage + api/events/cron/agent permissions + an MCP
server). To try the whole flow end-to-end:

1. Add `<workspace>/third-party-apps` as a local source.
2. Install **Demo Dashboard (3P)** from the Store — review the consent surface
   (note it declares `network: true` and a half-hourly heartbeat cron).
3. Open its sidebar page: counters served by its own backend through the proxy,
   core project/task counts through its declared `api` paths, live refresh via
   its declared `events`.
4. Uninstall it; confirm the page disappears and the cron is pruned — then
   re-install and see its counter state survived (that's `data/`).
