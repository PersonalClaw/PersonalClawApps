# Diarization (ONNX)

Non-gated speaker diarization ("who spoke when") via a sherpa-onnx segmentation + speaker-embedding pipeline. Install-and-go — no HuggingFace token. Provides a model for the diarization use-case; bind it in Settings → Models.

**Diarization (ONNX)** is a **model provider (diarization)** — it provides a speaker-diarization model for the diarization use-case; bind it in Settings → Models.

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
**Diarization (ONNX)** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or `POST /api/apps {"source": ".../apps/diarization-onnx"}`.)

## Settings

| Key | Label | Notes |
|---|---|---|
| `max_speakers` | Max speakers | Upper bound on distinct speakers (0 = auto-cluster). |

## License

MIT — see the apps repo [LICENSE](../LICENSE).
