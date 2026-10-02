# Diarization (pyannote)

Higher-accuracy speaker diarization via the pyannote.audio pretrained pipeline. Requires a HuggingFace token + license acceptance. Provides a model for the diarization use-case; bind it in Settings → Models. Large install (pulls torch).

**Diarization (pyannote)** is a **model provider (diarization)** — it provides a speaker-diarization model for the diarization use-case; bind it in Settings → Models.

## What this is

A standalone PersonalClaw app bundle (part of the core/app workspace split). It ships
as a self-contained directory:

- `app.json` — the manifest (identity, provider/backend/UI declarations, permissions).
- `provider.py` — the implementation, exposed via `create_provider`.
- `worker.py` — the diarization itself, run in a process of its own (below).
- `test_provider.py` — the app's own tests.

It imports only the PersonalClaw **SDK** (never core internals), so core can evolve
without breaking it:

- `personalclaw.sdk.diarization`
- `personalclaw.sdk.sidecar` (`run_once`)

## How it runs

The pipeline computes with torch, which lets Python's interpreter lock go while it works, and
then groups the voices it heard with scipy's clustering, which holds the lock while it runs: for
a recording of an hour or two, long enough that a diarization inside the gateway would stop it
answering anything for a second or more at a time. Each diarization therefore runs `worker.py` in
a child process of its own, which loads the packages this app installs and exits when it has
answered. The gateway goes on serving meanwhile. A diarization stopped before it finishes (its
knowledge step out of time, the gateway stopping) stops the child with it.

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**Diarization (pyannote)** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or [install it from a shell](../docs/third-party-install.md#installing-from-a-shell).)

## Settings

| Key | Label | Notes |
|---|---|---|
| `hf_token` | HuggingFace Token | Required. Accept the pyannote/speaker-diarization-3.1 license on HuggingFace, then paste a read token. |

## Setup notes

Requires a HuggingFace token and acceptance of the pyannote model license on huggingface.co before the model can be downloaded.

The pipeline, and the models it pulls in when it first runs, download into the PersonalClaw home
(`models/diarization-pyannote`), and **Delete** in **Settings → Models** removes them from there.
Earlier releases kept them in the Hugging Face folder other tools share (`$HF_HOME`, or
`~/.cache/huggingface`). That folder is not read any more, so a pipeline that is only there
downloads once more, into the home.

## Network

Downloads the pyannote pipeline from `huggingface.co` when you download it; diarizing runs on this machine.

## License

MIT — see the apps repo [LICENSE](../LICENSE).
