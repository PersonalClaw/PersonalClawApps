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

The link carries the owner token as a `?token=` query parameter, which is how a browser signs
in: the gateway accepts it there only inside the link's window (24 hours at most). A shell, a
script or a client app sends the same token as an `Authorization: Bearer` header instead. The
gateway accepts that for the token's whole lifetime, and it keeps the token out of URLs, shell
history and request logs.

**5 — review it, install it from that local path, and enable it.** An install is two
calls: the review says what the app gets and what the security scanner found, and installs
nothing; the install carries the review's `consent` digest, so it installs exactly the bytes
you reviewed (anything else — `"confirm": true` included — answers 409 with a fresh review).

```bash
auth="Authorization: Bearer $PERSONALCLAW_TOKEN"
review="$(curl -sS -X POST "$PERSONALCLAW_URL/api/apps/preview" -H "$auth" \
  -H 'Content-Type: application/json' -d "{\"source\": \"$PWD/my-tool\"}")"
echo "$review" | python3 -m json.tool      # read it: permissions, jobs, packages, the scan
consent="$(echo "$review" | python3 -c 'import json, sys; print(json.load(sys.stdin)["consent"])')"
curl -sS -X POST "$PERSONALCLAW_URL/api/apps" -H "$auth" \
  -H 'Content-Type: application/json' \
  -d "{\"source\": \"$PWD/my-tool\", \"consent\": \"$consent\"}"
curl -sS -X POST "$PERSONALCLAW_URL/api/apps/my-tool/enable" -H "$auth"
```

Prefer clicking? **Store → Add source → local path**, point it at `my-tool`, then install
and enable. Same review, same supply-chain scan gate, same consent — there is only one path.

**6 — confirm it is live.**

```bash
curl -sS "$PERSONALCLAW_URL/api/apps/my-tool" -H "$auth"
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
Unknown fields are preserved for forward compatibility, never fatal. Every boolean is
the JSON `true` or `false`, written without quotes: a `"false"` in quotes, a number or
`null` is refused at install (and in the review before it), with a message naming the
field.

### Identity (required)

```json
{
  "name": "my-app",              // unique id, kebab-case (validated)
  "version": "1.0.0",            // MAJOR.MINOR.PATCH, a pre-release as 1.0.0-rc.1 (validated)
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
  "execution": "in-process",                 // or "sidecar": its own process and Python environment
  "settingsSchema": { ... }                  // JSON Schema (Draft-07 + x-meta) for the Configure form
}
```

The factory receives the app's current config dict and returns a provider
instance implementing the relevant SDK contract.

`execution: "sidecar"` is for an engine heavy or crash-prone enough to keep out of
the gateway (torch, a native library). Your provider still loads in-process, and it
drives the engine through `personalclaw.sdk.sidecar.SidecarRunner`: a worker module
you ship runs in a child process under the app's own Python environment,
`apps/<app>/venv`, so a crash in it leaves the gateway up with a typed reason. The
engine packages go in `dependencies.sidecarDependencies` (see
[Dependencies](#dependencies)). `voice-clone-tts` is the worked example.

An engine that stays in-process is called from several of the gateway's threads at once: a
re-index, a recall in a chat and the check a re-index makes before it starts can each reach it
first. Build its model once, behind a lock every call takes, and give the model one call at a time
unless its library says it is safe from several threads. Choose its device yourself: left to choose
on Apple silicon, torch takes the GPU (`mps`), whose compiled kernels sit in one cache for the
whole process with no lock around it, so two threads on it at once crash the gateway. Run an
in-process torch model on the CPU, as `sentence-transformers` does, or move it to a sidecar.

`settingsSchema` properties support `x-meta` per field:
`label`, `help`, `placeholder`, `sensitive: true` (secret handling), `tags: ["advanced"]`
(collapsed by default), `instruction: true` (words the owner writes for the agent to follow,
see below), and `enum` for dropdowns. Describe a structured field and
the form edits it with controls instead of a JSON box: an `array` whose `items`
are strings is chips, and one whose `items` are an `object` with `properties` is
a row per entry. An `object` with `properties` is a control per field (a field
typed `["string", "null"]` gets a switch, off storing `null`); an `object` whose
entries are keyed is a row per entry, its `additionalProperties` saying what each
value is and its `propertyNames` `x-meta` naming the key. Values the user saves land in
`<home>/apps/<name>/data/config.json` and are read back via
`personalclaw.sdk.settings.ProviderSettings`.

A string setting declared `instruction: true` (a column of a table of rows included) holds the
owner's instruction for the agent, such as the prompt `mail-inbox` runs for mail to one of its
addresses. An inbox source hands such an instruction over beside a message's words, never in
them: `IncomingMessage(text=<the message as it arrived>, instruction=<her setting's value>)`,
from `personalclaw.sdk.inbox`. PersonalClaw takes it as hers only when your app's settings hold
it word for word in a setting declared an instruction, or your manifest gives that setting it as
its `default`; anything else handed over is dropped and recorded in the Security log. A run on
the message is handed her instruction first, outside any fence, then the message fenced once by
PersonalClaw. So never fence a message's text yourself, and never put an instruction in it: a
fence of yours, and the prompt beside it, would be wrapped again as data. Declare the
`message-instructions` core feature.

#### A folder in PersonalClaw's home is asked for, never spelled

PersonalClaw runs on the home `PERSONALCLAW_HOME` names (a second instance, a test's, a dev
gateway's), else `~/.personalclaw`, and only core works out which. A path your app keeps there
comes from `personalclaw.sdk.util`, asked when the path is used: `config_dir()` for the home,
`app_data_dir(<name>)` for your app's own folder in it. A setting that names such a folder
defaults to empty, and your code fills the empty value in from those: a default of
`~/.personalclaw/…` is the account's home whatever home PersonalClaw runs on, and the Configure
page shows it as the setting's value and saves it. `rsync-sync`'s Local working directory is the
worked example. `.github/scripts/check_active_home.py` fails an app whose code reads
`PERSONALCLAW_HOME`, builds `~/.personalclaw` from the account's home, or spells a path under it
in its code or its manifest.

#### Advanced and required — the one convention

The host renders every app's Configure form with the same widget, so the whole
catalog has to mean the same thing by `advanced` and `required` — otherwise
identical forms look arbitrary. Three rules, enforced by
`.github/scripts/check_settings_schema_posture.py` in CI:

1. **Optional tuning fields fold.** A field a first-run user never needs —
   `timeout_secs`, an optional `endpoint` override on a hosted
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
  "memory": false,                               // true to read and change the owner's memory
  "cron": true,                                  // may register manifest crons (needs `agent`)
  "agent": "text"                                // what its agent tasks may use: "text" | "read" | "tools"
}
```

Declare the **minimum** you need — the Store shows this block to the user as the
install-consent surface. All of these are enforced server-side EXCEPT `network`,
which is declaration-only by design (a backend subprocess has its own OS network
stack; the flag discloses intent to the user rather than fencing it). See the
[permission enforcement table](platform-architecture.md#permission-enforcement).

#### Agent tasks: pick the least tier your tasks need

`agent` names a tier, and install consent shows the owner one sentence for it:

| Tier | What a task's model gets | Use it when |
|---|---|---|
| `text` | the task your app sends and nothing else — none of the owner's memory, lessons or history — with no tools at all; it runs on PersonalClaw's own worker that has no tools to call, and any call it makes is refused | your app already has the content and wants text back: a summary, an extraction, a draft (Minutes and Growth) |
| `read` | an agent with read-only tools: it may read the owner's files and data and change nothing. It runs only on PersonalClaw's own agent, which can be held to read-only tools; a task on an agent CLI is refused before it starts | the task has to look things up first |
| `tools` | an agent with the owner's tools | the task has to change things |

No tier lets your app approve its agent's calls: a task starts on the owner's install consent,
and every call that needs approval asks her, whatever her own approval settings are. A turn in a
conversation your app started needs `tools` and asks the same way, and so does each of your
scheduled jobs: its agent runs at your app's tier (see [Crons](#crons)). Every agent your app's
agent starts is your app's work too, held the same way: a subagent, a batch of them
(`subagent_run`) and each step of a workflow run it starts run at no more than your tier and ask
her for their calls, and each ask names your app (and the job, when a job started the work). A
batch never starts on her own approval settings, and from an app whose tier is `text`, or that
holds none, it starts nothing. `"agent": true` names no tier and is refused at install.

In Python, a parsed manifest holds the tier as `Permissions.agent_tier`, `""` when the app runs no
agent work, and `personalclaw.sdk.manifest.AGENT_TIERS` lists the tiers, narrowest first. The field
is not `agent`, the boolean the tiers replaced, so code still passing `Permissions(agent=...)`
fails where it is called.

A task runs at your app's tier unless it asks for a narrower one, and asking for a wider one is
refused (`403 agent_tier_exceeded`) before anything runs:

```js
const agent = createAgentTask(ctx.name)
const res = await agent.run(`Summarise these notes:\n${notes}`)    // your app's tier
await agent.run('Find the open invoices.', { tier: 'read' })        // a tools app, reading only
```

The SDK rejects a refused task with an `AppPermissionError` carrying the gateway's sentence,
which names the tier the task asked for and the one your app holds. A `text` task names no
`agent`. Widening your tier in an update is a change the owner is asked to agree to again.

A request your provider makes goes through `personalclaw.sdk.net.fetch` under a policy built
with `egress_policy_for(<profile>)`: `CONNECTOR` for a vendor's API, `WEBHOOK` for a POST to an
address the owner configured. That layers the owner's **Settings → Security → Network egress**
onto the profile, so a host they put on Denied hosts is never reached, and one on Allowed hosts
is reached even when it is on their own network. Say a refusal (`EgressBlocked`) with
`egress_refusal(url, exc.decision)`: the sentence names the setting that lifts it. A synchronous
surface that says a refusal in its own words asks `evaluate(url, egress_policy_for(CONNECTOR))`
first, and refuses when that check raises, since it judged nothing; its request still goes
through a guarded client (below). The bare profile, or no policy at all, leaves the owner's
settings out, and `.github/scripts/check_egress_policy.py` holds every request in this repository
to the layered form, and every failed check to a refusal.

A provider that sends its requests with an HTTP client of its own, or through a vendor SDK, opens
that client from `personalclaw.sdk.net` with the guard inside it: `http_client(...)` and
`sync_http_client(...)` (an `httpx` client) or `http_session(...)` (an `aiohttp` session). Each
asks the guard about every request it sends, each redirect hop included, under the owner's Network
egress settings, and a refused request raises `EgressBlocked` before it is sent, its message the
sentence to show. `model_provider=True` judges a model provider's requests, with the endpoint the
owner set on the instance (`endpoint=...`) reachable on their own machine or network;
`shared_by_every_run=True` is for the agent's own model (its chat, embeddings and model list),
which a run whose network is off still thinks with. Hand an SDK that takes an `http_client`
(`openai`, `anthropic`) one of these.

A client library that takes no HTTP client asks the guard itself: build a
`RequestGuard(model_provider=True, ...)` with the same keywords, and call its `ask(url)` from the
hook the library runs before each request, letting the refusal propagate so the request is never
sent. That glue is the library's, so it lives in your app. For boto3, register a `before-send`
handler on the session as soon as it is made, before any client, as `bedrock-models` does:

```python
session = boto3.Session(profile_name=profile)
guard = RequestGuard(model_provider=True)

def ask(request, **_event):
    guard.ask(str(request.url))

session.events.register_first("before-send", ask)  # every client made from it asks, retries too
```

An app that uses any of these declares `"requiresCoreFeatures": ["guarded-clients"]`, and
`.github/scripts/check_network_clients.py` fails app code that opens an HTTP client any other
way, or an AWS session with no `before-send` hook that asks a guard registered on it before its
first client (a chat channel's connection to its own service is not held to it yet).

A download too large to hold in memory (a model's files) streams through
`open_url(url, timeout_s=...)` instead: the standard library's opener with every request asked
of the guard first, under `CONNECTOR` and the owner's settings, each redirect hop included. A
refused request raises `EgressBlocked` before it is sent, its message the sentence to show; what
the source answers is yours to read, bound and check, as with `urllib.request.urlopen`. An app that
downloads with it declares `"requiresCoreFeatures": ["guarded-download"]`, so a PersonalClaw without
it refuses the app rather than installing one that cannot load.

### Crons

```json
"crons": [{
  "name": "heartbeat",             // required
  "cron_expr": "*/30 * * * *",     // OR "every": <seconds> — one is required
  "agent": "",                     // agent to run (empty = default; none at the text tier)
  "message": "Record a heartbeat timestamp"
}]
```

Requires the `cron` permission and an agent tier (`permissions.agent`): each job's agent runs at
your app's tier, so a manifest that declares jobs, or `cron`, without one is refused at install.
Jobs register as `app:<app>:<cron>`, named for the owner by your app's display name, and
reconcile on boot + every lifecycle transition. Each run is your app's agent work, held as an
agent task is: at `text` its model is handed the job's message alone, with no tools, so a `text`
job names no `agent`; at `read` it gets read-only tools and sends no message; at `tools` it gets
the owner's tools. It approves none of its calls, so each one that needs approval asks the owner,
and nobody is there on a schedule to answer at once: give a job the least tier its tools need, and
prefer tools of your own that need no approval for what it does on every run (Ops' sweep is the
worked example). Install consent says each job's tier. A job has no conversation to post to:
surface its results through your backend, or your own tools' state.

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
  "pythonDependencies": ["faster-whisper>=1.0"],   // pip specs, installed into <home>/app-python
  "sidecarDependencies": ["omnivoice>=0.2.1,<0.3"] // pip specs for a sidecar's engine, installed into apps/<app>/venv
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
- **A process your provider starts gets every secret the gateway holds unless you pass its
  environment.** Your provider runs inside the gateway, and the gateway's environment holds
  every secret saved in PersonalClaw. Give a child that runs a program or package someone else
  wrote `env=child_process_env()` (`personalclaw.sdk.util`: the child allowlist, your `extra`
  over it, `installer="npm"` or `"pip"` for an install, and `ssh_agent=True` for a program that
  signs in over ssh with the owner's keys, which adds their SSH agent socket and nothing else),
  and a declared package run as
  `python -m` `env=app_packages_env()`. `.github/scripts/check_child_process_env.py` holds every
  spawn in this repository to one of those, or to a stated reason (your own authenticated tool
  that signs in from the environment, a doctor step).
- **A git your provider runs goes through `personalclaw.sdk.git`.** It usually runs in a
  repository an agent's shell can write (the owner's clone, a notebook), and a hook, a
  file-system monitor or an ssh command set in that repository's `.git` would otherwise run as
  the gateway. Build the argv with `git_argv(args)` and pass `env=git_env(remote=...)`, with
  `remote=talks_to_remote(args)`: the child allowlist, plus the owner's SSH agent for a command
  that talks to a remote. A command that talks to a remote signs in with the owner's own ssh
  command and credential helpers; a remote at a local path is refused. The same rail holds every
  spawn of a `git` argv to `git_argv`. A git older than 2.12 ignores some of those settings, so
  `git_argv` raises `GitTooOld` for one, an `OSError` whose message names the version needed, the
  one found and what to do: catch it where you catch a missing git, and show its message. In a
  doctor or setup step, `git_problem()` is that message before anything runs (`""` when git can
  run).

`sidecarDependencies` is the engine of a provider declared `execution: "sidecar"`, and a
manifest that lists it without one is refused. Nothing installs it with the app. The owner
presses **Install engine** on the app's card in Settings → Providers, or on its Configure
page, and PersonalClaw makes `apps/<app>/venv` and pip-installs the list there, showing pip's
output as it runs. Only your worker, in that child process, imports these packages; the
gateway never does. Each entry is a PEP 508 requirement (an option such as `--index-url` is
an install error), and install consent names them under what the app runs. An update keeps
the environment, so the engine survives it. If the new version declares a different list,
the card offers Install engine again and pip brings the same environment up to it. Say in
your provider's availability reason that Install engine is the way in, as `voice-clone-tts`
does, rather than giving shell commands.

There is also a `marketplace` block (mcp/skills/agents ids with
`managedBy: "gateway" | "app"`) for marketplace-managed dependencies.

### Prerequisites (`requires`)

```json
"requires": [{
  "name": "ComfyUI",                                   // what it is (at most 80 characters)
  "why": "Every image is made by a ComfyUI server ...", // what the app uses it for (300)
  "how": "Install ComfyUI and start it on this machine ..." // what the owner does to have it (600)
}]
```

What the app needs on this machine that PersonalClaw does not install: a local server it
sends its work to, a program a tool runs. Install consent leads with these, under "What it
needs that PersonalClaw doesn't install", the Store card says "Needs ComfyUI", and an update
that adds one asks for consent again. All three strings are plain text shown as you wrote
them, so write `how` as steps the owner can follow, with the address or command they need.
At most 10 entries, each named once. Something PersonalClaw can install is not a
prerequisite: a Python package goes in `dependencies`. `local-image-gen` is the worked
example.

### What it needs from PersonalClaw (`minPersonalClawVersion`, `requiresCoreFeatures`)

```json
"minPersonalClawVersion": "0.2.0",            // the oldest PersonalClaw release it runs on
"requiresCoreFeatures": ["approval-answers"]  // the core contracts it relies on, by name
```

PersonalClaw refuses to review, install, update or switch on an app it cannot host, and the
refusal names what it lacks, so the owner is told to update PersonalClaw instead of finding the
app quietly doing less. The floor and PersonalClaw's own version are read by the packaging
standard (PEP 440), so a release candidate or a dev build is older than its release: `0.3.0rc1`
refuses an app that needs `0.3.0`. A floor that is not a version (`latest`, `>=0.2`) refuses the
app everywhere, naming the value. Every PersonalClaw built between two releases reads the same
version, so `minPersonalClawVersion` cannot tell a build that has a contract your app relies on
from one made before it. A core feature can: each names one contract, and
`personalclaw.sdk.features` lists the ones the running PersonalClaw offers (`CORE_FEATURES`;
`core_has(name)` asks about one). Declare each one your app cannot work without. For one it can
do without, ask `core_has` and say so when it is missing.

| Feature | What it is | Declared by |
|---|---|---|
| `approval-answers` | the answers an approval prompt offers, handed over in the approval brief (`approval_brief_for(event)["answers"]`) | a channel app whose prompt offers them: `telegram-channel`, `slack-channel`, `discord-channel`, `email-channel` |
| `chat-trust` | a channel that runs a conversation itself keeps no trust of its own: its prompt offers the chat's answers (`approval_brief_for(event, chat=...)`), Allow for this chat becomes the Trust of PersonalClaw's chat for the conversation (`answer_in_chat`), and each call asks which of that chat's grants answers it (`chat_grant`) | a channel app that runs its own turns: `slack-channel` |
| `links-name-their-channel` | a chat's link to a channel thread names the channel it is on, where the chat answers: `link_channel(chat, thread, channel_id, provider=...)` links a chat on your channel (moving it off any thread it was on; the owner's own DM it left is told where it went), and `SessionManager.get_channel_provider(key)` says which channel a chat is on | a channel app that links a chat to one of its threads itself: `slack-channel` |
| `tool-call-screen` | a channel that runs a conversation itself asks PersonalClaw's deny-list about each call before it approves or asks about it (`screen_tool_call(hooks, event.title, event.tool_input)`, the hook chain's verdict read on the command the call would run as well as on its title) and refuses a call it refuses, never putting it on its prompt | a channel app that runs its own turns: `slack-channel` |
| `message-instructions` | an inbox source hands PersonalClaw the owner's instruction beside a message's words (`IncomingMessage.instruction`), held by a setting declared `x-meta.instruction`, and a run on the message is handed it outside any fence, then the message fenced once | an inbox app whose source hands one over: `mail-inbox` |
| `closing-streams` | a model's stream your app reads is closed the moment it stops reading: read inside `personalclaw.sdk.model.closing_stream`, it is closed by any way out of the block, and an agent CLI's turn left part way is told to stop and its session takes the next prompt at once | an app that reads a model's stream (`stream`, `stream_command`, `complete`): `slack-channel`, `code-review`, `issue-radar` |
| `guarded-clients` | the HTTP clients `personalclaw.sdk.net` hands out with the egress guard inside them (`http_client`, `sync_http_client`, `http_session`), each asking the guard about every request it sends, each redirect hop included, under the owner's Network egress settings, and the guard itself (`RequestGuard`) for a client library's own hook; a refused request raises `EgressBlocked` before it is sent | an app that sends requests with a client of its own: `bedrock-models`, `google-models`, `alibaba-models`, `fal-image`, `openai-tools`, `skills-sh` |

A PersonalClaw from before core features cannot read the field, so an app also checks what it
relies on where it uses it: a channel app handed a brief with no answers says so on the prompt,
with nothing to press, and logs it once. `.github/tests/test_core_features_declared.py` holds
every bundle's names to the ones the installed PersonalClaw offers, every app whose prompt
offers the brief's answers to declaring `approval-answers`, every app that gives or reads a
chat's Trust to declaring `chat-trust`, every app that links a chat on its own channel to
declaring `links-name-their-channel`, every app that screens a call with the deny-list to
declaring `tool-call-screen`, every app that reads a model's stream inside `closing_stream`
to declaring `closing-streams`, every app that opens a guarded HTTP client or asks the guard
itself to declaring `guarded-clients`, and every app whose source hands over a message's instruction to
declaring `message-instructions`.
`.github/tests/test_model_streams_are_read_inside_closing_stream.py` holds every app to reading a
model's stream that way: an `async for` over a provider's stream, or an `anext` of one, anywhere
else fails it.

### What it starts, installs and writes outside PersonalClaw (`launches`, `npmPackages`, `writes`)

```json
"launches": [{
  "program": "claude",                                  // its name as found on this machine (80)
  "why": "Claude Code does the work of each chat ...",  // what the app uses it for (300)
  "inherits": ["sign-in", "settings", "auto-approve-rules", "folder-settings"],
  "inheritsWhile": {"setting": "isolated_config", "value": false} // optional
}, {
  "program": "npx",
  "why": "Without a skills.sh API key, each search you make runs skills find ...",
  "npmPackage": "skills",                               // what npx downloads and runs
  "hosts": ["registry.npmjs.org", "skills.sh"]          // optional: the hosts it reaches (10)
}],
"writes": [{
  "path": "cc-config",                                  // in the PersonalClaw folder, or "~/…" (200)
  "why": "The Claude config Claude Code runs with ..."  // (300)
}],
"dependencies": {
  "npmPackages": ["@agentclientprotocol/claude-agent-acp"] // installed into <home>/acp-adapters
}
```

Install consent lists each of these under what the app runs, and an update that adds one, or
widens what a program inherits, asks for consent again:

- **`launches`** is each program on the machine your app starts, outside PersonalClaw: an agent
  CLI, or a tool your code runs. `inherits` says what it runs with, from four words. Three are
  the owner's own: `sign-in` (the account it is signed in to), `settings` (its own configuration
  folder) and `auto-approve-rules` (rules there that let it act without asking; consent says that
  what they allow, it does without asking first). The fourth, `folder-settings`, is its settings
  in the folder it works in, such as a repository's own, which can add rules of that kind and
  commands for it to run; consent names them apart, since whoever wrote the folder wrote them.
  Declare it for an agent CLI that reads a project's own config. When one of your provider's boolean
  settings decides that, name it in `inheritsWhile`: consent then says "While Isolated Claude
  settings is off, …" in the setting's own label, and where it starts.
  - `hosts` names the hosts the program reaches (`"github.com"`: lowercase, no scheme, port or
    path), and consent says "It reaches …".
  - An `npx` entry names the npm package npx downloads and runs in `npmPackage`, and every `npx`
    entry must. Consent says that each time, npx fetches the newest version of that package from
    the npm registry and runs it as you, and that npm runs its install scripts. Your code must run
    exactly that package, by name and with no version (`npx -y skills find …`): `skills-sh` is the
    worked example.
  - When the owner chooses the program, as with a runbook action they wrote, the program is `*`.
    Consent says "Starts the programs you name for it", and `why` says where they name them. A `*`
    entry covers no program your own code names, and lets core start nothing: `ops` is the worked
    example.
- **`dependencies.npmPackages`** is each npm package core may install for the app into
  `<home>/acp-adapters` when it is installed or switched on (`provision_acp_adapter`), or fetch
  with `npx` until it is. Names only, no versions. A package your own code runs with `npx` is not
  one of these: core never installs it, and its `launches` entry names it.
- **`writes`** is each place outside your app's folder its code writes, the programs it starts
  included (`~/.npm`, where npx keeps what it downloads).

The `launches-declared` CI job (`.github/scripts/check_launches_declared.py`) reads every spawn in
your app's own code (`subprocess`, `asyncio.create_subprocess_*`, `os.system`, `os.exec*`,
`os.spawn*`, `pty.spawn`) and fails the app for a program its manifest does not declare, or an
`npx` that runs a package its entry does not name. Start a program by name (a literal argv,
`shutil.which("<name>")`, `git_argv`, `find_ffmpeg`) so the rail can read it; its docstring lists
the shapes it reads, and what to do when your code hands a program on at run time.

An agent app that keeps its CLI's sessions to a config of their own passes the CLI's
per-session options with `register_acp_cli_entry(session_meta={...})`: a JSON object core adds as
the `_meta` of every `session/new` and `session/load` it sends from the app's entry.
`claude-code-agent` asks for Claude Code's `user` setting source alone while Isolated Claude
settings is on, so a session loads nothing from the folder it works in.

An agent app whose CLI compacts its own conversation when its context fills says so with
`register_acp_cli_entry(compacts_itself=True)`. Core then leaves the CLI's sessions alone at
the Auto-compact threshold (Settings → Chat), so a chat keeps the CLI's session, with the results
of its earlier tool calls. An app that does not say it gets the safe default: a session that
crosses the threshold is restarted from the chat's own history, since core cannot compact a
conversation the CLI holds, and the chat says it was restarted and why. Declare it only for a CLI
you have read compacting itself under ACP: `claude-code-agent` and `codex-agent` do, and their
`COMPACTS_ITSELF` comments say what they read.

Core holds what it does for your app to this list. `provision_acp_adapter` installs no package
the manifest does not list, and `register_acp_cli_entry` refuses a CLI from an app that lists no
program, whose adapter's engine (`requires_executable`) it does not list, or whose adapter runs
through `npx` as a package it does not list. The provider's card in Settings → Providers then
says why. `claude-code-agent`, `codex-agent`, `kiro-cli-agent` and `gemini-cli-agent` are the
worked examples.

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
`native` flag is reserved for the apps that ship inside PersonalClaw; never set it. An
app is native only when PersonalClaw installed it from its own package, so the review,
install and update of any other app that sets `"native": true` is refused, naming the
field.

## Capability types

`provider.type` must be one of the registered types. The ones you'll actually
build, each with its SDK contract (apps import ONLY `personalclaw.sdk.*` — the
boundary is lint-enforced):

| Type | SDK contract | What it plugs into | Reference app |
|---|---|---|---|
| `model` | `personalclaw.sdk.model` (chat LLMs), `sdk.stt`, `sdk.tts`, `sdk.diarization`, `sdk.embedding`, `sdk.image`, `sdk.local_model` (download/manage local models) | Settings → Models; bound per use-case (chat/background/embedding/stt/tts/…) | `anthropic-models` (branded API), `openai-compatible` (generic endpoint), `claude-subscription` (rides a CLI's subscription sign-in, no API key), `faster-whisper` (local STT), `sentence-transformers` (local embeddings) |
| `search` | `personalclaw.sdk.search` `SearchProvider` | Settings → Search; the `web_search` tool | `brave-search`, `duckduckgo-search` (keyless) |
| `agent` | `personalclaw.sdk.acp` (ACP agent bundles) | the Agents list | `claude-code-agent`, `codex-agent` |
| `tool` | `personalclaw.sdk.tool` (+ `sdk.mcp`) | the agent tool layer | `web-tools`, `openai-tools` |
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

Each model a catalog lists carries the jobs it can be bound for (`chat`,
`image_modality`, `embedding`, `stt`, …, or `[]` for none), and every picker in
Settings → Models offers a model only for those. When your vendor's `/models`
records say what a model does (a `type`, a capability record), pass
`register_branded_app(SPEC, capabilities_of=…)`: it is handed each record as the
vendor wrote it and returns that model's jobs (references: `mistral-models`,
`together-models`). Without one, a model is read by its id, which offers a
model whose name says nothing for chat.

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

Ship that bundle built. An install copies your app as it is and never builds anything, so
a page written in TSX is built ahead of time and its output committed: Minutes and Growth
build into `ui/bundle/`, and the `ui-bundles` CI job rebuilds them and fails when the
committed file differs. Don't build it from an install hook: the hook has 60 seconds, and
the user may have no Node at all.

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
one. The SDK also provides `createAgentTask` (background agent tasks at the
app's `agent` tier, see [Agent tasks](#agent-tasks-pick-the-least-tier-your-tasks-need)),
`useTheme`/`readAppTheme`, and React-hook variants
(`useAppApi`, `useAppEvents`) under an `AppApiProvider`.

Read your saved `configSchema` values via your own detail endpoint
(`GET /api/apps/<name>` — declare it in `permissions.api`), like demo-dashboard
does for its `label` and `refresh_interval_s`.

### Saving your settings from your page

`PUT /api/apps/<name>/config` replaces the whole settings file, so the gateway
saves it only over the copy your page read. `GET /api/apps/<name>/config` answers
with that copy and its `revision`. Keep the two together, and pass the revision
back as `{ basedOn }` on the save. The SDK sends it as `If-Match`, the one header
an app sets. `post`, `put` and `patch` all take it, and declaring
`/api/apps/<name>` in `permissions.api` covers both routes.

```js
import { createAppApi, isStaleWrite, notify } from '@personalclaw/app-sdk'

export function mount(el, ctx) {
  const api = createAppApi(ctx)
  const route = `/api/apps/${ctx.name}/config`
  let read // { config, revision, … }

  async function load() {
    read = await api.get(route)
    render(read.config)
  }

  // `edit` is only what the user changed, e.g. { collection: 'journal' }.
  async function save(edit) {
    const put = () => api.put(route, { ...read.config, ...edit }, { basedOn: read.revision })
    try {
      read = await put().catch(async (e) => {
        if (!isStaleWrite(e)) throw e
        // 409 stale_write: the settings changed after this page read them (another
        // tab, Settings → Providers, your own backend), and nothing was saved.
        // Apply the same edit to the stored copy and save over its revision.
        read = await api.get(route)
        return put()
      })
      render(read.config) // what the save stored, with its new revision
    } catch (e) {
      notify(e.message, 'error') // 428, or any other refusal, in the gateway's words
    }
  }

  // ... render into el, call save() from its controls ...
  load()
}
```

- `isStaleWrite(e)` is true only for `409 stale_write`, the one refusal a page
  recovers from by itself. If the other change could touch the field the user
  edited, show them both values instead of re-applying theirs.
- A save without `basedOn` is refused with `428 revision_required`
  (`e.status === 428`, `e.code === 'revision_required'`). It fails the same way
  every time, so it is a bug in the page, not something to retry.
- Every refusal rejects with the gateway's `status`, `code` and sentence
  (`e.message`), so `notify(e.message, 'error')` shows the user the gateway's
  own words.
- A sensitive field comes back masked, and sending the mask back keeps the
  stored secret. `{ ...read.config, ...edit }` therefore never erases a secret
  the user did not touch.

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
  through core. Code that works the home out itself would not be covered, which is one reason
  none may ([a folder in PersonalClaw's home](#a-folder-in-personalclaws-home-is-asked-for-never-spelled)).
  It also turns the OS keychain off for the run, with
  `personalclaw.sdk.testing.keychain_off()`: one keychain serves every home on the
  machine, so a scratch home alone would leave the owner's secrets in reach.
  And every test's git runs with none of the machine's git configuration: no system
  file, a global file of the test's own (`GIT_CONFIG_GLOBAL`, where a test puts a setting
  it needs, such as an ssh stand-in), and an empty `credential.helper`
  (`personalclaw.sdk.testing.neutral_git_env`). A git that could still sign in with a
  credential helper of the machine's own, which on a Mac is the owner's real keychain, is
  refused before it starts, and the test fails by name (`refuse_git_helpers`, the guard
  core's own suite installs).
- **Fake keys look like no key.** A test that hands your provider an API key, a bot token or an
  access key id uses a neutral fake (`fake-anthropic-test`, `fake-bot-token-saved`), never a
  provider's format: the repository is public and secret scanners read it. A masking test that
  needs core's redactor to see a key uses the AWS documentation's example key id.
  `.github/scripts/check_test_values.py` holds every bundle's tests to that.
- **The runtime's per-turn note is an instruction, not a message from the user.** Core's native
  loop sends one `role: "system"` message a turn tagged `personalclaw.sdk.model.VOLATILE_KEY` (the
  tool catalog, already fenced as the runtime's). A wire that takes a system message anywhere
  sends it where it is. A wire whose system prompt is out of band (Anthropic Messages, Bedrock
  Converse) appends it as one text block to the request's last user turn, after its tool results
  and any cache checkpoint: never as a user turn of its own, which a model answers in the chat,
  and never in the system prompt, where text that changes every turn defeats the cache.
  `bedrock-models` is the reference.
- **A model call names its model.** Core hands every call the model its binding in Settings →
  Models names, and a call whose binding names none is refused with the SDK's sentence
  (`personalclaw.sdk.model.require_model`) before anything is sent or loaded: nothing picks a
  model in its place. A chat-model app proves it with `apps_testkit.model_wire.blank_model_report`.
  An app that serves an image, video, speech, embedding or diarization call builds its adapters
  with `media_adapters(<app dir>, create_provider, scanners=(…every function it hands
  register_scanner…))` and compares `media_refusal_report(adapters)` with
  `media_refusal_expected(adapters)`. `.github/tests/test_model_apps_prove_the_wire.py` holds every
  model app to it.
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
