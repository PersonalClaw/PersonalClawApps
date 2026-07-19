# Anthropic

Anthropic Claude chat models (Messages API). Bring your own Anthropic API key.

**Anthropic** is a **model provider** — it registers Anthropic Claude chat models under Settings → Models.

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
**Anthropic** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or `POST /api/apps {"source": ".../apps/anthropic-models"}`.)

## Settings

| Key | Label | Notes |
|---|---|---|
| `api_key` | Anthropic API Key | Your Anthropic API key (console.anthropic.com). Leave empty to fall back to the ANTHROPIC_API_KEY environment variable. |
| `default_model` | Default Model | Anthropic model id. Empty = newest from the app's built-in catalog. |
| `endpoint` | Base URL | Optional custom Anthropic-compatible base URL. Empty uses the Anthropic default. |

## License

MIT — see the apps repo [LICENSE](../LICENSE).
