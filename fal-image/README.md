# FAL

Image generation via FAL's hosted models (FLUX and others). Generate and edit images from a prompt; bind a FAL model to the Image Generation use case in Models. Needs a FAL API key (fal.ai).

**FAL** is a **model provider** — it registers under Settings → Models (here: image generation).

## What this is

A standalone PersonalClaw app bundle (part of the core/app workspace split). It ships
as a self-contained directory:

- `app.json` — the manifest (provider type + `implementation`; Tier-2 apps carry no `native` flag — that's Tier-1-only).
- `provider.py` — the implementation, exposed via `create_provider`.
- `test_provider.py` — the app's own tests.

It imports only the PersonalClaw **SDK** (never core internals), so core can evolve
without breaking it:

- `personalclaw.sdk.image`
- `personalclaw.sdk.settings`
- `personalclaw.sdk.net`

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**FAL** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or `POST /api/apps {"source": ".../apps/fal-image"}`.)

## Settings

| Key | Label | Notes |
|---|---|---|
| `api_key` | FAL API Key | Your FAL API key (fal.ai). Leave empty to fall back to the FAL_KEY / FAL_API_KEY environment variable. |

## License

MIT — see `LICENSE`.
