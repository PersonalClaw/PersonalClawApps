"""Groq model provider (standalone app).

Speaks the **OpenAI-compatible** inference protocol over Groq's endpoint
(https://api.groq.com/openai/v1). The wire client (``OpenAIProvider``) is a supported standard that lives
in core and is exposed via ``personalclaw.sdk.model``; this app carries only the
provider-specific bits — the default endpoint, the API-key env var, and its
capability set — via the shared ``register_branded_app`` helper. Models come from
live ``/v1/models`` discovery (no hardcoded catalog).

Bring your own API key (config ``api_key`` or the ``GROQ_API_KEY`` environment variable).

Images: the connection carries image parts (``Capability.VISION``), and which models read them is
said per model on the catalog rows, the record the platform decides by. Core's id classifier tags
the ids that say so (``…-vision-…``); :data:`VISION_MODELS` names the ones whose ids don't.
"""

from __future__ import annotations

import dataclasses

from personalclaw.sdk.model import (
    BrandedProviderSpec,
    Capability,
    ConnectionResult,
    ModelCatalog,
    ModelInfo,
    PromptCache,
    get_default_registry,
    register_branded_app,
)

#: The Groq models that take images whose ids don't say so, from Groq's vision documentation:
#: Llama 4 Scout and Maverick are natively multimodal. Everything else keeps what its id implies.
VISION_MODELS = frozenset(
    {
        "meta-llama/llama-4-scout-17b-16e-instruct",
        "meta-llama/llama-4-maverick-17b-128e-instruct",
    }
)

SPEC = BrandedProviderSpec(
    type="groq",
    protocol="openai",
    default_base_url="https://api.groq.com/openai/v1",
    api_key_env="GROQ_API_KEY",
    default_model="",  # no curated pick: a call names its binding or the instance's Default Model
    capabilities=frozenset(
        {Capability.CHAT, Capability.CODE_TOOLS, Capability.STREAMING, Capability.VISION}
    ),
        # No hardcoded fallback (de-hardcode directive 2026-07-06): this is an
        # OpenAI-compatible provider — models come from live /v1/models discovery.
        fallback_models=(),
    # NONE - Groq's caching IS automatic and cannot be disabled, but Groq's own docs scope
    # it to a handful of models ("openai/gpt-oss-20b", "-120b", "-safeguard-20b"; the
    # OpenRouter matrix says Kimi K2). The served model is whichever one Settings → Models
    # or the instance's Default Model names, so it is unknown at declaration time and a
    # provider-wide AUTOMATIC would promise hits most selections never get.
    # Revisit if the posture contract ever grows a per-model axis.
    prompt_cache=PromptCache.NONE,
    notes="Groq LPU inference (OpenAI-compatible), very low latency. Bring your own Groq API key.",
)

# Registers the provider TYPE + catalog on import (the app loader imports this module).
_factory, create_provider, _branded_catalog = register_branded_app(SPEC)


class GroqCatalog(ModelCatalog):
    """Groq's live model list, with image input stated for :data:`VISION_MODELS`.

    Discovery, its fallback and the connection test are the branded catalog's, unchanged.
    """

    def __init__(self, branded: ModelCatalog) -> None:
        self._branded = branded

    async def list_models(self) -> list[ModelInfo]:
        return [_declare_vision(row) for row in await self._branded.list_models()]

    async def test_connection(self) -> ConnectionResult:
        return await self._branded.test_connection()


def _declare_vision(row: ModelInfo) -> ModelInfo:
    """``row`` with ``image_modality`` added when it is a chat model in :data:`VISION_MODELS`."""
    caps = list(row.capabilities)
    if row.id not in VISION_MODELS or "chat" not in caps or "image_modality" in caps:
        return row
    return dataclasses.replace(row, capabilities=[*caps, "image_modality"])


def create_catalog(options: dict | None = None, *, model: str = "") -> GroqCatalog:
    """Catalog factory (registry contract): the branded catalog, with Groq's vision models."""
    return GroqCatalog(_branded_catalog(options, model=model))


# register_branded_app registered its stock catalog under this type; register_catalog is
# last-wins by contract, so this swaps in the one that states which models read images.
get_default_registry().register_catalog(SPEC.type, create_catalog)
