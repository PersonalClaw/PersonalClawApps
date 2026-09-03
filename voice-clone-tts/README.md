# Voice Clone TTS

Cloning-capable text-to-speech beside Piper: **zero-shot voice cloning** from a short
reference clip, run as an isolated **sidecar**.

**Voice Clone TTS** is a **model provider (TTS) + local-model manager**. Unlike Piper
(a fixed voice bank), it conditions synthesis on a reference clip — the
`ref_audio`/`ref_text` a *clone-kind* voice profile resolves to — so it can render "your
own voice" on your own machine. It declares `supports_cloning`, so a clone-kind profile
routes here instead of being refused with `409 cloning_unsupported:<provider>`.

## What this is

A standalone PersonalClaw app bundle. It ships as a self-contained directory:

- `app.json` — the manifest. `provider.execution: "sidecar"` runs the torch-heavy engine
  in a child process, so a mid-synthesis crash leaves the gateway up with a typed reason
  (LOCAL-MODEL-MANAGER-V2 §3 machinery).
- `catalog.json` — the declarative model cards (`runtime: "torch"`, `matrix.supports_cloning`).
  Adding or pruning an engine model is a file drop, not a code change.
- `provider.py` — the implementation, exposed via `create_provider`.
- `test_provider.py` — the app's own tests.

It imports only the PersonalClaw **SDK** (`personalclaw.sdk.tts`), never core internals,
so core can evolve without breaking it.

## The engine is optional

The cloning engine is a **multi-GB torch dependency** and is **not** pinned in
`app.json` — it is detected lazily at runtime. With no engine installed the app degrades
gracefully: `is_available()` is `False` and `synthesize()` returns `None` (never raises),
so the manifest/contract tests run everywhere. Install an engine and download a model
card's weights to enable cloning.

## Status (roadmap atom MI-2b)

This bundle is the **APPS half** of atom MI-2. The CORE half — MI-2a, the cloning
*capability surface* (`supports_cloning`/`supports_voice_design` on `CapabilityMatrix`
and `TtsProvider`; the `route_synthesis` / `guard_synthesis_capability` 409 gate) —
merged as PersonalClaw/PersonalClaw#2351, and this app consumes that contract.

**Deferred to MI-2c** (needs a working engine spike that does not fit MI-2b's scope):

- The OmniVoice-vs-CosyVoice spike on fixtures (clip length, MPS latency, RAM) that picks
  **one** engine and records the loser's notes in the plan dir; this bundle ships both as
  *candidate* catalog cards until then.
- Real zero-shot inference in `synthesize` (the engine API is pinned by the spike).
- Real resumable weight download and the LMM-V2 through-clone selftest.

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**Voice Clone TTS** — the install runs through the security scanner and lifecycle exactly
like any other app. (Or `POST /api/apps {"source": ".../apps/voice-clone-tts"}`.)

## License

MIT — see [LICENSE](./LICENSE).
