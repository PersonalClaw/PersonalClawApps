# OpenAI-Compatible

Any OpenAI-compatible Chat Completions endpoint (self-hosted, proxy, or an unlisted cloud). Supply the base URL + API key.

**OpenAI-Compatible** is a **model provider** — it registers chat/embedding models from any OpenAI-compatible Chat Completions endpoint under Settings → Models.

## What this is

A standalone PersonalClaw app bundle (part of the core/app workspace split). It ships
as a self-contained directory:

- `app.json` — the manifest (identity, provider/backend/UI declarations, permissions).
- `provider.py` — the implementation, exposed via `create_provider`.
- `test_catalog.py`, `test_provider.py` — the app's own tests.

It imports only the PersonalClaw **SDK** (never core internals), so core can evolve
without breaking it:

- `personalclaw.sdk.model`

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**OpenAI-Compatible** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or `POST /api/apps {"source": ".../apps/openai-compatible"}`.)

## Settings

| Key | Label | Notes |
|---|---|---|
| `endpoint` | Base URL | The OpenAI-compatible base URL (should end at /v1), e.g. https://my-gateway/v1. |
| `api_key` | API Key | Bearer API key for the endpoint. Leave empty to fall back to the OPENAI_API_KEY environment variable. |
| `default_model` | Default Model | The model id served by your endpoint. |

## License

MIT — see the apps repo [LICENSE](../LICENSE).
