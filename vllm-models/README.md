# vLLM

A local vLLM server (OpenAI-compatible). Point it at your vLLM endpoint; capabilities are model-dependent.

**vLLM** is a **model provider** — it registers models served by your local vLLM server (OpenAI-compatible) under Settings → Models.

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
**vLLM** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or `POST /api/apps {"source": ".../apps/vllm-models"}`.)

## Settings

| Key | Label | Notes |
|---|---|---|
| `endpoint` | vLLM Base URL | The OpenAI-compatible base URL of your vLLM server (e.g. http://localhost:8000). |
| `default_model` | Default Model | The model id served by your vLLM deployment. |

## License

MIT — see the apps repo [LICENSE](../LICENSE).
