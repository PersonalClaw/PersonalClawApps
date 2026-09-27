# Gemini CLI

Run Google's Gemini CLI as an agent (acp:gemini-cli) over ACP. Gemini enters ACP mode via its own `--experimental-acp` flag — no adapter package — and self-authenticates with your `gemini` login (Google OAuth) or a GEMINI_API_KEY you pass through to it by name (Settings → Security → Child environment passthrough); PersonalClaw stores no key. The provider activates only when the `gemini` binary is present, and is unavailable otherwise.

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
(Google OAuth / Gemini API key / Vertex AI), and `/auth` re-runs it. A
`GEMINI_API_KEY` in the gateway's environment works too, once you add its name under
Settings → Security → Child environment passthrough: an agent CLI gets no variable from
the gateway's environment that you have not passed through by name. The runtime's
Sign-in terminal pre-types the resolved `gemini` binary for exactly that first run.

There is no `npx` fallback: a per-spawn download would not share your OAuth state, so
an unresolved binary is reported as unavailable instead.

## Environment

Gemini CLI runs with none of the gateway's environment except what it needs to start and the
variables it reads to pick its provider, project, region and model. Each of these is passed when
it is set in the gateway's environment, so Gemini CLI on Vertex AI or Code Assist keeps the
provider you chose:

| Variable | What Gemini CLI reads it for |
|---|---|
| `GOOGLE_GENAI_USE_VERTEXAI` | Run on Vertex AI instead of the Gemini API |
| `GOOGLE_GENAI_USE_GCA` | Run on Gemini Code Assist |
| `GOOGLE_CLOUD_PROJECT` | The Google Cloud project |
| `GOOGLE_CLOUD_LOCATION` | The Vertex AI location |
| `GEMINI_MODEL` | The model |

No credential is passed: a `GEMINI_API_KEY` or `GOOGLE_API_KEY` reaches it only once you add its
name under Settings → Security → Child environment passthrough, as described above.

## Settings

| Key | Label | Notes |
|---|---|---|
| `model` | Default Model | Optional model the agent defaults to. Empty uses the Gemini CLI's own default. |
| `acp_bin` | CLI Path | Optional absolute path to the gemini binary. Empty auto-resolves via PATH. Equivalent to the GEMINI_CLI_EXECUTABLE env var. |

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**Gemini CLI** — the install runs through the security scanner and lifecycle exactly
like any other app. (Or [install it from a shell](../docs/third-party-install.md#installing-from-a-shell).)

## Capability boundary

This bundle registers the provider; it does not change what Gemini CLI can do over ACP. Where ACP
providers sit relative to PersonalClaw's native runtime — at parity, host-compensated, or blocked by
a protocol or CLI constraint — is documented in [the ACP parity statement](https://github.com/PersonalClaw/PersonalClaw/blob/main/docs/agents/acp-parity.md). Gemini's own
column there is marked unverified until the binary has been driven on a measuring host.

## License

MIT — see `LICENSE`.
