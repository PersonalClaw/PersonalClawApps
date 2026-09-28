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
any other app. (Or [install it from a shell](../docs/third-party-install.md#installing-from-a-shell).)

## Settings

| Key | Label | Notes |
|---|---|---|
| `device` | Compute Device | Hardware acceleration for inference. |
| `language_code` | Language | Primary language for transcription. Leave empty for auto-detection. |
| `streaming` | Streaming Mode | Enable real-time streaming transcription via WebSocket. |

## Setup notes

Transcription is biased by your Vocabulary/Lexicon terms within Whisper's prompt budget. Models download on demand and are managed (download/delete) from Settings → Models.

Models download into the PersonalClaw home (`models/stt`). A model already in the Hugging Face
folder other tools share (`$HF_HOME`, or `~/.cache/huggingface`) is used in place, without a
download, once you turn that folder on in **Settings → Security → Outside PersonalClaw's
home**. PersonalClaw only reads it: nothing is downloaded to, changed in or deleted from that
folder, so Delete removes the home's copy only, and the model's row says when it is read from
there.

## Network

Downloads Whisper models from `huggingface.co` when you download one; transcribing runs on this machine.

## License

MIT — see the apps repo [LICENSE](../LICENSE).
