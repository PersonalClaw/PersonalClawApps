# Local Image Generation

Generate images entirely on your own machine. No cloud provider, no API key, and no
prompt or pixel leaves the host.

**Local Image Generation** is a **model provider** — it registers under
Settings → Models against the **Image Generation** use case, exactly as the hosted
image backends do.

## What this is

Every other image backend PersonalClaw can bind is somebody else's GPU. This bundle
closes that gap: with [ComfyUI](https://github.com/comfyanonymous/ComfyUI) running on
your machine and a checkpoint in its `models/checkpoints` directory, `image_generate`
renders locally and saves the result as a normal image artifact.

It ships as a self-contained directory:

- `app.json` — the manifest (`type: model`, `capabilities: ["image_gen"]`).
- `provider.py` — the `ImageGenProvider` implementation, exposed via `create_provider`.
- `test_provider.py` — the app's own rails.

It imports only the PersonalClaw **SDK**, never core internals:

- `personalclaw.sdk.image`
- `personalclaw.sdk.net`
- `personalclaw.sdk.settings`

## Setup

1. **Install and run ComfyUI.** Leave it on its default address,
   `http://127.0.0.1:8188`, or set yours in this app's settings.
2. **Pull a model.** Download **FLUX.1-schnell** (Apache-2.0, about 6.8 GB) into
   ComfyUI's `models/checkpoints` directory. PersonalClaw ships no model weights and
   fetches none — the model you run is the model you chose to download.
3. **Bind it.** Settings → Models → Image Generation → pick the local model.

Then ask for an image in chat. The artifact lands in Artifacts like any other.

## Install

From the App Store, add the apps directory as a **local source**, then install
**Local Image Generation** — the install runs through the security scanner and
lifecycle exactly like any other app. (Or
`POST /api/apps {"source": ".../local-image-gen"}`.)

## Settings

| Key | Label | Notes |
|---|---|---|
| `endpoint` | ComfyUI endpoint | Where your local ComfyUI listens. Default `http://127.0.0.1:8188`. Must be a loopback or private-network address — a public host is refused. |
| `steps` | Sampling steps | Denoising steps per image. Default 4, which suits FLUX.1-schnell's 1–4-step distillation. |

## Which model to use

PersonalClaw will only ever *recommend* a model whose licence is genuinely
OSI-permissive (Apache-2.0 or MIT). You can bind whatever ComfyUI can load, but the
catalog is explicit about which models we will not suggest and why.

| Model | Licence | Recommended? |
|---|---|---|
| **FLUX.1-schnell** | `apache-2.0` | **Yes — the default.** Best quality per gigabyte of the permissive set; 1–4 steps. |
| Qwen-Image | `apache-2.0` | Yes. Strongest at rendering legible text inside the image, but far larger (~40 GB). |
| Chroma | `apache-2.0` | Permissive, but its model card is flagged Not-For-All-Audiences, so it is offered and never suggested. |
| FLUX.1-dev | non-commercial | No — non-commercial weights. |
| SDXL 1.0 / SD 1.5 | OpenRAIL(++) | No — behavioural use restrictions; not OSI. |
| SD 3.5 Large | Stability Community | No — revenue-gated above $1M annual revenue. |
| SDXL-Turbo | `sai-nc-community` | No — non-commercial. |
| Sana 1.6B | tag says `apache-2.0` | No — the tag covers the code; the composed Gemma-2-2B-IT encoder binds Google's Gemma Terms and the card says research-only. |
| Janus-Pro-7B | tag says `mit` | No — the tag covers the code; the weights ship under the DeepSeek Model License. |

The last two are why the catalog records a licence tag and a disqualification reason
as separate facts: for Sana and Janus-Pro the tag is on the permissive allowlist and
the model is still disqualified, so a check that trusted the tag would wave both
through.

## Design notes

- **No second generation path.** The provider implements the existing
  `ImageGenProvider` ABC and is registered by the existing `type: model` manifest
  seam, so `image_generate` still runs through core's one audited dispatch. This app
  registers no tool, mounts no route, and writes no audit entry of its own.
- **No bundled weights.** The app is source only — a few tens of kilobytes. A rail
  fails the build if a model artifact, or any unexpectedly large file, appears in the
  bundle, so the model can never start counting against a packaging size budget.
- **Loopback-only egress.** The endpoint comes out of user config, so every request
  is made under a `loopback_only` egress policy with the resolved IP pinned and
  redirects disallowed. A local backend pointed at a public host would be an SSRF
  primitive wearing a local backend's name; this one refuses.
- **Calm when the model is missing.** A declared backend whose weights are not
  downloaded yet answers with a sentence saying what to pull — it never raises into
  the surface.

## Limitations

- **Text-to-image only.** Editing an existing image needs a per-checkpoint img2img
  graph; the provider declares `supports_edit=False` and refuses rather than
  approximating. Bind a cloud image model for edits.
- **Single-file checkpoints.** The graph uses ComfyUI's canonical
  `CheckpointLoaderSimple` path, so it loads all-in-one checkpoints (including the
  FLUX.1-schnell fp8 single file). A model that needs separate T5/CLIP/VAE nodes
  should be driven from your own ComfyUI workflow.
- **First run is slow.** Several gigabytes page off disk before the first denoising
  step. The poll ceiling is 15 minutes.

## License

MIT — see `LICENSE`.
