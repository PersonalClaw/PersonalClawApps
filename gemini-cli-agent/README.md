# Gemini CLI

Run Google's Gemini CLI as an agent (acp:gemini-cli) over ACP. Gemini enters ACP mode via its own `--experimental-acp` flag — no adapter package — and self-authenticates with your `gemini` login (Google OAuth) or a GEMINI_API_KEY in the environment; PersonalClaw stores no key. The provider activates only when the `gemini` binary is present, and is unavailable otherwise.

**Gemini CLI** is an **ACP agent bundle** — it registers an `acp:<cli>` agent via `personalclaw.sdk.acp` and appears in the Agents list.

## What this is

A standalone PersonalClaw app bundle (part of the core/app workspace split). It ships
as a self-contained directory:

- `app.json` — the manifest (provider type + `implementation`; Tier-2 apps carry no `native` flag — that's Tier-1-only).
- `provider.py` — the implementation, exposed via `create_provider`.
- `test_provider.py` — the app's own tests.

It imports only the PersonalClaw **SDK** (never core internals), so core can evolve
without breaking it:

- `personalclaw.sdk.acp`

## Requirements

Gemini CLI on the machine:

```
npm install -g @google/gemini-cli
```

Then sign in once — the first interactive `gemini` run presents its auth picker
(Google OAuth / Gemini API key / Vertex AI), and `/auth` re-runs it. Exporting
`GEMINI_API_KEY` works too. The runtime's Sign-in terminal pre-types the resolved
`gemini` binary for exactly that first run.

There is no `npx` fallback: a per-spawn download would not share your OAuth state, so
an unresolved binary is reported as unavailable instead.

## Settings

| Key | Label | Notes |
|---|---|---|
| `model` | Default Model | Optional model the agent defaults to. Empty uses the Gemini CLI's own default. |
| `acp_bin` | CLI Path | Optional absolute path to the gemini binary. Empty auto-resolves via PATH. Equivalent to the GEMINI_CLI_EXECUTABLE env var. |

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**Gemini CLI** — the install runs through the security scanner and lifecycle exactly
like any other app. (Or `POST /api/apps {"source": ".../apps/gemini-cli-agent"}`.)

## License

MIT — see `LICENSE`.
