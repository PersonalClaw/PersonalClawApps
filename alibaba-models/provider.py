"""Alibaba Cloud Model Studio (DashScope) provider.

Provides:
  - **Chat/Code/Streaming** via OpenAI-compatible endpoint (``register_branded_app``)
  - **Image input** on the Qwen models that read images (``Capability.VISION``; per model on the
    catalog rows, see :func:`takes_images`)
  - **Embedding** via the same OpenAI-compat endpoint (``/embeddings``)
  - **Image generation** via OpenAI-compat ``/images/generations`` (Qwen-Image, Wan)

Regional endpoints are selectable via the ``endpoint`` settings field — supports
Token Plan (Singapore), workspace-based regional URLs, and legacy international/
China endpoints.

Auth: ``Authorization: Bearer {key}`` with the API key from config or
``ALIBABA_API_KEY`` env var.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
from typing import Any

from personalclaw.sdk.image import (
    ImageGenError,
    ImageGenModel,
    ImageGenProvider,
    ImageResult,
)
from personalclaw.sdk.model import (
    BrandedProviderSpec,
    Capability,
    ConnectionResult,
    ModelCatalog,
    ModelInfo,
    PromptCache,
    ProviderResolutionError,
    get_default_registry,
    register_branded_app,
    require_model,
)
from personalclaw.sdk.net import sentence_with_detail

logger = logging.getLogger(__name__)

_DEFAULT_ENDPOINT = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
_IMAGE_TIMEOUT_S = 120.0

# Where an instance is configured. Endpoint sits under the form's Advanced disclosure, so a step
# that names it says so.
_ON_INSTANCE = "on this Alibaba Model Studio instance in Settings → Providers"
_ENDPOINT_STEP = f"check Endpoint {_ON_INSTANCE} (under Advanced)"
# What to change when Model Studio refuses an image request as invalid or answers it "not found":
# the model or options asked for, or the regional endpoint, which may not serve that model.
_REQUEST_STEP = (
    "Choose another model in Settings → Models or change the size or options you asked for; if "
    f"no model works, {_ENDPOINT_STEP}."
)

# ── Helpers ──────────────────────────────────────────────────────────────────


def _resolve_api_key(config: dict[str, Any]) -> str:
    """Resolve API key from config or environment."""
    key = str(config.get("api_key", "") or "")
    if key:
        return key
    return os.environ.get("ALIBABA_API_KEY", "")


def _resolve_endpoint(config: dict[str, Any]) -> str:
    """Resolve the base URL endpoint from config."""
    endpoint = str(config.get("endpoint", "") or "")
    return endpoint if endpoint else _DEFAULT_ENDPOINT


# ── Chat provider (branded, OpenAI-compat) ───────────────────────────────────

SPEC = BrandedProviderSpec(
    type="alibaba",
    protocol="openai",
    default_base_url=_DEFAULT_ENDPOINT,
    api_key_env="ALIBABA_API_KEY",
    default_model="",  # no curated pick: a call names its binding or the instance's Default Model
    capabilities=frozenset({
        Capability.CHAT, Capability.CODE_TOOLS, Capability.STREAMING,
        Capability.VISION, Capability.EMBEDDING,
    }),
    fallback_models=(),
    # NONE - Qwen DOES cache, but only behind an EXPLICIT per-message breakpoint
    # (OpenRouter's provider matrix: "Alibaba prompt caching requires explicit cache
    # breakpoints", cache_control: {"type": "ephemeral"}). This app rides core's
    # OpenAIProvider, which places no marker, so EXPLICIT here would mark a message that
    # nothing translates. NONE is the honest posture until a Qwen-specific adapter exists.
    prompt_cache=PromptCache.NONE,
    notes="Alibaba Model Studio (DashScope) — Qwen chat + embedding. Select your regional endpoint.",
)

_factory, _create_chat_provider, _branded_catalog = register_branded_app(SPEC)


def takes_images(model_id: str) -> bool:
    """Whether a DashScope model reads images though its id carries no marker core's classifier
    knows: the QVQ visual-reasoning models (``qvq-max``) and the Omni models (``qwen-omni-turbo``,
    ``qwen3-omni-flash``), which Model Studio documents as taking image input. The Qwen-VL family
    (``qwen-vl-max``, ``qwen3-vl-plus``) is already tagged by its ``-vl`` marker."""
    mid = model_id.lower()
    return mid.startswith("qvq-") or "omni" in mid


class AlibabaCatalog(ModelCatalog):
    """DashScope's live model list, with image input stated where :func:`takes_images` says so.

    Discovery, its fallback and the connection test are the branded catalog's, unchanged.
    """

    def __init__(self, branded: ModelCatalog) -> None:
        self._branded = branded

    async def list_models(self) -> list[ModelInfo]:
        return [_declare_vision(row) for row in await self._branded.list_models()]

    async def test_connection(self) -> ConnectionResult:
        return await self._branded.test_connection()


def _declare_vision(row: ModelInfo) -> ModelInfo:
    """``row`` with ``image_modality`` added when it is a chat model that takes images."""
    caps = list(row.capabilities)
    if not takes_images(row.id) or "chat" not in caps or "image_modality" in caps:
        return row
    return dataclasses.replace(row, capabilities=[*caps, "image_modality"])


def create_catalog(options: dict[str, Any] | None = None, *, model: str = "") -> AlibabaCatalog:
    """Catalog factory (registry contract): the branded catalog, with the models that read images."""
    return AlibabaCatalog(_branded_catalog(options, model=model))


# register_branded_app registered its stock catalog under this type; register_catalog is
# last-wins by contract, so this swaps in the one that states which models read images.
get_default_registry().register_catalog(SPEC.type, create_catalog)

# ── Image generation models (static catalog) ────────────────────────────────

_IMAGE_MODELS = [
    ImageGenModel(
        name="qwen-image-2.0",
        description="Qwen Image 2.0 — fast general-purpose image generation",
        sizes=["1024x1024", "720x1280", "1280x720"],
        supports_edit=False,
        downloaded=True,
        active=False,
    ),
    ImageGenModel(
        name="qwen-image-2.0-pro",
        description="Qwen Image 2.0 Pro — higher fidelity",
        sizes=["1024x1024", "720x1280", "1280x720"],
        supports_edit=False,
        downloaded=True,
        active=False,
    ),
    ImageGenModel(
        name="wan2.7-image",
        description="Wan 2.7 Image — creative image generation",
        sizes=["1024x1024", "720x1280", "1280x720"],
        supports_edit=False,
        downloaded=True,
        active=False,
    ),
    ImageGenModel(
        name="wan2.7-image-pro",
        description="Wan 2.7 Image Pro — premium quality",
        sizes=["1024x1024", "720x1280", "1280x720"],
        supports_edit=False,
        downloaded=True,
        active=False,
    ),
]


# ── Image Provider ───────────────────────────────────────────────────────────


class AlibabaImageProvider(ImageGenProvider):
    """Image generation via the DashScope OpenAI-compat /images/generations endpoint.

    Supports Qwen-Image and Wan model families.
    """

    def __init__(self, *, api_key: str = "", endpoint: str = "", name: str = "alibaba") -> None:
        self._api_key = api_key
        self._endpoint = endpoint or _DEFAULT_ENDPOINT
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @name.setter
    def name(self, value: str) -> None:
        self._name = value

    @property
    def display_name(self) -> str:
        return "Alibaba Model Studio (image)"

    def _key(self) -> str:
        return self._api_key or os.environ.get("ALIBABA_API_KEY", "")

    async def is_available(self) -> bool:
        return bool(self._key())

    async def list_models(self) -> list[ImageGenModel]:
        return list(_IMAGE_MODELS)

    async def generate(
        self,
        prompt: str,
        *,
        model: str = "",
        size: str = "",
        n: int = 1,
        **opts: Any,
    ) -> list[ImageResult]:
        import aiohttp

        # Like chat, a call names its model (the image binding in Settings → Models), and it is
        # refused when it names none. This used to take qwen-image-2.0 in its place.
        try:
            model_id = require_model(model)
        except ProviderResolutionError as exc:
            raise ImageGenError(str(exc)) from exc
        key = self._key()
        if not key:
            raise ImageGenError("No Alibaba API key configured (set ALIBABA_API_KEY).")

        # Use the OpenAI-compat images endpoint at the configured base URL.
        base = self._endpoint.rstrip("/")
        url = f"{base}/images/generations"
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        body: dict[str, Any] = {"model": model_id, "prompt": prompt, "n": n}
        if size:
            body["size"] = size

        timeout = aiohttp.ClientTimeout(total=_IMAGE_TIMEOUT_S)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, json=body) as resp:
                    text = await resp.text()
                    if resp.status != 200:
                        raise ImageGenError(_status_message(resp.status, text))
                    data = json.loads(text)
        except ImageGenError:
            raise
        except asyncio.TimeoutError as e:
            raise ImageGenError("Alibaba image generation timed out.") from e
        except Exception as e:
            raise ImageGenError(_unanswered_message(e)) from e

        results: list[ImageResult] = []
        for item in data.get("data", []):
            if not isinstance(item, dict):
                continue
            img_url = item.get("url", "")
            b64 = item.get("b64_json", "")
            if img_url or b64:
                results.append(ImageResult(
                    url=img_url, b64=b64,
                    revised_prompt=item.get("revised_prompt", ""),
                ))
        if not results:
            raise ImageGenError("Alibaba returned no images.")
        return results

    async def edit(
        self,
        prompt: str,
        *,
        source_image: str,
        mask: str = "",
        model: str = "",
        size: str = "",
        n: int = 1,
        **opts: Any,
    ) -> list[ImageResult]:
        raise ImageGenError("Alibaba image editing is not supported yet.")


def _error_detail(text: str) -> str:
    try:
        return str(json.loads(text).get("error", {}).get("message", ""))[:200]
    except Exception:
        return text[:200]


def _status_message(status: int, text: str) -> str:
    """What an image request's HTTP failure means and what to do, then Model Studio's words.

    Keyed on the status class. A key refusal names the Endpoint too, because a Model Studio key
    works only in the region it was made in, and the wrong regional endpoint refuses a key that is
    right.
    """
    if status in (401, 403):
        sentence = (
            f"Alibaba Model Studio refused the API key for the image request (HTTP {status}). "
            f"Check API Key {_ON_INSTANCE} (or ALIBABA_API_KEY), that it may use this model, and "
            "that Endpoint (under Advanced) is in the key's region: a key works only in its own "
            "region."
        )
    elif status == 400:
        sentence = (
            f"Alibaba Model Studio refused the image request as invalid (HTTP 400). {_REQUEST_STEP}"
        )
    elif status == 404:
        sentence = (
            "Alibaba Model Studio answered the image request with HTTP 404 (not found). "
            f"{_REQUEST_STEP}"
        )
    elif status == 429:
        sentence = (
            "Alibaba Model Studio's rate limit or quota stopped the image request (HTTP 429). "
            "Wait a minute and try again."
        )
    elif 500 <= status < 600:
        sentence = (
            f"Alibaba Model Studio failed on its side (image request, HTTP {status}). Try again "
            "in a few minutes."
        )
    else:
        sentence = f"Alibaba Model Studio's image request failed (HTTP {status}). {_REQUEST_STEP}"
    return sentence_with_detail(sentence, _error_detail(text))


def _unanswered_message(error: Exception) -> str:
    """The sentence for an image request that got no usable answer: unable to connect, answered
    with something that is not JSON, or failed some other way.

    Told apart by builtin types: the HTTP library raises an ``OSError`` for a connection it could
    not make, and it is imported only where the request is made.
    """
    if isinstance(error, (json.JSONDecodeError, UnicodeDecodeError)):
        sentence = (
            "Alibaba Model Studio's answer to the image request could not be read. Try again in "
            f"a moment; if it keeps happening, {_ENDPOINT_STEP}."
        )
    elif isinstance(error, OSError):
        sentence = (
            "The connection to Alibaba Model Studio failed during the image request. Check this "
            f"computer's internet connection and Endpoint {_ON_INSTANCE} (under Advanced), then "
            "try again."
        )
    else:
        sentence = (
            "The image request to Alibaba Model Studio failed unexpectedly. Try again in a moment."
        )
    return sentence_with_detail(sentence, error)


# ── Chat factory (multiInstance manifest entry point) ─────────────────────────


def create_provider(config: dict[str, Any] | None = None):
    """Chat provider factory (multi-instance, OpenAI-compat endpoint).

    Resolves the endpoint from config so the SPEC's default_base_url is
    overridden per-instance based on the user's regional selection. One
    ``alibaba`` config entry serves chat + embedding (OpenAI-compat) AND image
    generation (via the media scanner below) — surfacing as ONE provider.
    """
    cfg = config or {}
    endpoint = _resolve_endpoint(cfg)
    cfg_with_endpoint = dict(cfg)
    cfg_with_endpoint["base_url"] = endpoint
    return _create_chat_provider(cfg_with_endpoint)


# ── Media-capability config scanner ───────────────────────────────────────────
# Image generation resolves through the image_gen registry, which builds a
# per-config adapter. Alibaba contributes its adapter via the app-owned
# ``media_scanners`` extension point — one adapter per ``alibaba`` config entry,
# keyed by the entry name so ``<name>:model`` refs resolve to that entry.


def _alibaba_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for e in entries:
        ptype = str(e.get("type", ""))
        if ptype == "alibaba" or str((e.get("options") or {}).get("_original_type", "")) == "alibaba":
            out.append(e)
    return out


def _scan_image(entries: list[dict[str, Any]]) -> list:
    out = []
    for e in _alibaba_entries(entries):
        opts = e.get("options") or {}
        out.append(AlibabaImageProvider(
            api_key=str(opts.get("api_key", "") or ""),
            endpoint=str(opts.get("endpoint", "") or ""),
            name=str(e["name"]),
        ))
    return out


try:
    from personalclaw.sdk.model import register_scanner as _reg_scanner

    _reg_scanner("image_gen", _scan_image)
except Exception:  # noqa: BLE001 — older core without the extension point
    logger.debug("media_scanners extension point unavailable", exc_info=True)
