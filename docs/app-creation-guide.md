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

Prerequisites: PersonalClaw installed (`pip install personalclaw`), `pytest` and `uv`
available, and a gateway running (`personalclaw gateway`).

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

**3 — run its tests.** Use the same per-bundle runner as CI. It installs
`dependencies.pythonDependencies` from the bundle's `app.json` into the active Python
environment with `uv` (your test environment — a gateway installs them into
`<home>/app-python` instead; see [Dependencies](#dependencies)), then runs pytest. Generated
apps with no dependencies still run with no network, credentials, or gateway:

```bash
./scripts/test-bundles my-tool
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

**5 — review it, install it from that local path, and enable it.** An install is two
calls: the review says what the app gets and what the security scanner found, and installs
nothing; the install carries the review's `consent` digest, so it installs exactly the bytes
you reviewed (anything else — `"confirm": true` included — answers 409 with a fresh review).

```bash
review="$(curl -sS -X POST "$PERSONALCLAW_URL/api/apps/preview?token=$PERSONALCLAW_TOKEN" \
  -H 'Content-Type: application/json' -d "{\"source\": \"$PWD/my-tool\"}")"
echo "$review" | python3 -m json.tool      # read it: permissions, jobs, packages, the scan
consent="$(echo "$review" | python3 -c 'import json, sys; print(json.load(sys.stdin)["consent"])')"
curl -sS -X POST "$PERSONALCLAW_URL/api/apps?token=$PERSONALCLAW_TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"source\": \"$PWD/my-tool\", \"consent\": \"$consent\"}"
curl -sS -X POST "$PERSONALCLAW_URL/api/apps/my-tool/enable?token=$PERSONALCLAW_TOKEN"
```

Prefer clicking? **Store → Add source → local path**, point it at `my-tool`, then install
and enable. Same review, same supply-chain scan gate, same consent — there is only one path.

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

The Store card renders `description` in a two-line clamp and appends ` · by <author>`
inside it, so **the lead sentence is all the grid shows** — the rest is still in the DOM
for search and renders in full in the detail panel, but nobody reads it on the card. Lead
with a sentence that stands alone in **90 characters or fewer** and put the detail in the
sentences after it; end the description with a full stop. A longer lead is cut mid-phrase
on the card, which reads as a rendering bug rather than a summary. CI's `store-card-copy`
job enforces both (`.github/scripts/check_store_card_copy.py` — the 90 is measured against
the real grid, not a style preference).

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

#### Advanced and required — the one convention

The host renders every app's Configure form with the same widget, so the whole
catalog has to mean the same thing by `advanced` and `required` — otherwise
identical forms look arbitrary. Three rules, enforced by
`.github/scripts/check_settings_schema_posture.py` in CI:

1. **Optional tuning fields fold.** A field a first-run user never needs —
   `timeout_secs`, an optional `endpoint`/`base_url` override on a hosted
   provider, a `*_bin` binary path — carries `tags: ["advanced"]` so it sits
   behind the Advanced disclosure.
2. **Required fields never fold.** Anything in the schema's `required` array is
   first-run configuration and must not be tagged `advanced`. Requiredness, not
   the field name, is the discriminator: `endpoint` on a self-hosted app
   (ollama, openai-compatible, vllm, searxng) is the app's identity, belongs in
   `required`, and stays on the first screen — while the same field name on a
   hosted provider is an optional override and folds.
3. **`api_key` is never `required`.** Every key field falls back to its env var
   (say which one in `help`); the form stays mountable on a machine where the
   key arrives via the environment. This is a deliberate convention, not an
   oversight — do not "fix" it per app.

Judgment beyond these (is a model picker advanced? is a region first-run?) is
yours — follow the nearest peer app rather than inventing a new pattern.

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

### CLI steps (`cli.setup` / `cli.doctor`)

```json
"cli": { "setup": "cli_setup:run", "doctor": "cli_doctor:probe" }
```

`personalclaw setup` calls the setup function with a `personalclaw.sdk.cli.SetupContext`;
`personalclaw doctor` calls the doctor function and renders the `DoctorLine`s it returns.

- **Secrets go through `ctx.settings`**, into a setting declared `x-meta.sensitive`:
  `ctx.settings.update(app, {"bot_token": token})`. Saving keeps the value in the
  credential store under a key the app owns and writes only a reference into the
  settings file, and uninstalling the app removes it. `ctx.save_credential(name, value)`
  writes a shared, plain-named credential that no uninstall can attribute to your app;
  use it only for a name core itself reads. A channel's owner id is one: save it under
  `owner_id_credential(<provider>)` (`PERSONALCLAW_OWNER_ID_<PROVIDER>`) and read it back
  with `owner_id_for(<provider>)`, both from `personalclaw.sdk.channel`. `<provider>` is the
  name the transport files its delivery under,
  `services.register_channel_delivery(delivery, provider=<provider>)`, and core reads the
  owner by that name to reach them on your channel. Each channel keeps its own, because a
  user id means nothing on another platform.
- **Importing your own code.** Core loads these modules by path from the installed copy,
  the way the gateway loads your provider module, and holds the app's directory on
  `sys.path` while the step imports and while it runs. Import your own package as a
  top-level name (`from my_app_runtime.settings import load_token`) with no path code of
  your own. A step that cannot load makes `personalclaw setup --app <name>` exit 1 with the
  exception's class and message. `.github/tests/test_cli_steps_load.py` loads every app's
  steps through core's own loader, in a fresh interpreter, and fails on one that cannot be
  loaded: your own suite cannot see that, because its conftest puts the directory on the
  path first.

### Dependencies

```json
"dependencies": {
  "pythonDependencies": ["faster-whisper>=1.0"]  // pip specs, installed into <home>/app-python
}
```

Core ships lean — the app that needs a heavy library declares it here. The gateway
pip-installs the specs into `<home>/app-python` (`/data/app-python` in the published
image, `~/.personalclaw/app-python` on a pip or uv install), never into the environment
it runs from:

- **Loaded after core's packages.** The directory is appended to the gateway's
  `sys.path`, so an app can add a module but never shadow one core uses, and pip runs
  with every distribution the gateway can import pinned — a dependency that needs a
  different version of one of them fails the install instead of replacing it.
- **One resolution for every installed app.** Apps share one interpreter, so all of
  their requirements are resolved together; a conflict between two apps is refused with
  both named.
- **Importable in place.** A first install needs no restart. The install result reports
  `restart_required` only when a package the gateway had already loaded was replaced.
- **Only the processes core starts for the app see them.** In-process code (your
  provider), your backend and your worker (started through
  `personalclaw._app_python_child`), and your setup hooks (on `PYTHONPATH`). A plain
  `python` your code spawns does NOT: run a declared package's command as
  `sys.executable -m <module>` with the directory you import it from on the child's
  `PYTHONPATH` (the `piper-tts` app does this), and don't look for its console script
  beside the interpreter — pip puts it in `<home>/app-python/bin`.

There is also a `marketplace` block (mcp/skills/agents ids with
`managedBy: "gateway" | "app"`) for marketplace-managed dependencies.

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
| `notification` | `personalclaw.sdk.notification` `NotificationDeliveryProvider` | delivery of a notification addressed to somebody this machine cannot reach — a foreign-addressed note is recorded locally, fired nowhere locally, and offered to each backend until one says it addresses them | `dir-notification` |

This table lists the types with a **reference app**. Ten more are registered and
implementable — `inbox`, `knowledge`, `memory`, `prompt`, `sync`, `sandbox`, `ocr`,
`trigger`, `trigger_source`, `vector_store` — and three (`task`, `workflow`, `duty_gate`)
are registered in core with a live handler but have **no SDK submodule yet**, so an app
cannot implement them without reaching around the lint-enforced boundary. Ask before
starting one of those three; the fix is to promote the contract to `personalclaw.sdk.*`,
not to import the core module.

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
  `PERSONALCLAW_HOME` to `tmp_path`. The repository's `conftest.py` is the floor
  under that: every test starts with `PERSONALCLAW_HOME` pointing at a scratch
  directory of its own, so a test that forgets still does not resolve the real home
  through core. Code that builds `Path.home() / ".personalclaw"` itself is not covered.
- **End-to-end**: install your app from a local source (below) and drive it in
  the real UI. Set `PERSONALCLAW_SKIP_APP_BACKENDS=1` in unit tests that don't
  want backend subprocesses.

## Installing your app while developing

1. Apps page → Store → add the parent directory of your app as a
   **local source** (or `POST /api/apps/local-sources`).
2. Your app appears in the Store; install it. The install runs the full
   quarantine → scan → consent → hook → register pipeline.
3. Iterate: repo edits do NOT reach the installed copy at
   `~/.personalclaw/apps/<name>/`. Push changes with
   `POST /api/apps/<name>/update {"source": "/path/to/my-app"}`. An edit that changes
   what the app gets (a grant, a job, a package, a hook, its server or dashboard code)
   answers 409 with a review instead, and goes through with that review's digest — see
   [updating](third-party-install.md#what-happens-on-update).

See [third-party-install.md](third-party-install.md) for the user-facing
install story.
