# OpenRouter

OpenRouter via its OpenAI-compatible API — one key for hundreds of models across
providers. Bring your own OpenRouter API key.

**OpenRouter** is a **model provider** — it registers OpenRouter's models under
Settings → Models. A single instance serves chat, embedding, image generation, and
video generation.

## What this is

A standalone PersonalClaw app bundle (part of the core/app workspace split). It ships
as a self-contained directory:

- `app.json` — the manifest (identity, provider declaration, permissions).
- `provider.py` — the implementation, exposed via `create_provider`.
- `test_catalog.py`, `test_provider.py` — the app's own tests.

It imports only the PersonalClaw **SDK** (never core internals), so core can evolve
without breaking it:

- `personalclaw.sdk.model`
- `personalclaw.sdk.image`
- `personalclaw.sdk.video`
- `personalclaw.sdk.net`

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**OpenRouter** — the install runs through the security scanner and lifecycle exactly
like any other app. (Or `POST /api/apps {"source": ".../apps/openrouter-models"}`.)

## Settings

| Key | Label | Notes |
|---|---|---|
| `api_key` | OpenRouter API Key | Your OpenRouter API key (openrouter.ai/keys). Leave empty to fall back to the `OPENROUTER_API_KEY` environment variable. |
| `default_model` | Default Model | An OpenRouter model id (e.g. `anthropic/claude-sonnet-4.5`). Empty = resolved from live `/v1/models` discovery. |
| `endpoint` | Base URL | Optional override of the OpenRouter base URL. Empty uses `https://openrouter.ai/api/v1`. |

## Capabilities

Bind these under Settings → Models. One instance backs all of them, so every
capability uses the same account and key.

| Use case | Wire surface | Notes |
|---|---|---|
| `chat` (+ `code_tools`, `streaming`) | `POST /chat/completions` | OpenAI-compatible, via core's `OpenAIProvider`. Tool-calling and SSE streaming both supported. |
| `image_modality` (vision) | same endpoint, `image_url` content parts | Image **input**. Models are tagged from OpenRouter's declared `input_modalities`, so vision models are found by capability rather than by guessing from the model id. |
| `embedding` | `POST /embeddings` | 30 embedding models. Round-trip verified live. |
| `image_gen` | `POST /images` | Generation **and** editing (via `input_references`). Base64 response; core's artifact store persists the bytes. |
| `video_gen` | `POST /videos` → poll → `/content` | Async submit/poll/download, owned inside `generate()`. Returns a local file path. |

Multi-instance: add one instance per OpenRouter account. Each media adapter is keyed
by its config-entry name, so an `<instance>:<model>` binding always resolves to the
account that backs that instance's chat.

## Notes on discovery

OpenRouter's default `GET /models` listing is **text-only** — it returns 367 chat
models and silently omits every image, video, and embedding model. This app therefore
never calls the bare route. It uses:

- `GET /models?output_modalities=text,embeddings` — chat + embedding (397 models)
- `GET /images/models` — image generation (38 models, with per-model parameter caps)
- `GET /videos/models` — video generation (17 models, with per-model capability arrays)

Per-model caps are honored rather than assumed: `n` is clamped to the model's own
maximum (which ranges 1–10, well below the schema maximum), a video `duration` is
snapped to a value in that model's `supported_durations`, and an aspect ratio absent
from `supported_aspect_ratios` is not sent. Requesting a parameter a model doesn't
advertise is a 400 upstream, so an unadvertised parameter is omitted entirely.

Discovery lists are cached for 5 minutes per key. A transient failure degrades to the
last good list; having never succeeded, the picker is honestly **empty** rather than
showing model ids the key cannot actually call.

### Image sizes

The one `size` field core exposes maps onto whichever of OpenRouter's three
geometry keys fits the value, and only ever one of them (`size` + `aspect_ratio`
together is a hard 400):

| You pass | Sent as | Result |
|---|---|---|
| `1024x1024` | `size` | Exactly those pixels. |
| `1K` / `2K` / `4K` | `resolution` | The model's own idea of that tier (e.g. `1K` → 1408×768). |
| `16:9` | `aspect_ratio` | That ratio at the model's default size. |
| anything else | *omitted* | The model's default, rather than a guessed token. |

A pixel size is resolved upstream to a resolution tier, and a tier the model
doesn't list is a 400. That mapping is not a published rule and is **not** a simple
function of the dimensions (measured: `1024x1024` → 1K, but `1408x768` → 2K), so no
mapping is attempted here — instead the error names the sizes the model *does*
accept, since upstream's own text only names the tier it computed.

## Limitations

- **No mask/inpainting.** OpenRouter's `/images` has no mask parameter, so `edit`
  with a mask raises a typed error rather than silently returning a whole-image edit.
- **No chat-path attribution header.** `X-OpenRouter-Title` is sent on the image and
  video calls, which build their own headers. Core's `OpenAIProvider` exposes no
  `default_headers` seam, so the chat path sends none (they are optional upstream).
- **Rate limits.** A 429 is retried exactly once, honoring `Retry-After` (clamped to
  1–30s). A second 429 surfaces as an error rather than an unbounded backoff.

## Validation status

Validated end to end against the live API with a funded key:

- **Chat** — streamed completion, and **vision** (image input via an `image_url`
  content part) on a model tagged from its declared `input_modalities`.
- **Embedding** — `/embeddings` round-trip returning real vectors. This is what
  backs the manifest's `embedding` capability (and `supports_embeddings` on the
  registered provider type), so the claim is now measured rather than assumed.
- **Image generation** — plain, with an `aspect_ratio` size, and with a `resolution`
  size; every response decodes to a valid image.
- **Image editing** — `input_references` verified by *content*: a red source square
  came back green, so the reference reached the model rather than being ignored.
  A mask is refused pre-flight as designed.
- **Video** — full submit → poll → download on `bytedance/seedance-1-5-pro`: a
  4.05s 960×960 H.264+AAC MP4 at 1.9 MB, well under the 256 MB cap. It took **133s**
  for a 4-second clip on a *fast* model, which is the concrete reason the poll
  ceiling is 600s and not 300s.
- **Error surfacing** — 401/402/413/429/502/524 all produce actionable text, and the
  401 path correctly ignores OpenRouter's misleading `No cookie auth credentials
  found` body.
- **Connection test** — a bad key is rejected. `GET /models` cannot be used for this:
  it is public, returning 200 with a garbage key and with no key at all, so the probe
  hits the authenticated `GET /key` instead.

Not validated, and not depended on: whether `unsigned_urls` from a completed video
job require the `Authorization` header, and how long they stay valid. Nothing in
this app branches on the answer (v1 always downloads via `/content` with the key
attached).

## License

MIT — see the apps repo [LICENSE](../LICENSE).
