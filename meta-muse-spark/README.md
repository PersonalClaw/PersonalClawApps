# Meta Muse Spark

Meta Muse Spark chat models via the [Meta AI API](https://dev.meta.ai/docs/getting-started/overview/)
(OpenAI-compatible). Bring your own Meta AI API key.

**Meta Muse Spark** is a **model provider** — it registers the Meta AI API's models
(OpenAI-compatible) under Settings → Models.

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
**Meta Muse Spark** — the install runs through the security scanner and lifecycle exactly
like any other app. (Or `POST /api/apps {"source": ".../apps/meta-muse-spark"}`.)

## Setup

1. Get an API key from the [Meta AI developer portal](https://dev.meta.ai/).
2. In **Settings → Models**, add a **Meta Muse Spark** instance.
3. Enter your API key — or leave the field empty and set `META_MODEL_API_KEY` in your
   environment instead.
4. Bind `muse-spark-1.1` to the Chat use case in **Settings → Models**.

## Models

| Model | Context | Capabilities |
|-------|---------|-------------|
| muse-spark-1.1 | 1,048,576 tokens | Chat, Vision (image/pdf/video input), Streaming |

## Settings

| Key | Label | Notes |
|---|---|---|
| `api_key` | Meta AI API Key | Your Meta AI API key. Leave empty to fall back to the META_MODEL_API_KEY environment variable. |
| `default_model` | Default Model | A Meta AI model id. Empty uses muse-spark-1.1. |
| `endpoint` | Base URL | Optional override of the Meta AI base URL. Empty uses https://api.meta.ai/v1. |

## License

MIT — see `LICENSE`.
