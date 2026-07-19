# App Platform Architecture

How PersonalClaw runs apps: the install pipeline, the backend subprocess model,
the proxy and token model, permission enforcement, crons, and the MCP bridge.
This is the document to read if you want to understand what happens between
"user clicks Install" and "the app's page renders" — without reading core internals.

Core source pointers (for the curious): `personalclaw/apps/` (`app_manager.py`,
`backend_runtime.py`, `permissions.py`, `manifest.py`, `catalog.py`, `source.py`,
`app_crons.py`, `mcp_bridge.py`, `app_config.py`) and the REST layer in
`personalclaw/dashboard/handlers/apps.py`.

## The three app tiers

| Tier | Where it lives | Managed how |
|---|---|---|
| **Native** | inside the core package (`personalclaw/apps/native/`) | Seeded as installed on first boot, **locked on** — disable/uninstall/force-uninstall are refused; only settings are editable. |
| **First-party** | this repo's `apps/` directory | Surfaced in the Store via an always-present, read-only local source; user-managed like any other app. |
| **Third-party** | user-added sources (local dirs or git URLs) | Fully user-managed; full scanner gate. |

Installed apps live at `~/.personalclaw/apps/<name>/`. Each app's mutable state
lives in `~/.personalclaw/apps/<name>/data/` — the one directory preserved across
updates.

## Install pipeline

`install(source)` in `apps/app_manager.py` — every step in order:

1. **Source resolution** (`apps/source.py`): a local directory resolves in place
   (`origin="local"`); a git URL is shallow-cloned (`--depth 1`, 120s timeout)
   into a temp dir (`origin="external"`), `.git` metadata stripped. No hooks run
   here.
2. **Quarantine staging**: the source is copied into
   `~/.personalclaw/apps/.quarantine/<name>` FIRST — dangerous content never
   touches the live tree. The manifest is validated from the staged copy
   (staged copy is the source of truth).
3. **SkillScanner gate** (`supply_chain.py`): the staged content is scanned at a
   trust tier derived from origin (`builtin` → advisory-only, `registry` →
   official, `local`/`external` → community, the full gate). Verdicts:
   - `clean` — proceeds.
   - `warning` — install stops with `needs_consent` (the API returns **409**);
     re-submitting with `confirm: true` proceeds.
   - `dangerous` — **terminal refusal, non-overridable**. `confirm` does NOT
     bypass it.
4. **Platform gate**: an app with `platform.installMode: "client"` or whose
   `platform.os` list excludes this server's OS is NOT installed; the result
   carries `needs_client_install` + the manifest's copy-paste `clientInstall`
   one-liner (surfaced by the Store, never auto-run — it executes on the user's
   machine, outside the scanner).
5. **Commit**: staged tree moves to `~/.personalclaw/apps/<name>/`; the `data/`
   dir is created before any hook runs.
6. **Python dependencies**: declared `dependencies.pythonDependencies` are
   pip-installed into the shared core venv (600s cap; core ships lean, the app
   brings its heavy libs). If anything new was actually installed, the result
   carries `restart_required: true` — the running gateway can't import a module
   set it didn't start with.
7. **`setup.onInstall` hook**: a timeout-bounded shell subprocess (60s) in the
   app dir. Runs only after the scanner gate passed. A failing hook rolls the
   commit back (app dir removed).
8. **Registration**: `installed.json` written; providers registered; app-owned
   prompts seeded (idempotent, non-clobbering); dependency ledger records shared
   deps; **MCP servers registered**; **backend started**. Quarantine is GC'd.

Every lifecycle action is audited to the Security Event Log.

### Update, uninstall, deactivate

- **Update** (`update()`): atomic with rollback. New code is staged + re-scanned
  (same gate — an update is a fresh fetch of mutable content), old `data/` is
  preserved into the new tree, live dir swaps through a `.{name}.rollback` dir,
  `setup.onUpdate` runs, and ANY failure restores the old app. A leftover
  rollback dir signals a mid-swap crash; `recover_interrupted_updates()`
  reconciles it at boot.
- **Uninstall = deactivate**: `uninstall()` keeps the files. Providers
  deregister, the backend stops, MCP servers drop, crons reconcile away, and
  `installed.json.enabled` flips false — instant re-activation, `data/` intact.
- **Force uninstall** is the destructive path (Advanced → Force uninstall):
  `setup.onUninstall` runs, then files are removed. The dependency ledger
  classifies each shared dependency as removable / shared / user-installed so an
  uninstall never rips a dep out from under another app.
- **Enable/disable** run `setup.onEnable` / `setup.onDisable` (bounded by
  `onEnableTimeout` / `onDisableTimeout`, default 30s) and flip provider,
  prompt, MCP, backend, and cron registration together. Native apps refuse
  disable.

## Backend subprocess model

An app that declares `backend.entryPoint` gets an isolated **subprocess**, not an
in-process mount (`apps/backend_runtime.py`):

- **Launcher**: `backend.type` selects it — `python`/`asgi` → the core venv's
  Python, `node` → `node`; empty auto-detects from the entry suffix (`.py`,
  `.js`/`.mjs`/`.cjs`). The entry point must resolve INSIDE the app dir
  (containment check).
- **Port**: `backend.port: "auto"` (the default) binds an OS-assigned free
  localhost port. The chosen port is handed to the process via the **`PORT`**
  env var — the conventional contract; the backend must listen on it.
- **Identity + storage env**: `PERSONALCLAW_APP_NAME` is always set.
  `PERSONALCLAW_APP_DATA_DIR` (pointing at `~/.personalclaw/apps/<name>/data/`)
  is set **only if the app declares the `storage` permission** — without it the
  backend has no sanctioned place to persist.
- **Health**: `backend.healthCheck` (default `/health`) names the endpoint the
  platform probes.
- **Watchdog**: a daemon thread sweeps every **30 seconds** and relaunches any
  enabled app's backend that died (`start_backend_watchdog`). Set
  `PERSONALCLAW_SKIP_APP_BACKENDS=1` to suppress backend management (test
  isolation).
- **Orphan reaping**: backends run on auto-ports, so a fresh gateway can't
  reclaim a crashed predecessor's backends by port. On boot it scans the OS
  process table for live processes running THIS app's exact entry path and
  SIGTERMs the truly orphaned ones — **only processes re-parented to PID 1**; a
  process with a live parent belongs to another supervisor and is left alone.
  Path-identity (not recorded PIDs) means no recycled-PID risk.
- **Shutdown**: graceful `terminate()` with a 5s wait, then `kill()`.

## Reverse proxy and the app token

All traffic between the browser (or the app's UI) and an app backend goes
through the gateway proxy: `/apps/{name}/api/{tail}` → `http://127.0.0.1:<port>/<tail>`
(any method; 404 not installed, 403 disabled, 502 backend down; 30s round-trip cap).

The security contract at that hop:

- **Credential stripping**: the owner's session credential (cookie AND
  `Authorization` header) plus any inbound `X-PersonalClaw-App` header are
  removed before forwarding. An app backend must never receive the owner's
  token — it could replay it against the full gateway API.
- **App-scoped token injection**: the proxy attaches a fresh
  `Authorization: Bearer <token>` minted with an `app` claim naming this app,
  plus `X-PersonalClaw-App: <name>`. The token is short-lived (**1 hour TTL**)
  and bound to the current owner user — an app never exceeds the owner's reach,
  and a leaked token has a small blast radius.
- **Minting** (`POST /api/apps/{name}/token`): only a NON-app request (the
  owner/dashboard) may mint; a request already carrying an app identity gets
  403 — no cross-app escalation. The frontend SDK mints on mount and re-mints
  on expiry.

When an app-scoped token is presented anywhere on the gateway, the auth
middleware sets the request's app identity, and enforcement (below) gates on it.
The app claim is adopted in ALL auth modes, including `AUTH_MODE=none`.

## Permission enforcement

Declared in the manifest `permissions` block; enforced server-side per
`apps/permissions.py`. Enforcement status of every permission:

| Permission | Declares | Enforced where | Status |
|---|---|---|---|
| `api` (list of path prefixes) | which gateway API paths the app may call | app-permission middleware in the gateway server — an app-identified request to an undeclared path is rejected **403 before the handler runs**. Matching is prefix-based on the pathname only (query stripped); `*` suffix wildcards supported. The app's own proxy route `/apps/{name}/api/*` is always allowed (that's the app talking to itself). No declared `api` = no gateway API at all (deny by default). | **Enforced** |
| `events` (list of event types) | which WebSocket events the app's connection receives | the WS fan-out filter — an app-scoped WS connection only receives events matching its declared set | **Enforced** |
| `mcpTools` (list of tool names) | which MCP tools the app may invoke directly | the direct tool-invoke endpoint | **Enforced** |
| `memory` (`""` / `"app-scoped"` / `"shared"`) | memory tier access | app-permission middleware gates any `/api/memory` path; empty = none, `app-scoped` = own scope only, `shared` = both | **Enforced** |
| `cron` (bool) | may register manifest crons | cron reconciliation registers an app's crons only when held; without it the declaration is inert | **Enforced** |
| `storage` (bool) | gets a persistent data dir | the backend launcher hands `PERSONALCLAW_APP_DATA_DIR` only when held | **Enforced** |
| `agent` (bool) | may run background agent tasks | the app agent-run endpoint (`POST /api/apps/{name}/agent-run`) — checked independently server-side and client-side in the SDK (two independent gates) | **Enforced** |
| `network` (bool) | intends to reach the network | **DECLARATION-ONLY — unenforced by design.** An app backend is an OS subprocess with its own network stack; there is no in-process egress hook the gateway can intercept. The flag records intent so the Store's install-consent surface can show it ("network access: yes/no"); a future OS-level isolation layer (cgroups/nftables/seccomp) may enforce it. Every gateway-MEDIATED reach is still bounded by `api`. Treat `network: true` as an honest declaration, not a security boundary. | **Declared-only** |

The Store shows the full permission set + declared crons as an install-consent
surface BEFORE install, so the user sees what they're granting.

## App crons

Manifest `crons` entries are scheduled agent jobs (`apps/app_crons.py`):

- Only honored when the app holds the `cron` permission.
- Registered as jobs named `app:<app-name>:<cron-name>`, tagged
  `created_by="app:<name>"`.
- **Reconciliation is declarative and idempotent**: the desired set (enabled
  apps × permitted manifest crons) is diffed against registered `app:*` jobs
  and added/pruned to match. It runs **at gateway boot** and again **on every
  lifecycle transition** (install / enable / disable / uninstall / update) — so
  a disabled app's cron stops immediately, not at the next restart.
- App crons run **headless**: `approval_mode="auto"` (an unattended run can't
  wedge on a human) and always `silent=True` (there is no owner conversation to
  deliver into — an app surfaces results itself, via its backend or the
  `send_message` tool). The manifest `silent` flag is advisory; silent is
  already the effective behavior.

## MCP bridge

An app may ship its own MCP server(s) via manifest `mcpServers`
(`apps/mcp_bridge.py`):

- On install/enable the entries are written into the live MCP config
  (`~/.personalclaw/mcp.json`), **namespaced `{app}:{server}`** so two apps (or
  an app and the user) can't collide, and so deregistration removes exactly this
  app's servers.
- **cwd injection**: a stdio server shipped inside the app package (relative
  `command`/`args` like `backend/mcp_server.py`) gets `cwd=<app dir>` injected —
  the MCP client doesn't chdir per server, so without this a relative path would
  resolve against the gateway's cwd and never start. A spec that already sets an
  absolute cwd (or a remote `url` server) is left untouched.
- On disable/uninstall the app's namespaced entries are removed.

## UI contribution serving

An app with a `ui` block contributes an ESM bundle the dashboard code-splits in:

- Assets are served from `/apps/{name}/ui/{path}` — confined to the app's `ui/`
  dir with a path-traversal guard; only **enabled** apps serve UI.
- The host calls the bundle's exported `mount` function; the frontend SDK
  (`@personalclaw/app-sdk`, resolved by the host) provides `createAppApi`
  (fetch bound to the app token + declared API paths), `createAppEvents`
  (WS scoped to `permissions.events`), agent-task helpers, theme access, and
  `notify`. See the [app creation guide](app-creation-guide.md).

## Per-app configuration

Two flavors, both persisted in `~/.personalclaw/apps/{name}/data/config.json`
(inside `data/` so config survives updates):

- **Provider apps** declare `provider.settingsSchema`; the Settings UI renders a
  Configure form from it, and the provider reads values back through the SDK's
  `ProviderSettings`.
- **Backend/UI apps** declare `setup.configSchema`; `GET/PUT
  /api/apps/{name}/config` validates against it (required keys, declared types,
  enum membership; unknown keys rejected).

Fields tagged `"sensitive": true` in `x-meta` are treated as secrets by the UI.
