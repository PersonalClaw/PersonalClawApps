"""Alibaba Cloud Model Studio (DashScope) provider.

Provides:
  - **Chat/Code/Streaming** via OpenAI-compatible endpoint (``register_branded_app``)
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
    MediaCatalog,
    MediaModel,
    register_branded_app,
    register_media_catalog,
)

logger = logging.getLogger(__name__)

_DEFAULT_ENDPOINT = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
_IMAGE_TIMEOUT_S = 120.0

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
    default_model="",  # resolved from live model discovery
    capabilities=frozenset({
        Capability.CHAT, Capability.CODE_TOOLS, Capability.STREAMING,
        Capability.EMBEDDING,
    }),
    fallback_models=(),
    notes="Alibaba Model Studio (DashScope) — Qwen chat + embedding. Select your regional endpoint.",
)

_factory, _create_chat_provider, create_catalog = register_branded_app(SPEC)

# Register embedding catalog so the embedding adapter resolves `alibaba:model` refs.
register_media_catalog(
    "embedding", "alibaba",
    MediaCatalog(
        models=(
            MediaModel(name="text-embedding-v3", description="DashScope Text Embedding v3"),
            MediaModel(name="text-embedding-v2", description="DashScope Text Embedding v2"),
        ),
        default_model="text-embedding-v3",
    ),
)

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

        key = self._key()
        if not key:
            raise ImageGenError("No Alibaba API key configured (set ALIBABA_API_KEY).")

        model_id = model if model else "qwen-image-2.0"

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
                        raise ImageGenError(
                            f"Alibaba image generation failed (HTTP {resp.status}): "
                            f"{_error_detail(text)}"
                        )
                    data = json.loads(text)
        except ImageGenError:
            raise
        except asyncio.TimeoutError as e:
            raise ImageGenError("Alibaba image generation timed out.") from e
        except Exception as e:
            raise ImageGenError(f"Alibaba image generation request failed: {e}") from e

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
    from personalclaw.providers.media_scanners import register_scanner as _reg_scanner

    _reg_scanner("image_gen", _scan_image)
except Exception:  # noqa: BLE001 — older core without the extension point
    logger.debug("media_scanners extension point unavailable", exc_info=True)
