# Mistral AI

Mistral AI models via its OpenAI-compatible endpoint. Bring your own Mistral API key.

**Mistral AI** is a **model provider** — it registers Mistral AI models (OpenAI-compatible endpoint) under Settings → Models.

## What this is

A standalone PersonalClaw app bundle (part of the core/app workspace split). It ships
as a self-contained directory:

- `app.json` — the manifest (identity, provider/backend/UI declarations, permissions).
- `provider.py` — the implementation, exposed via `create_provider`.
- `test_catalog.py`, `test_provider.py` — the app's own tests.
- `test_wire.py` — proves a call's per-call temperature and output budget reach the request, against
  a recording endpoint.

It imports only the PersonalClaw **SDK** (never core internals), so core can evolve
without breaking it:

- `personalclaw.sdk.model`
- `personalclaw.sdk.net`

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**Mistral AI** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or [install it from a shell](../docs/third-party-install.md#installing-from-a-shell).)

## Settings

| Key | Label | Notes |
|---|---|---|
| `api_key` | Mistral AI API Key | Your Mistral AI API key. Leave empty to fall back to the MISTRAL_API_KEY environment variable. |
| `default_model` | Default Model | A Mistral model id. The model this instance answers with when nothing in Settings → Models names one. Leave it empty to choose its models in Settings → Models. |
| `endpoint` | Base URL | Optional override of the Mistral AI base URL. Empty uses https://api.mistral.ai/v1. |

## The model list

Settings → Models lists the models Mistral's `/v1/models` names for your key, each offered for
what Mistral's own record of it says (its `capabilities`): a model that answers a chat is a chat
model, and an image or audio one too where it reads images or audio; one that transcribes is
also offered for speech-to-text; an embedding model (`mistral-embed`, `codestral-embed`) for
Embedding. OCR, moderation and classifier models, and models that only transcribe live or only
speak, are offered for nothing: nothing here can drive them.

## Network

Reaches `api.mistral.ai`, or the endpoint you set in **Base URL**.

## License

MIT — see the apps repo [LICENSE](../LICENSE).
