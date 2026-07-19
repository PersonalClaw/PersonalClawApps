# Piper TTS

Text-to-speech using Piper neural voice models. Download and manage TTS voices for audio output.

**Piper TTS** is a **model provider (TTS) + local-model manager** — it provides Piper neural voices for the tts use-case and manages voice download/delete; bind a voice in Settings → Models.

## What this is

A standalone PersonalClaw app bundle (part of the core/app workspace split). It ships
as a self-contained directory:

- `app.json` — the manifest (identity, provider/backend/UI declarations, permissions).
- `provider.py` — the implementation, exposed via `create_provider`.
- `test_provider.py` — the app's own tests.

It imports only the PersonalClaw **SDK** (never core internals), so core can evolve
without breaking it:

- `personalclaw.sdk.tts`
- `personalclaw.sdk.util`

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**Piper TTS** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or `POST /api/apps {"source": ".../apps/piper-tts"}`.)

## License

MIT — see the apps repo [LICENSE](../LICENSE).
