# OpenAI

OpenAI chat + embedding models (Chat Completions API). Bring your own OpenAI API key.

**OpenAI** is a **model provider** — it registers OpenAI chat + embedding models under Settings → Models.

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
**OpenAI** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or `POST /api/apps {"source": ".../apps/openai-models"}`.)

## Settings

| Key | Label | Notes |
|---|---|---|
| `api_key` | OpenAI API Key | Your OpenAI API key (platform.openai.com). Leave empty to fall back to the OPENAI_API_KEY environment variable. |
| `default_model` | Default Model | OpenAI model id. Empty = resolved from live /v1/models discovery. |
| `endpoint` | Base URL | Optional custom OpenAI-compatible base URL. Empty uses the OpenAI default. |

## License

MIT — see the apps repo [LICENSE](../LICENSE).
