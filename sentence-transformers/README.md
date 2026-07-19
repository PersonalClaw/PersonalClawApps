# Sentence Transformers (local embeddings)

Local, in-process text embedding models via sentence-transformers (runs on your machine, no API key). Provides the embedding capability; bind a model in Settings → Models. Needs the sentence-transformers/torch package (server or container build).

**Sentence Transformers (local embeddings)** is a **model provider (embeddings) + local-model manager** — it provides local, in-process embedding models for the embedding use-case and manages their download/delete; bind a model in Settings → Models.

## What this is

A standalone PersonalClaw app bundle (part of the core/app workspace split). It ships
as a self-contained directory:

- `app.json` — the manifest (identity, provider/backend/UI declarations, permissions).
- `provider.py` — the implementation, exposed via `create_provider`.
- `test_provider.py` — the app's own tests.

It imports only the PersonalClaw **SDK** (never core internals), so core can evolve
without breaking it:

- `personalclaw.sdk.embedding`
- `personalclaw.sdk.local_model`

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**Sentence Transformers (local embeddings)** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or `POST /api/apps {"source": ".../apps/sentence-transformers"}`.)

## Setup notes

Needs the `sentence-transformers` Python package (declared as an app dependency; installed into the shared venv at install time — a fresh dependency requires a gateway restart). Models run fully locally; no API key.

## License

MIT — see the apps repo [LICENSE](../LICENSE).
