# Groq

Groq LPU inference (OpenAI-compatible), very low latency. Bring your own Groq API key.

**Groq** is a **model provider** — it registers Groq-hosted models (OpenAI-compatible) under Settings → Models.

## What this is

A standalone PersonalClaw app bundle (part of the core/app workspace split). It ships
as a self-contained directory:

- `app.json` — the manifest (identity, provider/backend/UI declarations, permissions).
- `provider.py` — the implementation, exposed via `create_provider`.
- `test_catalog.py`, `test_provider.py` — the app's own tests.

It imports only the PersonalClaw **SDK** (never core internals), so core can evolve
without breaking it:

- `personalclaw.sdk.model`
- `personalclaw.sdk.net`

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**Groq** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or `POST /api/apps {"source": ".../apps/groq-models"}`.)

## Settings

| Key | Label | Notes |
|---|---|---|
| `api_key` | Groq API Key | Your Groq API key. Leave empty to fall back to the GROQ_API_KEY environment variable. |
| `default_model` | Default Model | A Groq model id. Empty = resolved from live /v1/models discovery. |
| `endpoint` | Base URL | Optional override of the Groq base URL. Empty uses https://api.groq.com/openai/v1. |

## License

MIT — see the apps repo [LICENSE](../LICENSE).
