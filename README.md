# PersonalClaw Apps

[![License: MIT](https://img.shields.io/badge/License-MIT-informational.svg)](LICENSE)

First-party app bundles for [PersonalClaw](https://github.com/PersonalClaw/PersonalClaw). Each subdirectory
is a **self-contained app**: an `app.json` manifest plus its implementation.
Apps import core ONLY through the stable SDK surface (`personalclaw.sdk.*`) —
never core internals — so core can evolve without breaking them, and every app
installs through the same scanner-gated lifecycle as any third-party app.

## What's here

**64 app bundles**, one `app.json` each. 60 contribute a capability provider and
4 contribute none. Five contribute more than one — `companion` (a `tool` and a
`trigger`) and each of the four channel apps (a `channel` plus a
`trigger_source`, and `slack-channel` an `inbox` as well) — so the lists below
count **providers, not bundles**, and those five appear once per provider. 66
providers over 60 bundles, plus the 4 provider-less bundles, is 70 entries over
64 bundles; that is the whole gap between the headline and the sum of the counts.

Nobody has to keep that true by hand: the `readme-census` CI job
(`.github/scripts/check_readme_census.py`) checks the headline, every count
below, and every bundle name against the tree, so a new app cannot land
unlisted.

- **Model providers** (`model`, 23) — branded APIs (`anthropic-models`,
  `openai-models`, `bedrock-models`, `google-models`, `deepseek-models`,
  `groq-models`, `mistral-models`, `together-models`, `alibaba-models`,
  `openrouter-models`, `meta-muse-spark`), generic endpoints
  (`anthropic-compatible`, `openai-compatible`, `vllm-models`, `ollama-models`),
  subscription sign-in
  (`claude-subscription` — rides the Claude Code CLI's own login, no API key),
  and local inference (`faster-whisper` STT, `piper-tts` TTS plus
  `voice-clone-tts` for zero-shot cloning from a reference clip,
  `sentence-transformers` embeddings, `diarization-onnx` /
  `diarization-pyannote`), plus `fal-image` image generation.
- **Search providers** (`search`, 7) — `duckduckgo-search` (keyless default),
  `brave-search`, `tavily-search`, `exa-search`, `perplexity-search`,
  `searxng-search`, `wikipedia-search`.
- **Channels** (`channel`, 4) — `slack-channel` (see [docs/SLACK_SETUP.md](docs/SLACK_SETUP.md)),
  `discord-channel`, `telegram-channel`, `email-channel`.
- **Agents** (`agent`, 4) — `claude-code-agent`, `codex-agent`, `gemini-cli-agent`,
  `kiro-cli-agent` (ACP bundles).
- **Tools** (`tool`, 12) — `mcp-tools`, `openai-tools`, `web-tools`, `code-review`
  (deep per-file review of a GitHub PR over your local `gh`), `research-lab`
  (unattended multi-cycle research campaigns synthesised into one report),
  `notes` (a git-backed markdown notebook — an editor, not a second knowledge
  store), `design-critique` (accessibility + craft findings from a URL's markup
  or a screenshot's pixels), `spec-builder` (write a spec, compile it into a
  workflow definition the workflow engine runs), `ops` (an on-call first
  responder: watch alarms, claim, investigate against your own runbooks, propose
  a fix behind a confirm gate), `companion` (opt-in reminders, a watchlist and a
  day plan — it also serves its own automations as a `trigger` store, so
  disabling it removes every trigger it contributed), `issue-radar`
  (GitHub/GitLab issue triage — labels from the repository's own set,
  investigation notes kept locally), `docs-slides` (a brief becomes a real
  `.pptx` deck or a compiled `.docx`, rendered by the document writers core
  already ships).
- **Sync** (`sync`, 4) — four transports for the same state, pick by what you
  already have: `dir-sync` (a shared/synced folder or mount), `git-sync` (a git
  remote you own), `rsync-sync` (rsync over ssh to any host you can already log
  into), `s3-sync` (an S3-compatible object store you own).
- **Actions** (`action`, 2) — `webhook-action`, `a2a-action` (hand one task to an
  external A2A agent when a trigger fires, egress-allowlisted).
- **Inboxes** (`inbox`, 2) — `mail-inbox`, plus `slack-channel`'s inbox half.
- **Triggers** (`trigger`, 2) — `shared-automations` (serve trigger rows from one
  file a team shares — a synced folder, an NFS share, a checked-out repo), plus
  `companion`'s trigger half.
- **Trigger sources** (`trigger_source`, 4) — the trigger-source half of every
  channel app: `slack-channel`, `discord-channel`, `telegram-channel`,
  `email-channel`. Each turns the inbound traffic its transport already receives
  into `app:<name>:<event>` automation events, so a `kind: event` trigger can fire
  on a real message. Only traffic the app's trust gate already admitted is
  observed, and the event name comes from a frozen vocabulary in the app rather
  than from the message — a sender must not get to pick which of your automations
  runs.
- **Sandboxes** (`sandbox`, 1) — `lima-sandbox`, the virtual-machine
  execution-isolation tier (`limactl`, macOS and Linux).
- **Skills marketplace** (`skills`, 1) — `skills-sh`.
- **Backend + UI apps** (`backend+ui`, 2) — no provider at all, just a backend
  and a UI page: `growth` (growth/brag-doc tracker), `minutes` (meeting minutes
  on a synced timeline).
- **Client-install companions** (`client-install`, 2) — `browser-connector`
  (attach your own everyday browser to a gateway you already run) and
  `menu-bar-companion` (a macOS status-bar view of running loops and the
  approvals waiting on you). Both declare `platform.installMode: "client"`: they
  install on **your** machine, not the server, so the Store hands you a
  copy-paste command instead of installing them.

Building your own app? See the [app creation guide](docs/app-creation-guide.md) —
the `demo-dashboard` worked example there exercises every platform surface
(backend, UI, storage, api/events/cron/agent permissions, MCP server).

## Installing apps

This directory is the **first-party local source** — a PersonalClaw gateway
running from this workspace lists all of these apps in the Store automatically.
Otherwise, add it yourself:

1. Apps page → Store → add this `apps/` directory as a **local source**.
2. Install the apps you want. Each install runs the quarantine → security-scan
   → lifecycle pipeline; the Store shows each app's declared permissions and
   crons before you confirm.

Or via the API, per app:

```
POST /api/apps {"source": "/path/to/apps/<name>"}
```

Note: repo edits do not reach an installed copy (installed apps live at
`~/.personalclaw/apps/<name>/`). Push changes with
`POST /api/apps/<name>/update {"source": "/path/to/apps/<name>", "confirm": true}`.

## Documentation

| Doc | What it covers |
|---|---|
| [docs/platform-architecture.md](docs/platform-architecture.md) | How the platform works: install pipeline, scanner gate, backend subprocess model, proxy + app tokens, permission enforcement, crons, MCP bridge. |
| [docs/app-creation-guide.md](docs/app-creation-guide.md) | How to build an app: manifest schema, capability types, backend contract, UI contribution, permissions, testing. |
| [docs/third-party-install.md](docs/third-party-install.md) | How to install third-party apps: sources, the install/update/uninstall flows, the security gate. |
| [docs/SLACK_SETUP.md](docs/SLACK_SETUP.md) | Slack workspace integration walkthrough. |

Each app also has its own `README.md` with its settings and any app-specific
setup.

## Contributing a new app

1. Read the [app creation guide](docs/app-creation-guide.md).
2. Create a new kebab-case directory here with an `app.json`
   (name/version/displayName/description required), your implementation, tests
   (`test_provider.py` or `test_server.py`), a `README.md`, and a `LICENSE`.
3. Import core only via `personalclaw.sdk.*` — the import boundary is
   lint-enforced by the core test suite.
4. Declare the **minimum** permissions your app needs; the Store shows them to
   the user as the install-consent surface.
5. Validate as a user: install your app from this directory as a local source
   and drive it in the real UI.

## License

MIT — see [LICENSE](LICENSE).
