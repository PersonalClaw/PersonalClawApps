# Diarization (pyannote)

Higher-accuracy speaker diarization via the pyannote.audio pretrained pipeline. Requires a HuggingFace token + license acceptance. Provides a model for the diarization use-case; bind it in Settings → Models. Large install (pulls torch).

**Diarization (pyannote)** is a **model provider (diarization)** — it provides a speaker-diarization model for the diarization use-case; bind it in Settings → Models.

## What this is

A standalone PersonalClaw app bundle (part of the core/app workspace split). It ships
as a self-contained directory:

- `app.json` — the manifest (identity, provider/backend/UI declarations, permissions).
- `provider.py` — the implementation, exposed via `create_provider`.
- `test_provider.py` — the app's own tests.

It imports only the PersonalClaw **SDK** (never core internals), so core can evolve
without breaking it:

- `personalclaw.sdk.diarization`

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**Diarization (pyannote)** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or `POST /api/apps {"source": ".../apps/diarization-pyannote"}`.)

## Settings

| Key | Label | Notes |
|---|---|---|
| `hf_token` | HuggingFace Token | Required. Accept the pyannote/speaker-diarization-3.1 license on HuggingFace, then paste a read token. |

## Setup notes

Requires a HuggingFace token and acceptance of the pyannote model license on huggingface.co before the model can be downloaded.

## License

MIT — see the apps repo [LICENSE](../LICENSE).
