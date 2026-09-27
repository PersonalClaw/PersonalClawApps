# Voice Clone TTS

Cloning-capable text-to-speech beside Piper: **zero-shot voice cloning** from a short
reference clip, run as an isolated **sidecar**. It needs the OmniVoice engine, which
PersonalClaw does not install for you: see [Installing the engine](#installing-the-engine).

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

## Installing the engine

Cloning needs the **OmniVoice** engine, a torch stack of several GB. **PersonalClaw does not
install it for you**: the Store installs this app without it, and the app stays unavailable
until the engine is in place. It goes in this app's own Python environment, which is where
the sidecar runs it, so none of it loads into the gateway:

```sh
cd ~/.personalclaw/apps/voice-clone-tts
# The Python PersonalClaw runs on: OmniVoice needs 3.10 or newer.
python3 -m venv venv
venv/bin/pip install omnivoice
```

Use your `PERSONALCLAW_HOME` in place of `~/.personalclaw` if you set one. The app's card in
**Settings → Providers** shows the same command with the exact paths for your machine. Then
press **Check again** on that card, and download **OmniVoice** in **Settings → Models**
(about 3.3 GB of weights).

Updating the app replaces its folder, and today that removes this environment with it. The
app then reads as unavailable again and its card shows the command again; the weights stay,
because they live in `models/tts-clone/` under your PersonalClaw home.

With no engine installed the app degrades quietly: `is_available()` is `False` and
`synthesize()` returns `None` (it never raises), so the manifest and contract tests run
everywhere.

## Status

The CORE half of the cloning capability (`supports_cloning`/`supports_voice_design` on
`CapabilityMatrix` and `TtsProvider`, and the `route_synthesis` /
`guard_synthesis_capability` 409 gate) merged as PersonalClaw/PersonalClaw#2351, and this app
consumes that contract. The engine spike chose OmniVoice; `worker.py` runs its zero-shot
inference in the sidecar, and the weights download resumes after an interruption.

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**Voice Clone TTS** — the install runs through the security scanner and lifecycle exactly
like any other app. (Or [install it from a shell](../docs/third-party-install.md#installing-from-a-shell).)

## License

MIT — see [LICENSE](./LICENSE).
