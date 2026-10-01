# Google Gemini

Google Gemini via its OpenAI-compatibility endpoint. Bring your own Gemini API key.

**Google Gemini** is a **model provider** — it registers Google Gemini models (OpenAI-compatibility endpoint) under Settings → Models.

## The model list

Settings → Models lists the models the OpenAI-compatible endpoint names, each offered for what
Gemini's own record of it says (its `supportedGenerationMethods`): a model that generates content
is a chat model, or an image or speech model where its id says it makes those; one that embeds
is an embedding model; Imagen and Veo models make images and video. A model served only through
the Live API (`bidiGenerateContent`) and AQA (`generateAnswer`) are offered for nothing: nothing
here speaks those.

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
- `personalclaw.sdk.image`
- `personalclaw.sdk.video`
- `personalclaw.sdk.net`

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**Google Gemini** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or [install it from a shell](../docs/third-party-install.md#installing-from-a-shell).)

## Settings

| Key | Label | Notes |
|---|---|---|
| `api_key` | Google Gemini API Key | Your Google Gemini API key. Leave empty to fall back to the GEMINI_API_KEY environment variable. |
| `default_model` | Default Model | A Gemini model id. The model this instance answers with when nothing in Settings → Models names one. Leave it empty to choose its models in Settings → Models. |
| `endpoint` | Base URL | Optional. Where chat, embeddings, their model list and the connection check go (Gemini's OpenAI-compatible API). Empty uses https://generativelanguage.googleapis.com/v1beta/openai/. Image, video and speech always use Google's own address. |

## Network

Reaches `generativelanguage.googleapis.com`, or the endpoint you set in **Base URL**.

## License

MIT — see the apps repo [LICENSE](../LICENSE).
