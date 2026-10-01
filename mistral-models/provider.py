"""Mistral AI model provider (standalone app).

Speaks the **OpenAI-compatible** inference protocol over Mistral AI's endpoint
(https://api.mistral.ai/v1). The wire client (``OpenAIProvider``) is a supported standard that lives
in core and is exposed via ``personalclaw.sdk.model``; this app carries only the
provider-specific bits — the default endpoint, the API-key env var, its
capability set, and how Mistral's model records say what each model does — via the
shared ``register_branded_app`` helper. Models come from live ``/v1/models`` discovery
(no hardcoded catalog).

Bring your own API key (config ``api_key`` or the ``MISTRAL_API_KEY`` environment variable).
"""

from __future__ import annotations

from typing import Any

from personalclaw.sdk.model import (
    BrandedProviderSpec,
    Capability,
    PromptCache,
    infer_capabilities,
    register_branded_app,
)

SPEC = BrandedProviderSpec(
    type="mistral",
    protocol="openai",
    default_base_url="https://api.mistral.ai/v1",
    api_key_env="MISTRAL_API_KEY",
    default_model="",  # no curated pick: a call names its binding or the instance's Default Model
    capabilities=frozenset({Capability.CHAT, Capability.CODE_TOOLS, Capability.STREAMING, Capability.VISION}),
        # No hardcoded fallback (de-hardcode directive 2026-07-06): this is an
        # OpenAI-compatible provider — models come from live /v1/models discovery.
        fallback_models=(),
    # NONE - no prompt-caching documentation exists for the Mistral API
    # (docs.mistral.ai/capabilities/prompt_caching/ -> HTTP 404, checked 2026-08-18), so
    # nothing substantiates AUTOMATIC. Unverified beats mis-declared: NONE promises
    # nothing, whereas a wrong AUTOMATIC silently promises reads that never arrive.
    prompt_cache=PromptCache.NONE,
    notes="Mistral AI models via its OpenAI-compatible endpoint. Bring your own Mistral API key.",
)



def _capabilities_of(record: dict[str, Any]) -> list[str]:
    """What one of Mistral's ``/v1/models`` records says the model can be bound for.

    Each record carries ``capabilities``, Mistral's own flags (all false when absent). A model
    that answers a chat (``completion_chat``) is a chat model, and reads images (``vision``) or
    audio (``audio``) where it says so; one that transcribes (``audio_transcription``) is a
    speech-to-text model too, through Mistral's transcription endpoint. Mistral's records have no
    flag for embedding, and its embedding models say so by name (``mistral-embed``,
    ``codestral-embed``). The rest — OCR, moderation and classifier models, and models that only
    transcribe live or only speak — are offered for nothing: no job here drives them.
    """
    said = record.get("capabilities")
    said = said if isinstance(said, dict) else {}
    caps: list[str] = []
    if said.get("completion_chat"):
        caps.append("chat")
        if said.get("vision"):
            caps.append("image_modality")
        if said.get("audio"):
            caps.append("audio_modality")
    if said.get("audio_transcription"):
        caps.append("stt")
    if not caps and "embedding" in infer_capabilities(str(record.get("id") or "")):
        caps.append("embedding")
    return caps


# Registers the provider TYPE + catalog on import (the app loader imports this module).
_factory, create_provider, create_catalog = register_branded_app(
    SPEC, capabilities_of=_capabilities_of
)
