# Ollama

OpenAI-compatible LLM and embedding provider via Ollama. Connect to local or remote Ollama instances for chat and embedding.

**Ollama** is a **model provider + local-model manager** — it registers Ollama chat/embedding models under Settings → Models and manages pulls from a local or remote Ollama instance.

## What this is

A standalone PersonalClaw app bundle (part of the core/app workspace split). It ships
as a self-contained directory:

- `app.json` — the manifest (identity, provider/backend/UI declarations, permissions).
- `provider.py` — the implementation, exposed via `create_provider`.
- `tests/` — the app's own tests.

It imports only the PersonalClaw **SDK** (never core internals), so core can evolve
without breaking it:

- `personalclaw.sdk.local_model`
- `personalclaw.sdk.model`

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**Ollama** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or `POST /api/apps {"source": ".../apps/ollama-models"}`.)

## Settings

| Key | Label | Notes |
|---|---|---|
| `endpoint` | Ollama Endpoint | Base URL of the Ollama API server. |
| `default_model` | Default Model | Model to use when no specific model is requested. Leave empty to use the first available. |
| `embedding_model` | Embedding Model | Ollama model to use for embedding operations. Leave empty to use sentence-transformers instead. |
| `timeout_secs` | Request Timeout | Maximum seconds to wait for a response from Ollama. |

## License

MIT — see the apps repo [LICENSE](../LICENSE).
