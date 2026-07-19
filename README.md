# PersonalClaw Apps

First-party app bundles for [PersonalClaw](https://github.com/PersonalClaw/PersonalClaw). Each subdirectory
is a **self-contained app**: an `app.json` manifest plus its implementation.
Apps import core ONLY through the stable SDK surface (`personalclaw.sdk.*`) —
never core internals — so core can evolve without breaking them, and every app
installs through the same scanner-gated lifecycle as any third-party app.

## What's here

36 apps across the capability types:

- **Model providers** (16) — branded APIs (`anthropic-models`, `openai-models`,
  `bedrock-models`, `google-models`, `deepseek-models`, `groq-models`,
  `mistral-models`, `together-models`), generic endpoints
  (`anthropic-compatible`, `openai-compatible`, `vllm-models`, `ollama-models`),
  and local inference (`faster-whisper` STT, `piper-tts` TTS,
  `sentence-transformers` embeddings, `diarization-onnx` /
  `diarization-pyannote`), plus `fal-image` image generation.
- **Search providers** (7) — `duckduckgo-search` (keyless default),
  `brave-search`, `tavily-search`, `exa-search`, `perplexity-search`,
  `searxng-search`, `wikipedia-search`.
- **Agents** (3) — `claude-code-agent`, `codex-agent`, `kiro-cli-agent`
  (ACP bundles).
- **Tools** (3) — `mcp-tools`, `openai-tools`, `web-tools`.
- **Channel** (1) — `slack-channel` (see [docs/SLACK_SETUP.md](docs/SLACK_SETUP.md)).
- **Action** (1) — `webhook-action`.
- **Skills marketplace** (1) — `skills-sh`.
- **Backend + UI apps** (2) — `growth` (growth/brag-doc tracker), `minutes`
  (meeting minutes on a synced timeline).

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
