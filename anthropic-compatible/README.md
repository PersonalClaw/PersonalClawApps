# Anthropic-Compatible

Any Anthropic-compatible (Messages API) endpoint. Supply the base URL + API key.

**Anthropic-Compatible** is a **model provider** — it registers chat models from any Anthropic-compatible (Messages API) endpoint under Settings → Models.

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
**Anthropic-Compatible** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or `POST /api/apps {"source": ".../apps/anthropic-compatible"}`.)

## Settings

| Key | Label | Notes |
|---|---|---|
| `endpoint` | Base URL | The Anthropic-compatible base URL, e.g. https://my-gateway. |
| `api_key` | API Key | API key for the endpoint. Leave empty to fall back to the ANTHROPIC_API_KEY environment variable. |
| `default_model` | Default Model | The Anthropic model id served by your endpoint. |

## License

MIT — see the apps repo [LICENSE](../LICENSE).
