# App Creation Guide

How to build a PersonalClaw app: the manifest, the capability types, the backend
contract, UI contribution, permissions, and testing. The worked example
throughout is `third-party-apps/demo-dashboard` — the permanent integration
fixture that exercises every platform surface (backend, UI, storage, api,
events, cron, agent, mcpServers).

An app is a **directory** with an `app.json` manifest at its root. Nothing else
is mandatory — everything beyond the manifest is opt-in.

```
my-app/
├── app.json           # the manifest (required)
├── provider.py        # if you contribute a provider
├── backend/server.py  # if you ship a backend
├── ui/index.mjs       # if you contribute a UI page
├── assets/hero.png    # optional store banner
├── setup.sh           # optional install hook target
├── LICENSE
└── test_provider.py   # your tests
```

## Quickstart: minutes to first run

Prerequisites: PersonalClaw installed (`pip install personalclaw`), `pytest` available, and
a gateway running (`personalclaw gateway`).

**1 — see what you can build.** The type table is derived from the running build's provider
registry, so it is always the truth about this version:

```bash
personalclaw app new --list-types
```

**2 — generate an app.** Pick a type from that table (`tool` is the simplest):

```bash
personalclaw app new my-tool --type tool
```

You get a complete, installable app: `app.json` validated against core's own manifest
parser, a provider stub implementing that type's SDK contract with real signatures,
`app_cli.py` (the `setup`/`doctor` seams), a passing `test_provider.py`, a `README.md`, and
an MIT `LICENSE`. No `permissions` block — add only what your provider actually uses, since
the Store shows declared permissions as the install-consent surface.

**3 — run its tests.** They pass as generated, with no network, no credentials and no
gateway:

```bash
python -m pytest my-tool -q
```

**4 — point a shell at your gateway.** `personalclaw token` prints one line: a dashboard URL
with the token in the query string. Split it into the two pieces the next step needs:

```bash
TOKEN_URL="$(personalclaw token)"
export PERSONALCLAW_URL="${TOKEN_URL%%\?*}"
export PERSONALCLAW_TOKEN="${TOKEN_URL#*token=}"
```

`personalclaw token` finds the gateway by port, and it resolves that port from `--port`, then
`PERSONALCLAW_PORT`, then `dashboard.url` in your config — never from the running process. So if
you started the gateway on some other port, pass the same one (`personalclaw token --port 8123`).
Skip that and `token` prints an error where the URL should be, both `export`s store the error
text, and step 5 dies inside curl (`curl: (3) bad range in URL`) rather than telling you about
the port.

The gateway takes the owner token as a `?token=` **query parameter**. An
`Authorization: Bearer` header is only accepted for app-scoped narrowing tokens, so using
one here answers `{"error": "Token required"}`.

**5 — install it from that local path and enable it.**

```bash
curl -sS -X POST "$PERSONALCLAW_URL/api/apps?token=$PERSONALCLAW_TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"source\": \"$PWD/my-tool\", \"confirm\": true}"
curl -sS -X POST "$PERSONALCLAW_URL/api/apps/my-tool/enable?token=$PERSONALCLAW_TOKEN"
```

Prefer clicking? **Store → Add source → local path**, point it at `my-tool`, then install
and enable. Same install path and same supply-chain scan gate — there is only one.

**6 — confirm it is live.**

```bash
curl -sS "$PERSONALCLAW_URL/api/apps/my-tool?token=$PERSONALCLAW_TOKEN"
```

In the response, the `installed` block reports `"enabled": true` and `manifest.provider.type`
is the type you scaffolded — your provider is registered. `personalclaw doctor` now prints a
`my-tool` section, and the line under it comes from your `app_cli.py`.

That is first run. **Measured: 6.0-7.2 s of wall clock across steps 1-6** for a genuine first
run, out of an empty directory against a freshly-homed gateway, to an enabled app whose provider
is registered — and 2.2-2.7 s on repeats once Python's imports are warm. The unit is seconds
either way. Filling in the stub is the rest of this guide.

Prefer to fork a repo instead of generating? `personalclaw app new --from-template` fetches
[`personalclaw/app-template`](https://github.com/personalclaw/app-template) — the same
`--type tool` output, plus CI and a clone-to-installed README.

## The manifest (`app.json`)

The full field set, as parsed by the platform (`personalclaw/apps/manifest.py`).
Unknown fields are preserved for forward compatibility, never fatal.

### Identity (required)

```json
{
  "name": "my-app",              // unique id, kebab-case (validated)
  "version": "1.0.0",            // semver (validated)
  "displayName": "My App",
  "description": "One or two sentences shown on the Store card."
}
```

### Recommended metadata

```json
{
  "icon": "Sparkles",            // a lucide icon NAME (never an emoji)
  "heroImage": "assets/hero.svg",// optional banner, path relative to app dir
  "author": "You",
  "license": "MIT",
  "tags": ["demo", "productivity"]
}
```

The hero image is inlined as a data URI by the catalog (traversal-guarded,
image-types-only, ~1.5MB cap) so it renders for installed AND not-yet-installed
entries.

### Provider (capability contribution)

An app that plugs a capability into core declares a `provider` (or several via
`providers` — a list of the same shape):

```json
"provider": {
  "type": "search",                          // see capability types below
  "implementation": "provider:create_provider", // module.path:factory_fn, relative to the app dir
  "multiInstance": true,                     // user may add several instances (e.g. two endpoints)
  "capabilities": ["search"],                // what this provider can do
  "entity": "",                              // optional sub-grouping within a type
  "settingsSchema": { ... }                  // JSON Schema (Draft-07 + x-meta) for the Configure form
}
```

The factory receives the app's current config dict and returns a provider
instance implementing the relevant SDK contract.

`settingsSchema` properties support `x-meta` per field:
`label`, `help`, `sensitive: true` (secret handling), `tags: ["advanced"]`
(collapsed by default), and `enum` for dropdowns. Values the user saves land in
`~/.personalclaw/apps/<name>/data/config.json` and are read back via
`personalclaw.sdk.settings.ProviderSettings`.

### Backend

```json
"backend": {
  "entryPoint": "backend/server.py",  // must live inside the app dir
  "type": "python",                   // "python" | "asgi" | "node" | "" (auto-detect by suffix)
  "port": "auto",                     // "auto" (recommended) or a fixed port
  "healthCheck": "/health"
}
```

### UI

```json
"ui": {
  "entry": "ui/index.mjs",            // ESM bundle, relative to app dir
  "pages": [{
    "route": "/apps/my-app",          // required
    "label": "My App",                // required (sidebar text)
    "icon": "LayoutDashboard",        // lucide name (or "iconUrl" for an image)
    "entryPoint": "index.mjs",        // bundle path
    "mountFunction": "mount"          // exported function (default "mount")
  }],
  "sidebar": { "section": "Apps", "order": 10 }
}
```

### Permissions

```json
"permissions": {
  "api": ["/api/apps/my-app", "/api/projects"],  // gateway API path prefixes ("*" wildcard suffix ok)
  "events": ["refresh", "knowledge"],            // WS event types your UI may receive
  "mcpTools": [],                                // MCP tools the app may invoke directly
  "storage": true,                               // get a persistent DATA_DIR
  "network": false,                              // DECLARED intent only — see note
  "memory": "",                                  // "" | "app-scoped" | "shared"
  "cron": true,                                  // may register manifest crons
  "agent": true                                  // may run background agent tasks
}
```

Declare the **minimum** you need — the Store shows this block to the user as the
install-consent surface. All of these are enforced server-side EXCEPT `network`,
which is declaration-only by design (a backend subprocess has its own OS network
stack; the flag discloses intent to the user rather than fencing it). See the
[permission enforcement table](platform-architecture.md#permission-enforcement).

### Crons

```json
"crons": [{
  "name": "heartbeat",             // required
  "cron_expr": "*/30 * * * *",     // OR "every": <seconds> — one is required
  "agent": "",                     // agent to run (empty = default)
  "message": "Record a heartbeat timestamp",
  "persistent_session": false,     // carry context between runs (default true)
  "silent": true                   // advisory; app crons are always headless/silent
}]
```

Requires the `cron` permission. Jobs register as `app:<app>:<cron>` and
reconcile on boot + every lifecycle transition. They run unattended
(auto-approve, no owner-channel delivery) — surface results through your backend
or the `send_message` tool.

### MCP servers

```json
"mcpServers": {
  "my-echo": { "command": "python3", "args": ["backend/mcp_server.py"] }
}
```

Registered into the live MCP config namespaced `my-app:my-echo`; a relative
stdio command gets `cwd=<app dir>` injected so it spawns correctly.

### Setup hooks and config schema

```json
"setup": {
  "onInstall": "bash setup.sh",    // bounded shell subprocess in the app dir (60s)
  "onUpdate": "bash setup.sh",
  "onUninstall": "",
  "onEnable": "", "onDisable": "",
  "onEnableTimeout": 60,           // per-app override (default 30)
  "configSchema": {                // user-editable config for backend/UI apps
    "type": "object",
    "properties": {
      "label": { "type": "string", "default": "My Dashboard",
                 "x-meta": { "label": "Dashboard label", "help": "Shown as the header." } }
    }
  }
}
```

Hooks run only after the security scanner passes. A failing `onInstall` rolls
the install back.

### Dependencies

```json
"dependencies": {
  "pythonDependencies": ["faster-whisper>=1.0"]  // pip specs, installed into the shared venv
}
```

Core ships lean — the app that needs a heavy library declares it here. A
newly-installed dep needs a gateway restart to become importable (the install
result reports `restart_required`). There is also a `marketplace` block
(mcp/skills/agents ids with `managedBy: "gateway" | "app"`) for
marketplace-managed dependencies.

### Platform

```json
"platform": {
  "os": ["macos", "linux"],        // default
  "installMode": "server",         // "server" | "client"
  "clientInstall": { "shell": "curl ... | sh", "postInstall": "open ..." }
}
```

`installMode: "client"` (or an OS mismatch) makes the Store show the copy-paste
`clientInstall` one-liner instead of installing on the server.

### Quality declaration (optional — and CI-enforced here)

```json
"quality": {
  "tested": true,                  // the bundle ships tests AND they pass
  "designSystem": "v2",            // "v2" | "legacy" | "n/a"
  "a11y": true                     // an axe scan of this version found nothing
}
```

The Store renders this as a badge row, so in this repo it is a promise, not
decoration: the `quality-declarations` CI job runs core's verifier
(`python -m personalclaw.apps.quality .`) and **exits 1 on a claim the bundle
cannot back**. What each claim must show:

| Claim | Evidence CI checks |
|---|---|
| `tested: true` | at least one `test_*.py` (root or `tests/`) **and** `python -m pytest <bundle>` passes |
| `designSystem: "v2"` | every `*.ts`/`*.tsx` in the bundle passes token-lint — the same rule the host frontend is held to (`node_modules`/`dist`, `*.test.tsx` and `*.config.ts` are excluded) |
| `a11y: true` | the bundle ships `a11y/axe-report.json` — `{"appVersion", "tool", "violations": []}` — whose `appVersion` equals the manifest's and whose `violations` is empty |

Three things worth knowing before you declare:

- **Absent is not a claim, and an honest miss is never punished.** Omit an axis,
  or declare `false` / `"legacy"` / `"n/a"`, and CI asks nothing of it. Only
  `true` / `"v2"` is a claim.
- **A claim with nothing to check is a violation, not a free pass** —
  `designSystem: "v2"` with no frontend source, or `a11y: true` with no report,
  would badge a check that never ran. A backend-only app declares `"n/a"`.
- **CI verifies the axe report; it cannot produce one.** There is no browser and
  no host SPA build in this repo, so `a11y: true` means your own harness
  committed the artifact for *this* version — last release's clean scan cannot
  launder this one.

Note: legacy manifest fields `agents`, `skills`, `sops` (and the old
`backend.hooks`/`backend.routes`) were **stripped** — they parse into the
forward-compat `extra` bag but have no runtime consumer. Don't use them. The
`native` flag is reserved for core-shipped apps; never set it.

## Capability types

`provider.type` must be one of the registered types. The ones you'll actually
build, each with its SDK contract (apps import ONLY `personalclaw.sdk.*` — the
boundary is lint-enforced):

| Type | SDK contract | What it plugs into | Reference app |
|---|---|---|---|
| `model` | `personalclaw.sdk.model` (chat LLMs), `sdk.stt`, `sdk.tts`, `sdk.diarization`, `sdk.embedding`, `sdk.image`, `sdk.local_model` (download/manage local models) | Settings → Models; bound per use-case (chat/background/embedding/stt/tts/…) | `anthropic-models` (branded API), `openai-compatible` (generic endpoint), `claude-subscription` (rides a CLI's subscription sign-in, no API key), `faster-whisper` (local STT), `sentence-transformers` (local embeddings) |
| `search` | `personalclaw.sdk.search` `SearchProvider` | Settings → Search; the `web_search` tool | `brave-search`, `duckduckgo-search` (keyless) |
| `agent` | `personalclaw.sdk.acp` (ACP agent bundles) | the Agents list | `claude-code-agent`, `codex-agent` |
| `tool` | `personalclaw.sdk.tool` (+ `sdk.mcp`) | the agent tool layer | `mcp-tools`, `web-tools` |
| `channel` | `personalclaw.sdk.channel` `ChannelTransportProvider` + `ChannelDelivery` | messaging channels (inbound + outbound delivery) | `slack-channel` |
| `action` | `personalclaw.sdk.action` | trigger/schedule action providers | `webhook-action` |
| `skills` | `personalclaw.sdk.skill` `SkillsMarketplace` | Skills → Browse (read-only search + fetch) | `skills-sh` |

Thin branded model apps can use `personalclaw.sdk.provider_helpers`
(`register_branded_app`) — a few lines wrapping a protocol client core already
ships (Anthropic Messages, OpenAI-compatible Chat Completions).

A vendor that bills by **subscription** has no API key to configure: the user
already signed that vendor's own CLI in, and the token sits in a store the CLI
owns. Declare a `SubscriptionSource` (its paths, the key walk to the token, the
expiry stamp, and your own login sentence) and name it in the spec's
`credential_source` with an empty `api_key_env`. Core then reads that store
**read-only** as one fixed hop of the credential order — below an explicit
credential or configured key, above the env — and derives the extensions-list
availability probe from the same declaration, so a not-signed-in CLI greys your
app out with your own hint instead of failing at the wire. Never write, refresh
or repair another tool's credential store. Reference app: `claude-subscription`.

An app doesn't have to contribute a provider at all: `growth`, `minutes`, and
`demo-dashboard` are pure backend+UI apps.

## Backend contract

Your backend is a plain HTTP server, launched as a subprocess. The whole
contract, as exercised by `third-party-apps/demo-dashboard/backend/server.py`:

```python
import os
from pathlib import Path

PORT = int(os.environ.get("PORT", "0"))                      # listen HERE
APP_NAME = os.environ.get("PERSONALCLAW_APP_NAME", "my-app")
DATA_DIR = Path(os.environ.get("PERSONALCLAW_APP_DATA_DIR", "/tmp/my-app"))  # only set with storage:true
```

- **Startup**: bind `127.0.0.1:$PORT`. Any framework works (demo-dashboard uses
  aiohttp); node backends are equally supported.
- **Routes**: serve them bare (e.g. `/counters`). The gateway proxies
  `/apps/my-app/api/counters` → your `/counters`. Your UI reaches them via the
  SDK's `api.backendBase`.
- **Health**: implement the endpoint you declared (`/health` by default,
  returning 200). The 30s watchdog relaunches you if you crash.
- **Persistence**: write ONLY under `DATA_DIR` — it survives updates; anything
  else in your app dir is replaced wholesale on update.
- **Inbound identity**: each proxied request arrives with a fresh app-scoped
  bearer token and `X-PersonalClaw-App: <name>`; the owner's credentials never
  reach you. If your backend calls back into the gateway API, use that token —
  it is bounded by your declared `api` permissions.

## UI contribution

Your `ui` entry is an ESM bundle exporting a mount function. The host resolves
bare imports of `react` and `@personalclaw/app-sdk` for you (no bundling them).

```js
import { createAppApi, createAppEvents, notify } from '@personalclaw/app-sdk'

export function mount(el, ctx) {
  // ctx = { name, permissions, host }
  const api = createAppApi(ctx)
  async function load() {
    // your own backend (proxied):
    const counters = await api.get(`${api.backendBase}/counters`)
    // declared core APIs:
    const projects = await api.get('/api/projects')
    // ... render into el ...
  }
  // events (filtered to permissions.events):
  const off = createAppEvents(ctx, (e) => { load() })
  load().then(() => notify('Loaded', 'success'))
  return () => off()   // cleanup
}
```

Two mount shapes are supported: a React component shape (probed first) and the
imperative `(el, ctx)` shape shown above — demo-dashboard uses the imperative
one. The SDK also provides `createAgentTask` (background agent runs, gated by
the `agent` permission), `useTheme`/`readAppTheme`, and React-hook variants
(`useAppApi`, `useAppEvents`) under an `AppApiProvider`.

Read your saved `configSchema` values via your own detail endpoint
(`GET /api/apps/<name>` — declare it in `permissions.api`), like demo-dashboard
does for its `label` and `refresh_interval_s`.

## Testing

- **Provider apps** ship a `test_provider.py` next to the provider (every
  bundled app has one — copy a sibling's structure). Tests import your provider
  through the SDK contracts.
- **Backend apps** ship a `test_server.py` (see `growth`, `minutes`,
  demo-dashboard's platform tests) exercising routes against a temp `DATA_DIR`.
  **Never let a test touch real user state** — monkeypatch the data dir /
  `PERSONALCLAW_HOME` to `tmp_path`.
- **End-to-end**: install your app from a local source (below) and drive it in
  the real UI. Set `PERSONALCLAW_SKIP_APP_BACKENDS=1` in unit tests that don't
  want backend subprocesses.

## Installing your app while developing

1. Apps page → Store → add the parent directory of your app as a
   **local source** (or `POST /api/apps/local-sources`).
2. Your app appears in the Store; install it. The install runs the full
   quarantine → scan → hook → register pipeline.
3. Iterate: repo edits do NOT reach the installed copy at
   `~/.personalclaw/apps/<name>/`. Push changes with
   `POST /api/apps/<name>/update {"source": "/path/to/my-app", "confirm": true}`.

See [third-party-install.md](third-party-install.md) for the user-facing
install story.
