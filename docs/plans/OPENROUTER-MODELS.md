# OPENROUTER-MODELS — implementation plan

**Status:** PLAN ONLY — no production code written yet.
**Target repo:** `PersonalClawApps` (this repo). New bundle: `openrouter-models/`.
**Core repo referenced (read-only):** `/Users/golani/PersonalProjects/PersonalClaw/PersonalClaw`.
**Date:** 2026-07-28.

## Where this file lives, and why

There was **no `docs/plans/` convention in this repo before this plan**. Verified:
`PersonalClawApps/docs/` contained exactly four files —
`app-creation-guide.md`, `platform-architecture.md`, `SLACK_SETUP.md`,
`third-party-install.md` — and `git log --oneline -5 -- docs/` shows only two
commits touching `docs/` (`119b917` initial, `77f26ff` SECURITY cross-link). No
per-app or per-feature plan file exists anywhere in the apps repo.

I created `docs/plans/OPENROUTER-MODELS.md` because that is the **exact path the
task specified**, and because it mirrors the convention the core repo already uses
(`PersonalClaw/docs/roadmap/plans/*.md` — SCREAMING-KEBAB filenames, e.g.
`ACP-AGENT-PARITY.md`, `AGENT-PACKS.md`). So: new directory, naming borrowed from
core's plans directory. If the owner prefers plans to live only in core's roadmap
tree, this file moves there unchanged.

---

## 1. Scope + non-goals

### Ships (one app, one provider object, one credential)

| Capability | Wire surface | Contract implemented |
|---|---|---|
| Chat + streaming + tool-calling | `POST {base}/chat/completions` via the `openai` SDK | core's `OpenAIProvider`, wired by `register_branded_app` |
| Vision (image **input**) | same endpoint, `{"type":"image_url","image_url":{"url":…}}` content parts | `Capability.VISION` advertisement only — no app code; the composer already builds these parts |
| Image **generation** | `POST {base}/images` | `ImageGenProvider` (`personalclaw.sdk.image`) |
| Video **generation** | `POST {base}/videos` → poll `GET {base}/videos/{id}` → `GET {base}/videos/{id}/content?index=0` | `VideoGenProvider` (`personalclaw.sdk.video`) |

Base URL: `https://openrouter.ai/api/v1`. Auth: `Authorization: Bearer <key>`.
Optional attribution headers `HTTP-Referer` + `X-OpenRouter-Title` (the plan sends
`X-OpenRouter-Title: PersonalClaw` and omits `HTTP-Referer`; `X-Title` is the
legacy alias and is NOT used).

### Explicit non-goals for v1

| Out of scope | One-line reason |
|---|---|
| Legacy chat-image path (`/chat/completions` + `modalities:["text","image"]`, images at `choices[0].message.images[].image_url.url`) | New image models are added **exclusively** to `/api/v1/images`; carrying both is a dual path, which the clean-break tenet forbids. |
| The undocumented `/api/v1/images/generations` alias | Undocumented alias with no stability contract; verified it exists (returns the same `401` envelope as `/images`) but we build on the documented route only. |
| `openai` SDK `images.generate()` for the image path | OpenRouter's `/images` body is NOT OpenAI-Images-shaped (`resolution`/`aspect_ratio`/`input_references` instead of `size`-only + `image[]`), so the SDK helper would silently send the wrong shape. Image path uses guarded raw HTTP. |
| **Embeddings** | OpenRouter **does** serve them — verified: `GET /api/v1/models?output_modalities=all` returns **30** models with `architecture.output_modalities == ["embeddings"]` (e.g. `google/gemini-embedding-2`, `nvidia/nemotron-3-embed-1b:free`, `perplexity/pplx-embed-v1-4b`). Deferred anyway: `/embeddings` request/response shape on OpenRouter is unverified from this machine (needs a key), and `Capability.EMBEDDING` on the spec changes `supports_embeddings` on the registered type (`provider_helpers.py:325`) — a claim we should only make after a live round-trip. Tracked as open question Q8. |
| The Beta server-side "web search"/tool plugin | A vendor-specific chat plugin, not a capability in core's `CAPABILITIES` vocabulary; belongs in a later slice if at all. |
| Webhooks / `callback_url` for video | v1 polls. The `VideoGenProvider` ABC docstring (`video_gen/provider.py:77-83`) *requires* the provider to own its poll loop inside `generate`; a callback would need an inbound HTTP route this app does not have (and `permissions.network` is the only network grant we take). |
| Zero Data Retention posture toggles | Video is not ZDR-eligible upstream; nothing for the app to configure. |
| STT / TTS | OpenRouter lists `speech` (15) and `transcription` (12) output modalities, but neither has a verified dedicated endpoint shape; and adding the type to `OPENAI_FAMILY_TYPES` (the thing that would wire core's free adapters) is explicitly rejected below. Separate future slice. |

---

## 2. File-by-file plan

All paths relative to `/Users/golani/PersonalProjects/PersonalClaw/PersonalClawApps`.

| Path | Contents | Modeled on |
|---|---|---|
| `openrouter-models/app.json` | The manifest (§3 below, verbatim). One `provider` object, `type:"model"`, `providerType:"openrouter"`, `multiInstance:true`, 7 capability strings, 3-field `settingsSchema`, `pythonDependencies:["openai>=1.0"]`, `permissions:{network:true}`. | `google-models/app.json` (the 5-capability, `providerType`-bearing, multiInstance manifest) — verified at `google-models/app.json:21-35`. |
| `openrouter-models/provider.py` | Everything: the `BrandedProviderSpec` + `register_branded_app` chat wiring, `OpenRouterImageProvider`, `OpenRouterVideoProvider`, the TTL-cached discovery helpers, `create_provider`, the `_openrouter_entries` filter, the two media scanners, and the two `register_scanner` calls. Single module — no `server.py`, no per-app `LICENSE` (the repo root `LICENSE` covers it, matching `google-models/` which ships neither). | `google-models/provider.py` end-to-end: the SPEC block (`:64-78`), the shared TTL discovery cache (`:118-150`), the `name` property **with setter** (`:191-197`), the poll loop inside `generate` (`:436-437`, `:475-530`), the entries filter (`:722-728`), the name-keyed scanners (`:735-753`), and the guarded `register_scanner` block (`:756-763`). |
| `openrouter-models/README.md` | Identity paragraph, "What this is" file list, Install (Store local source + `POST /api/apps`), a Settings table generated from `settingsSchema`, a **Capabilities** table naming the three use-cases to bind (`chat`, `image_gen`, `video_gen`), and the License line pointing at `../LICENSE`. | `google-models/README.md` / `together-models/README.md` (identical skeleton, verified both). |
| `openrouter-models/test_provider.py` | Chat-spec + image/video adapter unit tests (§8). Autouse fixture stubbing `openai` into `sys.modules`; `import provider as prov` for the import-time side effect. | `google-models/test_provider.py` (the `_stub_openai` autouse fixture at lines 16-28, then `import provider as prov` at line 31). |
| `openrouter-models/test_catalog.py` | Catalog tests: plain `ModelCatalog` (not `ModelManager`), empty picker on unreachable endpoint, live models win, `test_connection` needs a key. | `google-models/test_catalog.py` verbatim structure (the triple `monkeypatch.setattr` of `fetch` across `personalclaw.net.client` / `personalclaw.sdk.net` / `personalclaw.net`). |

**No core-repo files change.** Both landmines are resolved without touching core
(§7). That keeps this a pure apps-repo PR, which is what the apps AGENTS.md
demands ("A platform-level change — that belongs in the core repo, not here").

---

## 3. `app.json` — full, ready to paste

```json
{
  "name": "openrouter-models",
  "version": "0.1.0",
  "displayName": "OpenRouter",
  "description": "OpenRouter — one key for hundreds of models across providers. Chat (with image input), image generation, and video generation from a single instance. Bring your own OpenRouter API key.",
  "icon": "Route",
  "author": "PersonalClaw",
  "tags": [
    "model",
    "llm",
    "image_gen",
    "video_gen"
  ],
  "dependencies": {
    "pythonDependencies": [
      "openai>=1.0"
    ]
  },
  "permissions": {
    "network": true
  },
  "provider": {
    "type": "model",
    "providerType": "openrouter",
    "implementation": "provider:create_provider",
    "multiInstance": true,
    "capabilities": [
      "chat",
      "code_tools",
      "streaming",
      "vision",
      "image_modality",
      "image_gen",
      "video_gen"
    ],
    "settingsSchema": {
      "type": "object",
      "properties": {
        "api_key": {
          "type": "string",
          "default": "",
          "x-meta": {
            "label": "OpenRouter API Key",
            "help": "Your OpenRouter API key (openrouter.ai/keys). Leave empty to fall back to the OPENROUTER_API_KEY environment variable.",
            "sensitive": true
          }
        },
        "default_model": {
          "type": "string",
          "default": "",
          "x-meta": {
            "label": "Default Model",
            "help": "An OpenRouter model id (e.g. anthropic/claude-sonnet-4.5). Empty = resolved from live /v1/models discovery."
          }
        },
        "endpoint": {
          "type": "string",
          "default": "",
          "x-meta": {
            "label": "Base URL",
            "help": "Optional override of the OpenRouter base URL. Empty uses https://openrouter.ai/api/v1.",
            "tags": [
              "advanced"
            ]
          }
        }
      }
    }
  }
}
```

### Field-by-field justification

**`provider.type: "model"`** — the only legal value here. Verified
`PROVIDER_TYPES` at `src/personalclaw/apps/manifest.py:581-598`: the 14 entity
classes are `model, agent, task, channel, inbox, skills, knowledge, memory,
notification, tool, workflow, search, action, prompt`. `image`/`video`/`stt`/`tts`
/`embedding` are **not** provider types — they are capability strings.

**`provider.providerType: "openrouter"`** — this is the concrete LLM-registry
type, and it is load-bearing. `api_provider_types_list`
(`src/personalclaw/dashboard/handlers/providers.py:113-119`) reads
`cfg.providerType` and explicitly documents that `cfg.type` is the entity class
`"model"`, not the registry type; the fallback is
`ext.manifest.name.replace("-models", "")` which would coincidentally also yield
`"openrouter"` — but declaring it is what `bedrock-models`/`google-models` do
(`bedrock-models/app.json:23`, `google-models/app.json:23`), so we declare it.

**Capabilities** — each checked against the real vocabularies:

| String | Source of truth | Why |
|---|---|---|
| `chat` | `CAPABILITIES[0]` at `use_cases.py:45`; `Capability.CHAT` at `llm/capabilities.py:15` | The chat use case. |
| `code_tools` | `CHAT_SUBCATEGORIES[0]` at `use_cases.py:83`; `Capability.CODE_TOOLS` at `capabilities.py:16` | OpenAI-wire tool-calling works through OpenRouter; `register_branded_app` maps this to `supports_tools` (`provider_helpers.py:324`). |
| `streaming` | `Capability.STREAMING` at `capabilities.py:21` | SSE streaming is supported on `/chat/completions`. |
| `vision` | `Capability.VISION` at `capabilities.py:20` | Image **input** via `image_url` content parts. Maps to `supports_vision` (`provider_helpers.py:326`). |
| `image_modality` | `CAPABILITIES` at `use_cases.py:52` | The *use-case* name for "understands images", distinct from `vision` (the provider *advertisement*). It is one of `MULTI_ACTIVE_USE_CASES` (`use_cases.py:99`), so declaring it puts OpenRouter's vision models in the image-understanding picker pool. Verified: `?output_modalities=all` shows **11** models with `output_modalities == ["image","text"]` and many text models with `image` in `input_modalities`. |
| `image_gen` | `CAPABILITIES` at `use_cases.py:53` | Drives `ModelTypeHandler.register`'s `if "image_gen" in caps` branch (`providers/registry.py:570-576`), isinstance-guarded against `ImageGenProvider`. |
| `video_gen` | `CAPABILITIES` at `use_cases.py:59` | Drives the `if "video_gen" in caps` branch (`providers/registry.py:577-583`), isinstance-guarded against `VideoGenProvider`. |

Deliberately **absent**: `embedding` (deferred, §1 + Q8), `stt`, `tts`,
`audio_modality`, `video_modality`, `diarization`, `audio_gen` — none is verified
live, and `use_cases.py:44-59` is the closed vocabulary they'd have to come from.

**`multiInstance: true`** — matches `google-models`/`bedrock-models`/
`together-models`. One instance per OpenRouter account/key. `ModelTypeHandler.create`
(`providers/registry.py:488-511`) then builds one provider per enabled instance
and passes `inst.config` (which carries `api_key`) straight into the factory.

**`implementation: "provider:create_provider"`** — the required
`module.path:factory_fn` form (`manifest.py:650-656`, `_HOOK_OR_ENTRYPOINT_RE`).

**`dependencies.pythonDependencies: ["openai>=1.0"]`** — `register_branded_app`
with `protocol="openai"` builds core's `OpenAIProvider`, which lazily
`require_sdk("openai", …)` at construction (`llm/openai.py:73`). Every
OpenAI-compatible app in this repo declares exactly this (`together-models`,
`google-models`, `groq-models`, `deepseek-models`, `mistral-models`,
`openai-compatible`, `vllm-models`, `alibaba-models` — verified by reading all
`*/app.json`). The image/video adapters use `personalclaw.sdk.net.fetch`, which
brings its own `aiohttp` from core — **no extra dependency**, and CI's `tests` job
stubs `openai` so the bundle still imports without it.

**`permissions: {"network": true}`** — the minimum. `Permissions`
(`manifest.py:274-318`) has `api`, `events`, `mcpTools`, `storage`, `network`,
`memory`, `cron`, `agent`. This app needs no gateway API prefixes, no events, no
MCP tools, no app storage (generated bytes land in the artifact store via core),
no memory, no cron, no background agent — only outbound HTTP. Note the two apps
that declare permissions today (`growth`, `minutes`) both set `network:false`
and the model apps declare none at all; declaring `network:true` here is the
honest minimum for an app whose entire job is outbound calls.

**`icon: "Route"`** — a real lucide name (`manifest.py:748-751` requires a lucide
name, never an emoji). Distinct from `Sparkle`(google), `Boxes`(together),
`Image`(fal), `Cloud`(bedrock).

---

## 4. Chat provider — the exact `BrandedProviderSpec` call

```python
from personalclaw.sdk.model import BrandedProviderSpec, Capability, register_branded_app

_BASE = "https://openrouter.ai/api/v1"

SPEC = BrandedProviderSpec(
    type="openrouter",
    protocol="openai",
    default_base_url=_BASE,
    api_key_env="OPENROUTER_API_KEY",
    default_model="",          # de-hardcoded: resolved from live /v1/models at start()
    max_tokens=None,           # openai-wire: leave unset (anthropic-wire needs a value)
    capabilities=frozenset({
        Capability.CHAT,
        Capability.CODE_TOOLS,
        Capability.STREAMING,
        Capability.VISION,
    }),
    fallback_models=(),        # de-hardcode directive: unreachable endpoint => EMPTY picker
    notes="OpenRouter — one key for hundreds of models across providers (OpenAI-compatible). "
          "Bring your own OpenRouter API key.",
)

_factory, _create_chat_provider, create_catalog = register_branded_app(SPEC)
```

Every field decided, against `BrandedProviderSpec` at
`src/personalclaw/sdk/provider_helpers.py:48-60`:

- `type="openrouter"` — must equal `app.json`'s `providerType` so
  `canonical_provider_type` (an identity map, `llm/registry.py:329-336`) resolves
  a `config.json` entry of `type:"openrouter"` to this registered type. It also
  means `_original_type` is **not** stamped for our entries (`llm/registry.py:398-399`
  only stamps when `ptype != registry_type`) — the scanner filter still tolerates it
  (§7).
- `protocol="openai"` — OpenRouter is OpenAI-compatible; this selects
  `OpenAIProvider` in `_build_provider` (`provider_helpers.py:98-104`).
- `default_base_url=_BASE` — note `openai_compatible_list_models`
  (`llm/catalog.py:331-333`) appends `/v1` **only if the base doesn't already end
  in `/v1`**; `https://openrouter.ai/api/v1` does, so discovery hits
  `https://openrouter.ai/api/v1/models` — verified live: HTTP 200, 358 models.
- `api_key_env="OPENROUTER_API_KEY"` — consulted third in the credential order
  documented at `provider_helpers.py:252-265` (explicit credential-store descriptor
  → per-instance `options.api_key` → env → anon placeholder). Per-instance key
  wins, which is the correct behavior for a multi-account setup.
- `default_model=""` — the de-hardcode directive. `OpenAIProvider.start()`
  (`llm/openai.py:95-108`) resolves the first chat-capable model from live
  `/v1/models` when unpinned.
- `fallback_models=()` — with `default_model=""`, `BrandedCatalog._fallback()`
  (`provider_helpers.py:129-152`) returns `[]`, so an unreachable/keyless endpoint
  yields an **honestly empty** picker, never a fake id. This is locked by a test
  (§8, `test_empty_list_when_endpoint_unreachable`).

**Wiring** (`provider_helpers.py:230-336`): `register_branded_app` registers the
provider TYPE + the catalog as an import-time side effect, idempotent against
reload, and returns the trio. The manifest's
`implementation: "provider:create_provider"` resolves to a thin wrapper so the
factory returns ONLY the chat provider (the google-models shape):

```python
def create_provider(config: dict[str, Any] | None = None):
    """Chat provider factory (multi-instance). ONE openrouter config entry serves
    chat + image_gen + video_gen; the media adapters are built per-config-entry by
    the scanners below, NOT returned here — so the app surfaces as ONE provider."""
    return _create_chat_provider(config or {})
```

Chosen over fal-image's list-returning factory (`fal-image/provider.py:479-492`)
even though `ModelTypeHandler.register` normalizes both
(`providers/registry.py:519`): the scanner path is what keys each media adapter
by the **config entry name**, which is what makes `<instance-name>:model`
bindings resolve to the same account as chat. fal-image hardcodes
`name → "fal"` (`fal-image/provider.py:255-257`) and therefore cannot support two
accounts — and its `name` is a read-only property, so `ModelTypeHandler`'s
`if not hasattr(provider, "name")` rename (`registry.py:500-501`) can't fix it.

### Attribution headers

`OpenAIProvider.__init__` (`llm/openai.py:88-91`) constructs
`openai.AsyncOpenAI(api_key=…, base_url=…)` and passes **no** `default_headers`,
and `extra_options` are per-request call kwargs (`llm/openai.py:166-168`), not
client headers. So there is **no seam to inject `X-OpenRouter-Title` on the chat
path** without either an SDK-boundary violation or a core change. Decision:
**chat sends no attribution headers in v1** (they are optional; OpenRouter works
without them). The image/video paths *do* send `X-OpenRouter-Title: PersonalClaw`
because they build their own headers. Recorded as open question Q9 (would need a
core `BrandedProviderSpec.default_headers` field to fix properly).

---

## 5. Image provider design

```python
_IMAGE_TIMEOUT_S = 120.0      # matches google-models/provider.py:60 and alibaba-models:42
_DISCOVERY_TTL_S = 300.0      # matches google-models/provider.py:118

class OpenRouterImageProvider(ImageGenProvider):
    def __init__(self, *, api_key: str = "", endpoint: str = "", name: str = "openrouter") -> None: ...

    @property
    def name(self) -> str: ...
    @name.setter                      # REQUIRED — see below
    def name(self, value: str) -> None: ...

    @property
    def display_name(self) -> str:    # "OpenRouter (image)"

    def _key(self) -> str:            # self._api_key or os.environ.get("OPENROUTER_API_KEY", "")
    def _base(self) -> str:           # self._endpoint or _BASE

    async def is_available(self) -> bool
    async def list_models(self) -> list[ImageGenModel]
    async def generate(self, prompt, *, model="", size="", n=1, **opts) -> list[ImageResult]
    async def edit(self, prompt, *, source_image, mask="", model="", size="", n=1, **opts) -> list[ImageResult]
```

Signatures are copied verbatim from the ABC at
`src/personalclaw/image_gen/provider.py:50-118`. `download_model`/`delete_model`
are inherited no-ops (`:109-115`) — correct for a hosted provider.

**The `name` setter is mandatory, not stylistic.** `ModelTypeHandler.create`
does `if not hasattr(provider, "name"): provider.name = …`
(`providers/registry.py:500-501`) — a bare read-only `@property` makes `hasattr`
True so no assignment is attempted; but `ExtensionInstance` display wiring and
the scanner keying both want a settable name. google-models declares the setter
on all three adapters (`google-models/provider.py:191-197`, `:373-379`,
`:558-564`); we copy that.

### `generate` → `POST {base}/images`

Single request, no polling (the endpoint is synchronous). Body assembled from
what the caller gives us **and what the live model descriptor says is legal**:

```
POST {base}/images
Authorization: Bearer <key>
Content-Type: application/json
X-OpenRouter-Title: PersonalClaw

{"model": <model_id>, "prompt": <prompt>, "n": <clamped>, "output_format": "png",
 ["resolution": …] | ["aspect_ratio": …] | ["size": …] }
```

Rules the implementation enforces:

1. **`size` vs `resolution`/`aspect_ratio` are mutually exclusive** — sending an
   explicit `size` alongside either is a 400 upstream. The ABC hands us a single
   opaque `size: str` (`image_gen/provider.py:76`). Mapping, in a
   `_normalize_size(size, descriptor)` helper modeled on
   `fal-image/provider.py:140-152` (`_normalize_image_size`):
   - `"1024x1024"`-shaped (regex `^\d{2,5}[xX*]\d{2,5}$`) → `{"size": s}`, and
     **nothing else** dimension-related is sent.
   - a value in the model's `supported_parameters["resolution"]["values"]`
     (e.g. `"1K"`, `"2K"`, `"4K"`, `"512"`) → `{"resolution": s}`.
   - a value in `supported_parameters["aspect_ratio"]["values"]`
     (e.g. `"16:9"`) → `{"aspect_ratio": s}`.
   - empty / unrecognized → omit all three, let the model default.
2. **`n` clamps to the model's live cap, not the schema's.** Verified from
   `GET /api/v1/images/models`: `google/gemini-3-pro-image` reports
   `n: {"type":"range","min":1,"max":1}` while `bytedance-seed/seedream-4.5`
   reports `max: 10`. The schema maximum is 10; the per-model cap is what's
   enforced. Absent `n` key ⇒ the parameter is unsupported ⇒ omit it entirely.
3. **`output_format: "png"`** — one of `png|jpeg|webp|svg`; only sent when the
   model's descriptor advertises `output_format`. PNG is the safe default and
   matches `ImageResult.mime`'s default (`image_gen/provider.py:44`).

**Response decode + where the bytes land.** Response is
`{created, data:[{b64_json, media_type?}], usage:{…}}` — **base64 only, never a
hosted URL**. The provider returns `ImageResult(b64=…, mime=media_type or "image/png")`
and **writes nothing to disk itself**. Verified the persistence path:

- `ImageResult` docstring (`image_gen/provider.py:33-47`) states the capability
  layer materializes to `local_path`.
- `_materialize_image` (`src/personalclaw/mcp_artifacts.py:296-328`) decodes
  `b64` first (`:307-312`) — no network hop for us, unlike fal-image whose
  URL results take the `fetch` branch (`:313-321`).
- The bytes are then persisted by
  `prov.create_binary(name=…, data=…, mime=…, kind="image", source="chat", actor="agent")`
  at `mcp_artifacts.py:558-566`, i.e. **the native artifact store**, rooted at
  `config_dir() / "artifacts"` (`src/personalclaw/artifacts/native.py:60`), one
  directory per slug with versioned binary snapshots
  (`native.py:435-480`). Delivery is `/api/artifacts/<slug>/raw?version=N`
  (`mcp_artifacts.py:573`).

So the reused helper is **core's `_materialize_image` + `create_binary`** — the
app must NOT invent its own file naming. google-models does exactly this: its
`_generate_via_content` returns `ImageResult(b64=…, mime=…)` and never touches
the filesystem (`google-models/provider.py:337-349`). fal-image returns `url=…`
(`fal-image/provider.py:314-327`) and lets `_materialize_image`'s fetch branch
pull the bytes. We follow google-models (b64), which is strictly better — no
expiring URL, no second egress hop.

### `edit` → `input_references`

OpenRouter's image-to-image is the **top-level `input_references`** array, entries
shaped `{"type":"image_url","image_url":{"url":<data-uri-or-url>}}` (max 16 by
schema; per-model cap from the descriptor — verified `microsoft/mai-image-2.5-pro`
caps at 1, `x-ai/grok-imagine-image-quality` at 3, `google/gemini-3-pro-image` at
14). So `edit` is genuinely implementable, not a stub:

1. Read `source_image` (a local path per the ABC, `image_gen/provider.py:100-105`)
   and base64 it into a `data:<mime>;base64,…` URI — the exact technique
   `fal-image/provider.py:296-306` uses.
2. Send it as `input_references: [{"type":"image_url","image_url":{"url": data_uri}}]`
   alongside `prompt` and `model`.
3. `mask` is **not supported** by OpenRouter's `/images`. When a non-empty `mask`
   is passed, raise `ImageGenError("OpenRouter's image API has no mask/inpainting
   parameter; omit the mask or use a mask-capable provider.")` — a typed, honest
   refusal, not a silent drop.
4. If the resolved model's descriptor has **no** `input_references` key, or its
   `max` is `0`, raise `ImageGenError(f"Model {model_id!r} does not accept input
   images (no input_references support).")` before spending a request.

### `list_models` → `GET {base}/images/models`

Verified live (HTTP 200): `{"data": [ … 38 entries … ]}`, each entry
`{id, name, description, created, architecture:{input_modalities, output_modalities},
supported_parameters:{…}, supports_streaming, endpoints}`. Descriptor grammar
confirmed by taking the union across all 38: keys are
`aspect_ratio`(enum), `background`(enum), `input_references`(range),
`n`(range), `output_compression`(range), `output_format`(enum),
`quality`(enum), `resolution`(enum), `seed`(boolean) — matching the
`{type:"enum",values:[…]}` / `{type:"range",min,max}` / `{type:"boolean"}`
grammar exactly. **An absent key means unsupported.**

Mapping onto `ImageGenModel` (`image_gen/provider.py:16-30`):

| `ImageGenModel` field | Source |
|---|---|
| `name` | entry `id` (e.g. `"google/gemini-3-pro-image"`) |
| `description` | entry `description` (truncate), else `name` |
| `sizes` | `supported_parameters["resolution"]["values"]` (e.g. `["1K","2K","4K"]`) concatenated with `supported_parameters["aspect_ratio"]["values"]` (e.g. `["1:1","16:9",…]`), both `[]`-safe. The ABC calls `sizes` "supported output sizes (e.g. `1024x1024`)" — OpenRouter doesn't express pixel dims here, so we surface the tokens the API actually accepts, which is what the picker must offer for a request to succeed. `list_models_for_provider` (`image_gen/registry.py:191-208`) passes `sizes` straight to the API, so the UI shows exactly these. |
| `supports_edit` | `True` iff `supported_parameters` has an `input_references` entry whose `max >= 1` |
| `downloaded` | `True` (hosted) |
| `active` | `m.id == active_model`, where `active_model` comes from `active_image_gen()` guarded by `resolved[0].name == self._name` — the google-models idiom (`google-models/provider.py:209-214`), with `self._name` (not a literal `"google"`), because our name is the config-entry name |

**TTL-cached, per-key**, mirroring `_discover_models`
(`google-models/provider.py:118-150`): `dict[str, tuple[float, list]]` keyed by
api_key, 300 s, `time.monotonic()`, returns the stale entry on failure and `[]`
if there was never one — so a transient 5xx degrades to the last good list, and
no-key degrades to an empty picker. Separate caches for `/images/models` and
`/videos/models`.

### HTTP transport + error mapping

All image/video HTTP goes through `personalclaw.sdk.net.fetch` with
`policy=egress_policy_for(CONNECTOR)` — the guarded chokepoint fal-image uses
(`fal-image/provider.py:185, 193-201`), preferred over google-models' raw
`aiohttp` (`google-models/provider.py:128, 265`). Rationale: it is the same
pattern core's own discovery uses (`llm/catalog.py:327, 342-344`) and gives us
host classification, redirect re-check, and SEL audit for free.

**One measured constraint:** `CONNECTOR` is
`EgressPolicy(name="connector", max_bytes=10_000_000, timeout_s=20.0)`
(`src/personalclaw/net/policy.py:55`), and `_read_capped`
(`net/client.py:207-220`) **silently truncates** at `max_bytes`, setting
`truncated=True`. 20 s is fine for a JSON response but too short for a 120 s
image generation, and 10 MB will truncate a 4K PNG or any real MP4. So:

- **JSON calls** (discovery, submit, poll) → `egress_policy_for(CONNECTOR)` as-is.
- **The image `POST /images` call** (long, and returns a large base64 body) →
  `egress_policy_for(CONNECTOR).with_overrides(timeout_s=_IMAGE_TIMEOUT_S, max_bytes=64_000_000)`.
  `with_overrides` is a documented public method (`net/policy.py:41-45`).
- **The video content download** → same override with `max_bytes=256_000_000`,
  and the code **asserts `not resp.truncated`**, raising
  `VideoGenError("… truncated at the egress byte cap …")` rather than saving a
  corrupt MP4. This is a real bug class fal-image and google-models both dodge
  only because they hand core a URL and let `_materialize_video` fetch it — which
  itself uses bare `CONNECTOR` (`mcp_artifacts.py:597-602`) and would truncate at
  10 MB. Ours is the safer path (§6).

Error envelope is `{error:{code, message, metadata?}}` with HTTP status == `error.code`.
A shared `_error_detail(text)` helper mirrors `google-models/provider.py:693-697`
(`json.loads(text)["error"]["message"][:200]`, falling back to `text[:200]`).
Status-specific messages, all raised as `ImageGenError`/`VideoGenError`:

| Status | Surfaced message |
|---|---|
| 401/403 | "OpenRouter rejected the API key." **Do not** pattern-match the body — verified live that an unauthenticated `POST /api/v1/images` returns the misleading `{"error":{"message":"No cookie auth credentials found","code":401}}`; the plan keys off the **status code only**. |
| 402 | "OpenRouter reports insufficient credits — top up at openrouter.ai/credits." |
| 413 | "The input image is too large for OpenRouter's limit." |
| 429 | "OpenRouter rate-limited this request." + honor `Retry-After` (see below). |
| 502 | "The upstream provider failed. Image billing is all-or-nothing, so nothing partial was produced — and nothing was charged for a 502." |
| 524 / 529 | "OpenRouter timed out / is overloaded; retry shortly." |
| other non-2xx | `f"OpenRouter … failed (HTTP {status}): {_error_detail(text)}"` |

**`Retry-After` handling.** On 429, read the header from `resp.headers` (a plain
dict on `FetchResponse`, `net/client.py:42`), parse it as delta-seconds, clamp to
`[1, 30]`, `await asyncio.sleep(...)`, and retry **once**. A second 429 raises.
Bounded so the retry can never outlive the enclosing timeout.

---

## 6. Video provider design

```python
_VIDEO_TIMEOUT_S = 600.0          # see the timeout note below
_VIDEO_POLL_INTERVAL_S = 5.0      # matches google-models/provider.py:59
_VIDEO_TERMINAL_OK = ("completed",)
_VIDEO_TERMINAL_BAD = ("failed", "cancelled", "expired")
_VIDEO_PENDING = ("pending", "in_progress")

class OpenRouterVideoProvider(VideoGenProvider):
    def __init__(self, *, api_key: str = "", endpoint: str = "", name: str = "openrouter") -> None: ...
    @property
    def name(self) -> str: ...
    @name.setter
    def name(self, value: str) -> None: ...
    @property
    def display_name(self) -> str:    # "OpenRouter (video)"
    async def is_available(self) -> bool
    async def list_models(self) -> list[VideoGenModel]
    async def generate(self, prompt, *, model="", duration_seconds=5.0, aspect_ratio="", **opts) -> list[VideoResult]
    # private:
    async def _submit(...) -> str                    # -> job id
    async def _poll(job_id) -> dict                  # -> terminal job doc
    async def _download(job_id, index=0) -> tuple[bytes, str]
```

Signatures verbatim from `src/personalclaw/video_gen/provider.py:46-86`.

### The submit→poll loop is owned INSIDE `generate`

The ABC is explicit (`video_gen/provider.py:77-83`): *"A submit->poll provider
MUST own its poll loop inside this coroutine, bounded by a per-provider
timeout."* So `generate` = `_submit` → `_poll` → `_download` → return
`[VideoResult(local_path=…)]`, exactly the shape
`google-models/provider.py:436-437` uses (`op_name = await self._submit(...)` then
`return await self._poll_and_fetch(...)`).

**Timeout value, and why it differs from the apps we're copying.** The cited
precedents are `300.0` in three places — `google-models/provider.py:58`
(`_VIDEO_TIMEOUT_S = 300.0`), `fal-image/provider.py:129`
(`_VIDEO_POLL_TIMEOUT_S = 300.0  # video generation takes longer`), and
`bedrock-models/provider.py:1453` (`_VIDEO_POLL_TIMEOUT = 300`). This plan uses
**600.0** and states the reason rather than silently deviating: verified from
`GET /api/v1/videos/models` that OpenRouter serves clips up to **20 s**
(`openai/sora-2-pro`: `supported_durations [4, 8, 12, 16, 20]`) and **15 s** at
**4K** (`bytedance/seedance-2.0`: `supported_resolutions [480p, 720p, 1080p, 4K]`,
durations to 15) — materially longer than Veo's 8 s or Nova Reel's fixed 6 s, so
300 s would time out on legitimate jobs. The loop structure (`while elapsed <
TIMEOUT: … await asyncio.sleep(INTERVAL); elapsed += INTERVAL` with a `while/else`
raising on exhaustion) is copied from `google-models/provider.py:483-497`. If the
owner prefers strict parity at 300 s, that's a one-constant change (Q7).

### Submit

```
POST {base}/videos  ->  HTTP 202  {id, polling_url, status:"pending"}
{"model": <id>, "prompt": <prompt?>, "duration": <int>, "generate_audio": <bool>,
 ["resolution": …] | ["aspect_ratio": …] | ["size": …],
 ["frame_images": [{"frame_type":"first_frame", "image_url":{"url":…}}]] }
```

- `model` is the only required field. **`prompt` is NOT required** — image-only
  generation is legal. So an empty prompt is only rejected when the request also
  carries no `frame_images`/`input_references`; note core's own `_video_generate`
  wrapper already refuses an empty prompt earlier (`mcp_artifacts.py:632-635`), so
  in practice the prompt-less path is reachable only via a future UI.
- `duration` is snapped to the **nearest value in the model's
  `supported_durations`** array (never sent raw): verified these are explicit int
  arrays per model — `google/veo-3.1-fast: [4,6,8]`, `kwaivgi/kling-video-o1: [5,10]`,
  `minimax/hailuo-2.3: [6,10]`, `x-ai/grok-imagine-video-1.5: [1..15]`. The ABC
  gives us a float `duration_seconds` (`video_gen/provider.py:73`); we `round()`
  then pick `min(supported, key=lambda d: abs(d - want))`.
- `aspect_ratio` is sent only if present in `supported_aspect_ratios`. Verified
  this is `null` for some models (`x-ai/grok-imagine-video-1.5`,
  `openai/sora-2-pro` has ratios but `supported_frame_images: null`) — a `null`
  array means "don't send it", distinct from an empty list.
- **`generate_audio` is ALWAYS sent explicitly** (docs and the OpenAPI schema
  disagree on the default), but only for models whose descriptor has
  `generate_audio` non-null. Verified the field is `true` for 11 models, `false`
  for `minimax/hailuo-2.3`, and `null` for 4. Value: `bool(opts.get("generate_audio", True))`
  when the model supports it, omitted when `null`.
- `seed` sent only when `descriptor["seed"] is True` (verified `true`/`false`/`null`
  all occur).
- **`frame_images` wins over `input_references`** when both are supplied, per the
  documented precedence — so the implementation sends **only one of them**, never
  both, and prefers `frame_images` when the caller provides a first/last frame.
  Each `frame_images` entry must carry `frame_type` ∈ `{first_frame, last_frame}`,
  validated against the model's `supported_frame_images` array (verified
  `["first_frame"]` for 6 models, `["first_frame","last_frame"]` for 10, `null`
  for `openai/sora-2-pro`).
- Accept **HTTP 202** as success (not just 200) and read `id`. `polling_url` is
  read but the code polls the canonical `GET {base}/videos/{id}` it constructs
  itself — fal-image documents the exact bug class where a vendor's returned
  status URL was malformed (`fal-image/provider.py:176-184`).

### Poll — all SIX statuses handled

`GET {base}/videos/{jobId}` → `status` ∈
`pending | in_progress | completed | failed | cancelled | expired`. The docs' table
lists only four; the code handles all six explicitly (lowercased compare):

| Status | Action |
|---|---|
| `pending`, `in_progress` | sleep `_VIDEO_POLL_INTERVAL_S`, continue |
| `completed` | break; read `unsigned_urls` + `usage.{cost,is_byok}` |
| `failed` | `raise VideoGenError(f"OpenRouter video job failed: {detail}")` |
| `cancelled` | `raise VideoGenError("OpenRouter video job was cancelled.")` |
| `expired` | `raise VideoGenError("OpenRouter video job expired before its output could be downloaded.")` |
| anything else / missing | treat as pending but log once at debug (forward-compat: a new status must not crash), and let the outer timeout end it |

Loop exhaustion → `raise VideoGenError(f"OpenRouter video generation timed out after {_VIDEO_TIMEOUT_S:.0f}s.")`.
A transient non-200 on a poll is swallowed and retried (google-models does the
same, `google-models/provider.py:492-493`) so one 5xx doesn't kill a paid job.

### Download

`GET {base}/videos/{jobId}/content?index=0` → raw `video/mp4`.

**Decision: the provider downloads the bytes itself and returns `local_path`,
not `url`.** Three verified reasons:

1. `_materialize_video` (`mcp_artifacts.py:585-610`) fetches a returned `url`
   with bare `CONNECTOR` — `max_bytes=10_000_000` — and `_read_capped` truncates
   silently. A 1080p 10 s MP4 exceeds that, so returning a URL would hand core a
   silently-corrupt video. Returning `local_path` takes the `Path(local).read_bytes()`
   branch (`:604-609`) instead, with no cap.
2. `unsigned_urls` auth is ambiguous (below) — downloading inside the provider,
   where we still hold the key, sidesteps the question entirely for the happy path.
3. `bedrock-models` already establishes the `local_path` precedent:
   `return [VideoResult(local_path=local_path, mime="video/mp4", duration_s=6.0)]`
   at `bedrock-models/provider.py:1587`, writing to
   `os.path.join(tempfile.gettempdir(), f"bedrock_video_{ts}.mp4")` (`:1564`).

So: write to `tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)`, return
`VideoResult(local_path=path, mime=<Content-Type or "video/mp4">, duration_s=<snapped duration>)`.
Core then reads and persists via `create_binary(kind="video")`
(`mcp_artifacts.py:728-732`). The temp file is intentionally **not** unlinked by
the provider — core reads it after `generate` returns; OS temp cleanup handles it.
(Same lifetime contract bedrock relies on.)

### `unsigned_urls` — how the auth ambiguity gets resolved empirically

The docs contradict themselves on whether `unsigned_urls` require the
`Authorization` header. The plan does **not** guess. Resolution procedure, run
during validation (§9, step V6) once the owner supplies a key:

1. Generate one short clip; capture the terminal poll JSON (`unsigned_urls`,
   `usage`).
2. `curl -sI "<unsigned_url>"` with **no** auth header → record status.
3. `curl -sI "<unsigned_url>" -H "Authorization: Bearer $KEY"` → record status.
4. `curl -sI "{base}/videos/{id}/content?index=0" -H "Authorization: …"` → record
   status + `Content-Length` (also answers "is this bigger than 10 MB?").
5. Also probe the validity window: re-run step 2 after ~10 min and ~1 h to bound
   the TTL (open question Q3).

Because v1 downloads via `/content` with the key attached, **the answer changes
nothing in shipped code** — it only tells us whether a future "hand the user a
shareable link" feature is possible. The findings get recorded in the app README
and this file's execution log. This is deliberately the *observation*, not a
behavior branch: no dual path.

### `list_models` → `GET {base}/videos/models`

Verified live (HTTP 200): `{"data":[ … 17 entries … ]}` with explicit arrays —
`supported_resolutions`, `supported_aspect_ratios`, `supported_sizes`,
`supported_durations`, `supported_frame_images`, `generate_audio`, `seed`,
`pricing_skus`, `allowed_passthrough_parameters` (union of keys across all 17
confirmed). Any of these can be `null`.

Mapping onto `VideoGenModel` (`video_gen/provider.py:14-29`):

| Field | Source |
|---|---|
| `name` | entry `id` (e.g. `"google/veo-3.1-fast"`) |
| `description` | entry `description` (truncate), else `name` |
| `aspect_ratios` | `supported_aspect_ratios or []` — **never a hardcoded `["16:9","9:16"]`**, which is what `google-models/provider.py:406` and `fal-image/provider.py:89` do. OpenRouter tells us the truth per model (`bytedance/seedance-2.0` supports 7 ratios; `minimax/hailuo-2.3` only `["16:9"]`). |
| `max_duration_s` | `max(supported_durations)` when non-empty, else the dataclass default `10`. Verified range across models: 10 → 20. |
| `downloaded` | `True` (hosted) |
| `active` | `m.id == active_model` from `active_video_gen()`, guarded on `resolved[0].name == self._name` (`google-models/provider.py:392-395` idiom) |

---

## 7. Discovery + registration wiring

### The entries filter

```python
def _openrouter_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The config.json provider entries this app owns (type ``openrouter``)."""
    out = []
    for e in entries:
        ptype = str(e.get("type", ""))
        original = str((e.get("options") or {}).get("_original_type", ""))
        if ptype == "openrouter" or original == "openrouter":
            out.append(e)
    return out


def _entry_key(e: dict[str, Any]) -> str:
    return str((e.get("options") or {}).get("api_key", "") or "")


def _entry_endpoint(e: dict[str, Any]) -> str:
    return str((e.get("options") or {}).get("endpoint", "") or "")
```

Modeled on `google-models/provider.py:722-733` and `bedrock-models/provider.py:1739-1757`,
both of which carry the same `_original_type` tolerance. Why keep it even though
`canonical_provider_type` is an identity map today (`llm/registry.py:329-336`, so
`ptype == registry_type` and `_original_type` is never stamped for us —
`llm/registry.py:398-399`, `dashboard/handlers/providers.py:838-840`): the map is
documented as "the single hook … so a future alias only needs adding", so a future
alias must not silently orphan our media adapters.

### Scanners, keyed by CONFIG ENTRY NAME

```python
def _scan_image(entries):
    return [
        OpenRouterImageProvider(
            api_key=_entry_key(e), endpoint=_entry_endpoint(e), name=str(e["name"]),
        )
        for e in _openrouter_entries(entries)
    ]


def _scan_video(entries):
    return [
        OpenRouterVideoProvider(
            api_key=_entry_key(e), endpoint=_entry_endpoint(e), name=str(e["name"]),
        )
        for e in _openrouter_entries(entries)
    ]


try:
    from personalclaw.sdk.model import register_scanner as _reg_scanner

    _reg_scanner("image_gen", _scan_image)
    _reg_scanner("video_gen", _scan_video)
except Exception:  # noqa: BLE001 — older core without the extension point
    logger.debug("media_scanners extension point unavailable", exc_info=True)
```

`name=str(e["name"])` — **the config entry's name, not the literal `"openrouter"`**.
This is what makes an `<instance-name>:<model-id>` binding resolve to the same
account that backs that instance's chat. Verified this is the documented intent in
both precedents: `google-models/provider.py:719` ("keyed by the entry's name so
`<name>:model` refs resolve to that entry's key") and
`bedrock-models/provider.py:1759-1761` ("Key each adapter by the config entry name
(not the generic \"bedrock\")"). It is also what the registries key on:
`image_gen/registry.py:26-27` (`_providers[provider.name] = provider`) and
`_ensure_scanned` (`:60-81`), which registers by `getattr(p, "name")` and removes
stale scanner providers whose config entry disappeared.

`register_scanner` is idempotent per function object (`providers/media_scanners.py:32-41`),
so a re-imported module in tests doesn't stack duplicates. `scan()` is re-run on
every registry resolution (`image_gen/registry.py:48-52` deliberately keeps the
scanner sweep **outside** the `_auto_registered` latch to survive boot-order
races), so adapters appear as soon as a config entry exists.

Both symbols come from `personalclaw.sdk.model` — verified re-exported at
`src/personalclaw/sdk/model.py:68` (`from personalclaw.providers.media_scanners import register_scanner`)
and listed in `__all__` at `:106`. That keeps the bundle inside the SDK boundary
(`tests/test_apps_import_boundary.py:29-56`, which allows only `personalclaw.sdk[.*]`
and exempts `test_*.py`).

### Landmine 1 — prune safety: HANDLED, no core change

`_dynamic_media_provider_names()` (`use_cases.py:132-183`) is the function whose
output decides whether an `active_models.json` ref survives
`_prune_removed_providers` (`:213-233`). Our provider names are discoverable
**three** independent ways:

1. **Config-name identity.** Our adapters are named after the config entry
   (`name=str(e["name"])`), and `_known_provider_names()` (`:186-210`) already
   unions in *every* `config.json` `providers[].name`. So a binding
   `MyOpenRouter:google/veo-3.1` survives purely because `MyOpenRouter` is a
   configured provider name — the same reason a `google:…` image binding survives.
   This is the primary, structural guarantee.
2. **Live registry sweep.** `_dynamic_media_provider_names` calls
   `ig._ensure_registered()` then `ig.list_providers()` (`:144-147`) and the same
   for `video_gen` (`:150-153`); `_ensure_registered` runs `_ensure_scanned` →
   `scan("image_gen")` → our `_scan_image`. So our names appear there too.
3. **Installed-bundle sweep.** `:161-169` walks
   `get_provider_registry().list_by_type("model")` and, for any ext whose
   `capabilities` contain `image_gen`/`video_gen`, adds
   `getattr(inst, "name", "") or ext.name`. Our manifest declares both, so
   `openrouter-models` is covered even while disabled.

The guarding test is `PersonalClaw/tests/test_model_registry_prune.py`. Note the
irony worth recording: that file's fixture at `:28-43` already **uses the literal
string `"openrouter"`** as the name of an `openai_compatible` instance
(`providers=[{"name": "openrouter", "type": "openai_compatible"}]`,
asserting `loaded["chat"] == ["openrouter:gpt-5"]`). That is a *fixture name*, not
a provider type — it does not conflict with registering a real `openrouter` type,
and no core test change is needed. (Verified this and two comments —
`llm/stream_tags.py:10`, `providers/provider_bridge.py:953` — are the **only**
`openrouter` hits in the whole core tree; `grep -rni openrouter` over the apps repo
returns nothing.)

### Landmine 2 — `OPENAI_FAMILY_TYPES`: DO NOT ADD `"openrouter"`

**Decision: `"openrouter"` is deliberately NOT added to `OPENAI_FAMILY_TYPES`
(`use_cases.py:423-432`, currently `openai, openai_compatible, together, groq,
deepseek, mistral, azure_openai, google`).**

Why it would be actively wrong: `_register_remote_providers`
(`image_gen/registry.py:84-104`) iterates `openai_family_providers()` and
registers a core `OpenAIImageProvider` for each, **keyed by the config name**.
Since our scanner uses that *same* key, and `_ensure_scanned` documents scanner
providers as "AUTHORITATIVE … OVERWRITES any same-named OpenAI-family adapter"
(`image_gen/registry.py:62-66`), the two would race on the same dict key. Worse,
the core adapter speaks **OpenAI-Images** (`/images/generations` with `size` +
`image[]`), which OpenRouter's `/images` is verifiably not — so any window in
which the core adapter won would produce 400s. The same `OPENAI_FAMILY_TYPES`
membership also wires core's free STT/TTS adapters (`:420-422` docstring), which
we have not verified against OpenRouter at all.

Cost of the decision: the app must (and does) contribute its own `image_gen`
adapter via the scanner — which it wants to anyway, since only the app knows the
`/images` shape. No functionality is lost. Recorded here so a future reader
doesn't "fix" the omission.

---

## 8. Test plan

Two files, both in `openrouter-models/`, both exempt from the SDK-only lint
(`tests/test_apps_import_boundary.py:38-39` skips `test_*.py`) and both runnable
with **no vendor SDK** (CI's `tests` job installs core + pytest + the manifest's
declared deps only).

### `openrouter-models/test_provider.py`

Fixtures:
- `_stub_openai` (autouse) — a `types.ModuleType("openai")` exposing a trivial
  `AsyncOpenAI`, `monkeypatch.setitem(sys.modules, "openai", fake)`. Verbatim
  from `google-models/test_provider.py:16-28`.
- `_fake_fetch(payloads)` — a factory returning an async `fetch` stand-in that
  serves a queued list of `(status, json_or_bytes, headers)` and records the
  `(method, url, body)` of every call, so tests can assert the exact request
  shape. Patched over `personalclaw.sdk.net.fetch` (and the two aliases
  `personalclaw.net.fetch`, `personalclaw.net.client.fetch`, matching the
  belt-and-braces triple in `google-models/test_catalog.py:35-38`).
- `_no_env` — `monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)`.

Chat / registration properties:

| Test function | Property locked |
|---|---|
| `test_type_and_catalog_registered` | `get_default_registry().capability_of("openrouter").type == "openrouter"` and `catalog_of("openrouter") is not None` — the import-time side effect fired. |
| `test_spec_defaults` | `SPEC.type == "openrouter"`, `default_base_url == "https://openrouter.ai/api/v1"`, `api_key_env == "OPENROUTER_API_KEY"`, `default_model == ""`, `fallback_models == ()`, and `{CHAT, CODE_TOOLS, STREAMING, VISION} <= SPEC.capabilities`. |
| `test_create_provider_uses_default_endpoint` | `create_provider({})._base_url == "https://openrouter.ai/api/v1"` and `._model == ""`. |
| `test_create_provider_config_overrides` | `{"api_key","model","endpoint"}` override base_url + model. |
| `test_create_provider_returns_single_chat_provider` | the factory returns ONE provider (not a list) and it is **not** an `ImageGenProvider`/`VideoGenProvider` — locks the google-models shape against an accidental fal-style list. |
| `test_registry_build` | a registered `ProviderEntry(type="openrouter", options={"api_key": …})` builds with the right base_url. |

Manifest-vs-code parity:

| Test function | Property locked |
|---|---|
| `test_manifest_capabilities_are_known_use_cases` | every string in `app.json`'s `provider.capabilities` is in `CAPABILITIES + CHAT_SUBCATEGORIES` (imported from core) — catches a typo that would silently never register. |
| `test_manifest_provider_type_matches_spec` | `app.json`'s `provider.providerType == SPEC.type` — the mismatch that makes "Add instance" create an unbuildable entry. |
| `test_manifest_provider_type_is_model` | `provider.type == "model"` ∈ `PROVIDER_TYPES`. |
| `test_manifest_declares_network_permission_only` | `permissions == {"network": True}` — the over-declaration guard the app bar asks for. |

Scanner / registration wiring:

| Test function | Property locked |
|---|---|
| `test_openrouter_entries_matches_type_and_original_type` | `_openrouter_entries` selects `{"type":"openrouter"}` **and** `{"type":"openai","options":{"_original_type":"openrouter"}}`, and rejects a `google` entry. |
| `test_scanners_key_adapters_by_entry_name` | `_scan_image`/`_scan_video` over `[{"name":"acct-a",…},{"name":"acct-b",…}]` yield adapters whose `.name` is `"acct-a"`/`"acct-b"` — **not** `"openrouter"`. This is landmine 1's structural guarantee. |
| `test_scanners_thread_per_entry_api_key` | adapter `_api_key` comes from that entry's `options.api_key`, so two accounts don't cross-talk. |
| `test_adapter_name_is_settable` | `p.name = "x"` works on both adapters (the `@name.setter` requirement from `providers/registry.py:500-501`). |
| `test_scanners_registered_for_both_capabilities` | after import, `media_scanners._scanners` has our callables under `"image_gen"` and `"video_gen"`. |
| `test_openrouter_not_in_openai_family_types` | `"openrouter" not in personalclaw.providers.use_cases.OPENAI_FAMILY_TYPES` — landmine 2, asserted as a standing invariant so a later "helpful" core PR trips this test. |

Image provider:

| Test function | Property locked |
|---|---|
| `test_image_is_available_requires_key` | `False` with no key/env; `True` with either. |
| `test_image_list_models_maps_supported_parameters` | a stubbed `/images/models` payload (real shape, taken from the live response) yields `ImageGenModel(name="google/gemini-3-pro-image", sizes=["1K","2K","4K","1:1",…], supports_edit=True)`; a model with no `input_references` key → `supports_edit is False`. |
| `test_image_list_models_empty_without_key` | no key → `[]`, no network call attempted (the honest-empty-picker rule). |
| `test_image_list_models_uses_ttl_cache` | two calls → one `fetch`. |
| `test_image_list_models_returns_stale_on_error` | primed cache + a 500 → the stale list, not `[]`. |
| `test_image_generate_posts_to_images_endpoint` | request URL is exactly `{base}/images` — **never** `/images/generations`. |
| `test_image_generate_decodes_b64_to_image_result` | `{"data":[{"b64_json": …, "media_type":"image/png"}]}` → `ImageResult(b64=…, mime="image/png")` with `local_path == ""` (core materializes). |
| `test_image_generate_never_sends_size_with_resolution` | `size="1K"` → body has `resolution` and **no** `size`; `size="1024x1024"` → body has `size` and **no** `resolution`/`aspect_ratio`. The documented 400. |
| `test_image_generate_maps_aspect_ratio_token` | `size="16:9"` → `aspect_ratio` key. |
| `test_image_generate_clamps_n_to_model_cap` | `n=5` against a `max:1` descriptor → body `n` is 1 (or absent), not 5. |
| `test_image_generate_omits_unsupported_params` | a descriptor with no `output_format`/`seed` → those keys absent from the body. |
| `test_image_generate_sends_bearer_and_title_headers` | `Authorization: Bearer …` + `X-OpenRouter-Title: PersonalClaw`; **no** `X-Title`. |
| `test_image_generate_raises_without_key` | `ImageGenError` mentioning `OPENROUTER_API_KEY`, with no network call. |
| `test_image_generate_error_mapping_by_status` | parametrized over `401, 402, 413, 429, 502, 524, 529` → `ImageGenError` whose message matches the §5 table. |
| `test_image_generate_ignores_cookie_auth_message` | a 401 whose body is `"No cookie auth credentials found"` still yields the API-key message — locks "never pattern-match that string". |
| `test_image_generate_honors_retry_after_once` | 429 with `Retry-After: 2` → sleeps (patched), retries once, succeeds; a second 429 raises. |
| `test_image_generate_raises_when_no_data` | `{"data":[]}` → `ImageGenError`, never an empty success. |
| `test_image_edit_sends_input_references_data_uri` | `edit(source_image=<tmp png>)` → body `input_references[0]["image_url"]["url"]` starts `data:image/png;base64,`, and `type == "image_url"`. |
| `test_image_edit_rejects_mask` | non-empty `mask` → `ImageGenError` naming mask/inpainting; no request sent. |
| `test_image_edit_rejects_model_without_input_references` | descriptor lacking `input_references` → `ImageGenError`; no request sent (no spend). |
| `test_image_uses_guarded_fetch_not_raw_aiohttp` | the module contains no `aiohttp.ClientSession` construction (AST/source scan) — locks the `sdk.net.fetch` chokepoint. |

Video provider:

| Test function | Property locked |
|---|---|
| `test_video_is_available_requires_key` | mirrors the image test. |
| `test_video_list_models_maps_explicit_arrays` | live-shaped `/videos/models` entry → `VideoGenModel(name="google/veo-3.1-fast", aspect_ratios=["16:9","9:16"], max_duration_s=8)`. |
| `test_video_list_models_tolerates_null_arrays` | `supported_aspect_ratios: None` → `aspect_ratios == []` (not `TypeError`), `supported_durations: None` → `max_duration_s == 10` (the dataclass default). |
| `test_video_list_models_does_not_hardcode_ratios` | a model advertising 7 ratios surfaces all 7 — no `["16:9","9:16"]` constant. |
| `test_video_generate_accepts_202_and_polls_to_completed` | `202 {id,status:pending}` → `in_progress` → `completed` → one `/content?index=0` GET; asserts the poll URL is `{base}/videos/{id}`. |
| `test_video_generate_sleeps_between_polls` | `asyncio.sleep` patched + asserted called with `_VIDEO_POLL_INTERVAL_S` — no busy-loop. |
| `test_video_generate_returns_local_path_not_url` | `VideoResult.local_path` is a real readable file, `url == ""` — locks the §6 byte-cap decision. |
| `test_video_generate_raises_on_truncated_download` | a `FetchResponse(truncated=True)` → `VideoGenError` mentioning truncation, and **no** file returned. |
| `test_video_generate_handles_failed_status` / `..._cancelled_status` / `..._expired_status` | each terminal-bad status raises `VideoGenError` with its own message — all three, since the docs list only four statuses total. |
| `test_video_generate_treats_unknown_status_as_pending` | a hypothetical `"queued"` doesn't crash; the loop continues and the outer timeout governs. |
| `test_video_generate_times_out` | a never-completing poll → `VideoGenError` naming the timeout, bounded by a monkeypatched `_VIDEO_TIMEOUT_S`. |
| `test_video_generate_snaps_duration_to_supported` | `duration_seconds=7.0` against `[4,6,8]` → body `duration == 6` (nearest); `duration_seconds=5.0` against `[5,10]` → `5`. |
| `test_video_generate_always_sends_generate_audio` | body contains `generate_audio` when the descriptor's value is non-null (the docs/OpenAPI disagreement), and **omits** it when the descriptor is `null`. |
| `test_video_generate_omits_unsupported_aspect_ratio` | a ratio absent from `supported_aspect_ratios` isn't sent. |
| `test_video_generate_prefers_frame_images_over_input_references` | both supplied → body has `frame_images` and **no** `input_references`. |
| `test_video_generate_frame_images_require_frame_type` | each entry carries `frame_type` ∈ `{first_frame,last_frame}`, validated against `supported_frame_images`; an unsupported `last_frame` raises before the request. |
| `test_video_generate_allows_empty_prompt_with_frame_image` | prompt-less + `frame_images` submits (prompt is not required upstream); prompt-less **and** reference-less raises. |
| `test_video_generate_error_mapping_by_status` | parametrized like the image test. |
| `test_video_generate_raises_without_key` | `VideoGenError` naming `OPENROUTER_API_KEY`. |

### `openrouter-models/test_catalog.py`

Structure copied from `google-models/test_catalog.py`.

| Test function | Property locked |
|---|---|
| `test_catalog_is_plain_catalog` | `isinstance(cat, ModelCatalog)` and `not isinstance(cat, ModelManager)` — hosted API, no local model management. |
| `test_empty_list_when_endpoint_unreachable` | `fetch` → 500 ⇒ `list_models() == []`. **The de-hardcode contract**: no curated fallback, no fake ids. |
| `test_live_models_win_over_fallback` | `{"data":[{"id":"live-model-1"}]}` ⇒ `["live-model-1"]`. |
| `test_discovery_url_has_no_double_v1` | the fetched URL is `https://openrouter.ai/api/v1/models`, not `…/api/v1/v1/models` — the `llm/catalog.py:331-333` `/v1`-append behavior against a base that already ends in `/v1`. |
| `test_test_connection_needs_key` | no key ⇒ `ConnectionResult.ok is False`. |

### Local gate before any commit

```
cd /Users/golani/PersonalProjects/PersonalClaw/PersonalClawApps
python -m pytest openrouter-models -q
# manifest round-trip, exactly as CI's manifest-validate job does:
python -c "import json;from personalclaw.apps.manifest import AppManifest as M; \
  d=json.load(open('openrouter-models/app.json')); m=M.from_dict(d); \
  assert M.from_dict(m.to_dict()).to_dict()==m.to_dict(); print('manifest OK')"
# boundary lint, from the CORE repo with the apps dir symlinked/available:
cd ../PersonalClaw && python -m pytest tests/test_apps_import_boundary.py -q
```

---

## 9. Validation plan (as a user)

Prerequisite: the owner supplies an OpenRouter API key with credits. Until then,
only the **key-absent** path (V0) is runnable — and it must be run, because the
honest-empty-picker rule is the one thing we can prove without spending money.

Verified environment facts these steps depend on: the workspace apps clone is
named `PersonalClawApps`, so the gateway's `<workspace>/apps` auto-discovery
misses it and `PERSONALCLAW_FIRST_PARTY_APPS_DIR` must be set; the gateway runs
the **installed** copy under `$PERSONALCLAW_HOME/apps/<name>/`, so repo edits are
pushed with `POST /api/apps/{name}/update`; and backend `.py` changes never
hot-reload.

### V0 — key ABSENT: honest empty picker, no fake models

```bash
cd /Users/golani/PersonalProjects/PersonalClaw/PersonalClaw
source .venv/bin/activate
unset OPENROUTER_API_KEY
PERSONALCLAW_HOME="$PWD/.dev-home" \
PERSONALCLAW_FIRST_PARTY_APPS_DIR="$PWD/../PersonalClawApps" \
  make serve-fresh
```

Then, with the `PERSONALCLAW_READY:` URL+token from stdout (or
`PERSONALCLAW_HOME="$PWD/.dev-home" .venv/bin/personalclaw token --port 10000`):

1. App Store → **OpenRouter** appears (proves `PERSONALCLAW_FIRST_PARTY_APPS_DIR`
   took effect) → Install. The consent surface must list **network** and nothing
   else.
2. Settings → Providers → Add instance → the **OpenRouter** type is offered
   (proves `providerType` reached `api_provider_types_list`). Create an instance
   named `or-test` with `api_key` **left empty**.
3. Assert, via UI and curl:
   - `curl -s -H "Authorization: Bearer $TOK" localhost:10000/api/models?provider=or-test`
     → an **empty** model list. No `gpt-4`, no `claude-*`, no invented ids.
   - `curl -s -H "Authorization: Bearer $TOK" localhost:10000/api/image-gen/providers`
     → `or-test` present with `"available": false`.
   - the same for the video-gen providers route.
   - Settings → Models → the `image_gen` / `video_gen` pickers show `or-test`
     with **zero** models, and the connection test reports "No API key configured
     (set it or OPENROUTER_API_KEY)".
4. `tail -f .dev-home/gateway.log` — no traceback, no "wired wrong adapter"
   warning, and specifically no `OpenAIImageProvider` registered under `or-test`
   (landmine 2 held).
5. Delete the instance; confirm `or-test:*` refs vanish from
   `.dev-home/active_models.json` rather than ghosting (the `_ensure_scanned`
   stale-removal path, `image_gen/registry.py:77-81`).

### V1 — key present: instance + chat

```bash
# stop the foreground make serve with Ctrl-C first (backend .py changes never hot-reload)
export OPENROUTER_API_KEY='<owner-supplied>'
PERSONALCLAW_HOME="$PWD/.dev-home" \
PERSONALCLAW_FIRST_PARTY_APPS_DIR="$PWD/../PersonalClawApps" \
  make serve
```

1. Settings → Providers → edit `or-test` → paste the key into the (sensitive)
   `api_key` field → Save. Confirm the key is mirrored to
   `.dev-home/config.json` `providers[].options.api_key` (this is what the media
   scanners read) and **not** echoed in any API response.
2. Connection test → green with a model count in the hundreds.
3. Settings → Models → bind `chat` to `or-test:anthropic/claude-sonnet-4.5`
   (or whatever the picker offers). Open a chat, send "hello" — assert a streamed
   reply, and in the browser Network tab a single POST to the gateway (the
   upstream call is server-side).
4. Tool-calling: ask for something requiring a tool (e.g. "read
   `docs/plans/OPENROUTER-MODELS.md` and give me its headings") — proves
   `code_tools`.
5. Vision: attach a PNG and ask "what is in this image?" — proves the
   `image_url` content-part path end to end. Bind `image_modality` to a
   vision-capable OpenRouter model first.

### V2 — image generation

1. Settings → Models → `image_gen` → the picker lists OpenRouter image models
   with size tokens (e.g. `google/gemini-3-pro-image` with `1K/2K/4K` + the ratio
   list). Bind one.
2. In chat: "generate an image of a red bicycle on a beach".
3. Assert: the image renders **inline** in the reply; the markdown src is
   `/api/artifacts/<slug>/raw?version=1`; the artifact exists under
   `.dev-home/artifacts/<slug>/` with a real PNG body; gateway log shows one
   `POST https://openrouter.ai/api/v1/images` and **no** `/images/generations`.
4. Size handling: repeat with an explicit `2K` and with `1024x1024`. Both succeed
   and the log shows `resolution` for the first, `size` for the second — never
   both (the documented 400).
5. Edit: "make the bicycle blue" against the produced artifact. Assert the
   artifact version increments to 2, the request body carried
   `input_references[0].image_url.url` as a `data:` URI, and the new version
   renders.
6. Negative: bind a model whose descriptor has no `input_references` (e.g. one
   with `max: 0`) and attempt an edit → a clear `ImageGenError` in chat, **no**
   upstream request in the log (no spend).

### V3 — video generation

1. Settings → Models → `video_gen` → bind `or-test:google/veo-3.1-fast` (verified
   `supported_durations [4,6,8]`, `supported_resolutions [720p,1080p,4K]`,
   `generate_audio: true`).
2. In chat: "generate a 4-second video of waves on a beach".
3. Assert in `.dev-home/gateway.log`: one `POST /api/v1/videos` → **202**; a
   sequence of `GET /api/v1/videos/<id>` polls ~5 s apart showing
   `pending`→`in_progress`→`completed`; then one
   `GET /api/v1/videos/<id>/content?index=0`.
4. Assert the request body carried `duration: 4` (snapped to a supported value)
   and an explicit `generate_audio`.
5. Assert the artifact is a playable MP4 in the UI, of plausible size (a
   truncated file would be caught by the `truncated` guard and surface as an
   error instead — verify no truncation warning).
6. Timing: confirm the whole thing completed inside `_VIDEO_TIMEOUT_S` (600 s).
   If a 4 s clip takes anywhere near that, revisit the constant.

### V4 — two accounts don't cross-talk

Add a second instance `or-test-b` with a different key (or the same key, a
different `default_model`). Bind `image_gen` to `or-test-b:<model>`. Assert the
generated image is attributed to `or-test-b` in the log and that `or-test`'s
binding is untouched — the name-keying guarantee from §7.

### V5 — repo-edit push loop

After any `provider.py` change:
`curl -X POST -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  localhost:10000/api/apps/openrouter-models/update \
  -d '{"source":"/Users/golani/PersonalProjects/PersonalClaw/PersonalClawApps/openrouter-models","confirm":true}'`
then Ctrl-C `make serve` and restart (backend Python never hot-reloads). Editing
`.dev-home/apps/openrouter-models/` directly is a trap — the next update
overwrites it.

### V6 — resolve the `unsigned_urls` auth + TTL ambiguity

Run the four-step probe from §6 against the job id captured in V3, plus the
re-probe at ~10 min and ~1 h. Record the observed statuses, `Content-Length`, and
the point at which the URL stops working. Write the findings into
`openrouter-models/README.md` and this file's execution log. **No code branches on
the outcome in v1** — v1 always downloads via `/content` with the key.

### V7 — error surfaces are legible

1. Point `endpoint` at `https://openrouter.ai/api/v1` but corrupt the key →
   attempt an image generation → chat shows "OpenRouter rejected the API key",
   **not** "No cookie auth credentials found".
2. If the owner is willing, exhaust/spoof a 402 → the credits message appears.
3. Attach a deliberately huge source image to `edit` → the 413 message appears
   (this also empirically bounds open question Q1).

---

## 10. Open questions for the owner

Undocumented upstream — the plan refuses to guess, and each is a real decision:

- **Q1. Max input-image bytes/dimensions for `input_references` / `frame_images`.**
  Undocumented. The plan currently sends whatever the caller's file is, base64'd,
  and maps a 413 to a clear message (V7.3 bounds it empirically). Should the app
  pre-emptively downscale above some threshold, or keep failing loudly with the
  upstream's own limit? (Failing loudly is the current, clean-break choice.)
- **Q2. Video job TTL** — how long a completed job's output stays fetchable via
  `/content`. Matters if the poll loop ever exits early or a retry is added later.
- **Q3. `unsigned_urls` validity window** — measured in V6; no shipped behavior
  depends on it in v1, but a future "shareable link" feature would.
- **Q4. Do `unsigned_urls` require the `Authorization` header?** The docs
  contradict themselves. Resolved empirically in V6; v1 sidesteps it by
  downloading via `/content`.
- **Q5. Video concurrency caps** — unknown. If the platform ever runs two video
  generations at once (a loop, or two chats), do we need an app-level semaphore?
  v1 has none.
- **Q6. Paid-tier RPM/RPD limits** — unknown. v1's 429 handling is a single
  `Retry-After`-honoring retry. Enough, or should it be a bounded backoff chain?

Decisions the code alone cannot make:

- **Q7. `_VIDEO_TIMEOUT_S = 600.0` vs the 300.0 used by google-models
  (`:58`), fal-image (`:129`), and bedrock-models (`:1453`).** I chose 600 because
  OpenRouter verifiably serves 20 s clips (`openai/sora-2-pro`) and 4K/15 s
  (`bytedance/seedance-2.0`), where 300 s would time out on a legitimate job. This
  is a deliberate deviation from precedent — confirm, or hold parity at 300.
- **Q8. Ship `embedding` in v1?** OpenRouter verifiably serves 30 embedding
  models. Deferred because the `/embeddings` round-trip is unverified without a
  key and because `Capability.EMBEDDING` changes the registered type's
  `supports_embeddings` (`provider_helpers.py:325`) — a claim worth earning. Add
  now (needs the key first), or a follow-up slice?
- **Q9. Chat-path attribution headers.** `OpenAIProvider` offers no
  `default_headers` seam (`llm/openai.py:88-91`) and `extra_options` are per-request
  call kwargs, not client headers. So `X-OpenRouter-Title` can't be sent on chat
  without a core change (a `BrandedProviderSpec.default_headers` field). v1 sends
  it only on image/video. Accept, or open a core issue to add the field?
- **Q10. Should `sizes` carry aspect-ratio tokens?** `ImageGenModel.sizes` is
  documented as "supported output sizes (e.g. `1024x1024`)"
  (`image_gen/provider.py:20-22`), but OpenRouter expresses geometry as
  `resolution` **and** `aspect_ratio` tokens. The plan concatenates both into
  `sizes` so the picker offers exactly what the API accepts. Alternative: surface
  only `resolution` values and lose ratio control in the UI. Confirm the
  concatenation.
- **Q11. `generate_audio` default.** v1 sends `True` (when the model supports it)
  because a silent video is usually the surprise. Prefer `False` as the default,
  or a per-instance `settingsSchema` toggle? (A toggle adds a config field, so it
  needs the round-trip contract — hence asking rather than adding.)
- **Q12. Where should this plan file live long-term?** It created
  `PersonalClawApps/docs/plans/` (see the header). Keep it here, or move it under
  the core repo's `docs/roadmap/plans/`?

---

## Execution log

*(Append `DONE` / `DEVIATION` / `DISCOVERY` / `BLOCKED` entries here during
implementation, per the workspace execution protocol.)*

- 2026-07-28 — **DISCOVERY (pre-implementation verification).** Verified live
  against `https://openrouter.ai/api/v1`, unauthenticated:
  `GET /models` → 200, **358** models, output modalities `{text: 343,
  ["image","text"]: 11, ["audio","text"]: 4}` — i.e. **zero** pure-image and
  **zero** pure-video, confirming the default list silently omits them.
  `GET /models?output_modalities=all` → **465** models, adding `embeddings: 30`,
  `image: 29`, `video: 17`, `speech: 15`, `transcription: 12`, `rerank: 4`.
  `GET /models?output_modalities=image` → **40**.
  `GET /images/models` → 200, **38** entries; descriptor-key union is
  `aspect_ratio`(enum), `background`(enum), `input_references`(range),
  `n`(range), `output_compression`(range), `output_format`(enum), `quality`(enum),
  `resolution`(enum), `seed`(boolean) — exactly the documented grammar.
  Per-model caps confirmed sharply below the schema maxima
  (`microsoft/mai-image-2.5-pro`: `n max 1`, `input_references max 1`;
  `google/gemini-3-pro-image`: `n max 1`, `input_references max 14`;
  `bytedance-seed/seedream-4.5`: `n max 10`).
  `GET /videos/models` → 200, **17** entries with explicit arrays; `null` occurs
  for `supported_aspect_ratios` (`x-ai/grok-imagine-video-1.5`),
  `supported_frame_images` (`openai/sora-2-pro`), `generate_audio` (4 models),
  and `seed`. `supported_sizes` is `null` on every entry inspected.
  Unauthenticated `POST /images`, `POST /videos`, and `POST /images/generations`
  all return `{"error":{"message":"No cookie auth credentials found","code":401}}`
  with HTTP 401 — the misleading message is real; the undocumented alias exists.
- 2026-07-28 — **DISCOVERY (core constraint not in the brief).**
  `CONNECTOR = EgressPolicy(max_bytes=10_000_000, timeout_s=20.0)`
  (`net/policy.py:55`) and `_read_capped` (`net/client.py:207-220`) **truncates
  silently**. Core's `_materialize_video` (`mcp_artifacts.py:597-602`) fetches a
  provider-returned `url` with bare `CONNECTOR`, so any video over 10 MB handed
  back as a `url` is silently corrupted. This is why §6 returns `local_path` and
  asserts `not truncated`.

---

### 2026-07-28 — implementation session

**DONE — owner decisions applied.** Both overrides landed as directed:

- **Q8 (INCLUDE `embedding`).** `embedding` added to the manifest capabilities and
  `Capability.EMBEDDING` to `SPEC.capabilities`. Verified the consequence the plan
  flagged: `get_default_registry().capability_of("openrouter").supports_embeddings`
  is now `True` (locked by `test_embedding_capability_reaches_the_registered_type`).
  Re-verified the model count live: `GET /models?output_modalities=all` → 474 total,
  **30** with `output_modalities == ["embeddings"]`.
  ⚠️ **The `/embeddings` round-trip is UNVALIDATED until the owner's API key
  arrives**, and `supports_embeddings` on the registered type now depends on it. If
  the round-trip fails, that flag is the claim to retract.
- **Q7 (600 s, applied everywhere).** `_VIDEO_TIMEOUT_S = 600.0` here, and the three
  existing ceilings raised to 600 in a **separate, self-contained commit** on
  `improvement-video-timeout` (branched off `main`; this app's branch is stacked on
  it). Verified each constant before editing rather than trusting the cited line —
  all three matched: `google-models/provider.py:58` (`_VIDEO_TIMEOUT_S = 300.0`),
  `fal-image/provider.py:129` (`_VIDEO_POLL_TIMEOUT_S = 300.0`),
  `bedrock-models/provider.py:1453` (`_VIDEO_POLL_TIMEOUT = 300`).

All other open questions took the plan's stated choice: **Q1** fail loudly on 413
(no pre-emptive downscale), **Q2/Q3/Q4** observation-only (v1 always downloads via
`/content` with the key, so no shipped behavior branches on the answer), **Q5** no
app-level semaphore, **Q6** a single `Retry-After`-honoring retry, **Q9** attribution
header on image/video only, **Q10** `sizes` concatenates resolution + aspect-ratio
tokens, **Q11** `generate_audio` defaults to `True` when supported, **Q12** the plan
file stays at this path.

**DEVIATION 1 — the app ships its own `ModelCatalog` instead of the stock
`BrandedCatalog`.** Forced by the Q8 decision, and the plan did not anticipate it.
`BrandedCatalog.list_models` calls `openai_compatible_list_models`, which issues a
bare `GET {base}/models` — and OpenRouter's default listing is **text-only**.
Measured live: the unfiltered route returns 367 models with **zero** embedding
models, while `?output_modalities=text,embeddings` returns **397** including all 30.
So declaring `embedding` while using the stock catalog would have advertised
embedding support with a permanently empty embedding picker. `OpenRouterCatalog`
issues the filtered request and is registered after `register_branded_app`
(`register_catalog` is documented last-wins, deliberately non-strict, so the swap is
clean and reload-safe). `test_catalog_replaces_the_stock_branded_catalog` locks the
ordering, since an inversion silently breaks embedding discovery.

**DEVIATION 2 — capability tags come from OpenRouter's declared `architecture`
block, not core's `infer_capabilities` id heuristic.** Measured against the live
397-model list, the id heuristic:
  - **misses 105 of the image-input models** (`qwen/qwen3.7-flash`, `x-ai/grok-4.5`,
    `moonshotai/kimi-k3`, … carry no `vision`/`vl-` marker), which would have kept
    them out of the `image_modality` pool; and
  - **mis-tags the 9 chat-models-with-image-output** (`google/gemini-3-pro-image`,
    `openai/gpt-5-image`, …) as `image_gen`, which — because
    `infer_capabilities` makes generation tags mutually exclusive with chat — would
    have dropped them out of the **chat** pool entirely.
OpenRouter states `input_modalities`/`output_modalities` explicitly for all 397
entries, so the declared data wins. Verified end to end against the live endpoint:
397 rows → 153 chat, 152 chat+image, 30 embedding, 29/28 multimodal, and
`google/gemini-3-pro-image` correctly retains `chat`.

**DISCOVERY — image/video discovery must use the dedicated routes, confirmed.** The
plan's instruction not to call bare `GET /models` is right for a second reason
beyond omission: the dedicated `/images/models` and `/videos/models` responses are
the **only** ones carrying `supported_parameters` / `supported_durations` /
`supported_aspect_ratios`. Those per-model caps are what the request builders need,
and they are absent from the chat listing. Re-verified live: `/images/models` → 200,
38 entries, descriptor-key union exactly as the plan documented; `/videos/models` →
200, 17 entries. Per-model caps confirmed far below the schema maxima — `n` maxes at
1 for 17 models, 6 for 11, 10 for 7 (schema max is 10); `input_references` maxes at
1 for 16 models and 16 for 6.

**DISCOVERY — `null` really does occur and is handled.** Live: `x-ai/grok-imagine-
video-1.5` reports `supported_aspect_ratios: null`, `openai/sora-2-pro` reports
`supported_frame_images: null`, `generate_audio` is `null` on 4 models, and
`supported_sizes` is `null` on every video entry inspected. Driving the real
adapters against the live endpoint confirmed the null-ratio model surfaces
`aspect_ratios == []` rather than raising.

**VALIDATED LIVE (no API key needed — the discovery routes answer
unauthenticated).** Ran the real adapters against the real API:
`OpenRouterCatalog.list_models()` → 397 rows with correct capability tags;
`OpenRouterImageProvider.list_models()` → 38 models with resolution+ratio size
tokens and `supports_edit` derived from `input_references`;
`OpenRouterVideoProvider.list_models()` → 17 models with per-model ratios and
`max_duration_s` from `max(supported_durations)`.

**NOT VALIDATED — needs the owner's key with credits.** This is the owner's
checklist; each is a path no test can prove:
  1. `POST /images` generation round-trip, and that the base64 decode → artifact
     store path renders inline.
  2. The image `edit` round-trip via `input_references`, and a live confirmation
     that `size` alongside `resolution`/`aspect_ratio` really is a 400 (the mutual
     exclusion is enforced from the docs, not from an observed rejection).
  3. The video submit → poll → download cycle end to end, including that a real
     clip stays under the 256 MB download cap and that `duration` snapping is
     accepted upstream.
  4. The `/embeddings` round-trip — **load-bearing for Q8**, since
     `supports_embeddings` on the registered type now asserts it.
  5. Whether `unsigned_urls` require the `Authorization` header, and their validity
     window (plan §6 / V6). Nothing branches on the answer in v1.
  6. That a 402/413/429 surfaces the intended message against real upstream
     responses (the status→message mapping is unit-tested, not observed).

**GATES — all green.** `python -m pytest openrouter-models -q` → **112 passed**;
the three timeout-commit apps → **9 / 24 / 31 passed**; every bundle in the repo run
per-bundle as CI does → green (`slack-channel` needs its declared `slack_sdk`, which
CI installs per-bundle — pre-existing, unrelated). Manifest round-trip through core's
real `AppManifest.from_dict`/`to_dict` → `manifest OK`, and the full CI loop → `OK: 39
manifests valid`. Boundary lint from both sides: the apps-repo AST lint → `OK: all app
imports go through personalclaw.sdk.*`, and core's
`tests/test_apps_import_boundary.py` → **58 passed**, explicitly covering
`apps/openrouter-models/provider.py` (run behind a temporary `apps` →
`PersonalClawApps` symlink, since the test skips without `<workspace>/apps`; the
symlink was removed afterwards and **no core file was modified**). flake8 at the
project's 100-char contract → `provider.py` clean; the test files' `E402` is
structural (the `openai` stub must precede `import provider`) and matches every
sibling app. Also confirmed the suite is genuinely offline: re-run with
`socket.connect` patched to raise, all 112 still pass — no test reaches the network.

---

- 2026-07-28 — **DONE (end-to-end validation with a funded key).** Every capability
  driven through the app's OWN provider objects (not curl), so what is proven is the
  shipped code path. Cost: **$0.82**, almost all of it the one video generation.
  - **Chat** — streamed completion returns `PONG`. **Vision** — an `image_url`
    content part on `openai/gpt-4o-mini` correctly reads a 64×64 red PNG ("Red.").
    (A 1×1 PNG is rejected upstream as `image_parse_error`; that was a bad test
    fixture, not an app defect.)
  - **Embedding (Q8, load-bearing)** — `/embeddings` round-trip returns 2 vectors of
    2560 dims via `provider.embed()`. `supports_embeddings=True` **stands**.
  - **Image generation** — plain, `aspect_ratio="16:9"`, and `resolution="1K"` all
    return valid decodable JPEGs. Verified by decoding, not by byte count.
  - **Image editing** — verified by CONTENT, not just status: a red source square
    came back green, proving `input_references` reached the model and was used. A
    mask is refused pre-flight as designed.
  - **Video** — full submit → poll → download on `bytedance/seedance-1-5-pro`:
    a **4.05s 960×960 H.264+AAC MP4, 1.91 MB** at `local_path`, far under the 256 MB
    cap; requested `duration_seconds=4` and `1:1` both honored.
  - **Errors** — 401/402/413/429/502/524 all produce actionable text; unreadable
    source path and oversized input both fail cleanly with typed errors, no hang.
  - **V5 (`unsigned_urls`)** — still unvalidated, and still nothing branches on it.
- 2026-07-28 — **DISCOVERY → FIX. `test_connection()` reported OK for a bad key.**
  The probe validated the key by calling `list_models()`, but `GET /models` is a
  PUBLIC route: measured live, it returns **200 with a garbage key and with no key at
  all**. So Settings → "Test connection" went green on a typo'd key — precisely the
  answer that button exists to rule out. Now probes `GET /key` (401
  `{"error":{"message":"User not found."}}`), which is free. Two regression tests
  added with a per-route fetch stub; both were confirmed to FAIL against the old
  implementation before the fix was kept.
- 2026-07-28 — **DISCOVERY. The `size`/`resolution` mutual exclusion in the plan was
  wrong in one direction, and right in the other.** Measured live:
  `size` + `aspect_ratio` **is** a hard 400 (`size "1024x1024" conflicts with
  aspect_ratio "16:9"`), but `size` + `resolution` is **accepted**, with `size`
  winning. Sending exactly one key is still correct — a redundant pair would depend
  on which one upstream happens to prefer — but the test comment asserting a
  "documented 400" for the accepted pair was corrected to say what actually happens.
- 2026-07-28 — **DISCOVERY → FIX. An out-of-range pixel `size` was a dead end.**
  Zero of 38 image models advertise a `size` parameter (all declare only
  `resolution`/`aspect_ratio`), yet `size` IS honored and is the highest-fidelity
  option — and it is the form core's own `image_generate` tool schema suggests to the
  agent (`"e.g. '1024x1024'"`), so it is the COMMON path. Upstream resolves it to a
  resolution tier and rejects a tier the model doesn't list, but its message names
  only that tier ("Image size 2K is not supported for this model"), which the caller
  cannot act on: it asked for a WxH, not for "2K". A tier mapping was attempted and
  then **abandoned as unsound** — the mapping is not a function of the dimensions
  (`1024x1024` ⇒ 1K but `1408x768` ⇒ 2K), so snapping would silently deliver a size
  the caller did not request. Instead the model's real enum is appended to the error;
  verified live, the message now lists all 15 accepted sizes. Two tests added,
  including one asserting a 402 is NOT decorated with a size hint.
- 2026-07-28 — **DEVIATION (owner-facing). `image_modality` removed from the
  manifest capability list.** Core aliases `image_modality` → `vision`
  (`provider_bridge.py:35`), and that list renders verbatim as chips in
  `ProviderCard` + `ModelBackends`, so declaring both printed image input twice under
  two names; nothing in `registry.register()` branches on it either. Every sibling
  model app declares only `vision`. `image_modality` remains correct for PER-MODEL
  catalog tags. Test that pinned the wrong value replaced with one asserting the
  invariant.

**GATES after validation — all green.** `pytest openrouter-models -q` → **117
passed** (112 + 5 added). `OK: 39 manifests valid`. Boundary lint both sides: apps-repo
AST lint clean; core's `test_apps_import_boundary.py` → **58 passed**, explicitly
covering `apps/openrouter-models/provider.py`. flake8 at 100 chars → clean apart from
the structural `E402` in tests that every sibling shares.
