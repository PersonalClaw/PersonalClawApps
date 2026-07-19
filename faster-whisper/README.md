# Faster Whisper

Speech-to-text using CTranslate2-optimized Whisper models. Download and manage STT models for voice input.

**Faster Whisper** is a **model provider (STT) + local-model manager** — it provides speech-to-text models for the stt use-case and manages their download/delete lifecycle; bind a model in Settings → Models.

## What this is

A standalone PersonalClaw app bundle (part of the core/app workspace split). It ships
as a self-contained directory:

- `app.json` — the manifest (identity, provider/backend/UI declarations, permissions).
- `provider.py` — the implementation, exposed via `create_provider`.
- `test_provider.py` — the app's own tests.

It imports only the PersonalClaw **SDK** (never core internals), so core can evolve
without breaking it:

- `personalclaw.sdk.local_model`
- `personalclaw.sdk.stt`

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**Faster Whisper** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or `POST /api/apps {"source": ".../apps/faster-whisper"}`.)

## Settings

| Key | Label | Notes |
|---|---|---|
| `device` | Compute Device | Hardware acceleration for inference. |
| `language_code` | Language | Primary language for transcription. Leave empty for auto-detection. |
| `streaming` | Streaming Mode | Enable real-time streaming transcription via WebSocket. |

## Setup notes

Transcription is biased by your Vocabulary/Lexicon terms within Whisper's prompt budget. Models download on demand and are managed (download/delete) from Settings → Models.

## License

MIT — see the apps repo [LICENSE](../LICENSE).
