"""Together AI model provider (standalone app).

Speaks the **OpenAI-compatible** inference protocol over Together AI's endpoint
(https://api.together.xyz/v1). The wire client (``OpenAIProvider``) is a supported standard that lives
in core and is exposed via ``personalclaw.sdk.model``; this app carries only the
provider-specific bits — the default endpoint, the API-key env var, its
capability set, and how Together's model records say what each model does — via the
shared ``register_branded_app`` helper. Models come from live ``/v1/models`` discovery
(no hardcoded catalog).

Bring your own API key (config ``api_key`` or the ``TOGETHER_API_KEY`` environment variable).
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
    type="together",
    protocol="openai",
    default_base_url="https://api.together.xyz/v1",
    api_key_env="TOGETHER_API_KEY",
    default_model="",  # no curated pick: a call names its binding or the instance's Default Model
    capabilities=frozenset({Capability.CHAT, Capability.CODE_TOOLS, Capability.STREAMING, Capability.VISION}),
        # No hardcoded fallback (de-hardcode directive 2026-07-06): this is an
        # OpenAI-compatible provider — models come from live /v1/models discovery.
        fallback_models=(),
    # NONE - no prompt-caching documentation exists for Together's serverless inference
    # (docs.together.ai/docs/prompt-caching -> HTTP 404, checked 2026-08-18). Nothing
    # substantiates AUTOMATIC, so NONE - the posture that promises nothing.
    prompt_cache=PromptCache.NONE,
    notes="Together AI serverless inference (OpenAI-compatible). Bring your own Together API key.",
)

#: The understanding tags a chat model may also carry: what it reads besides text.
_READS = ("image_modality", "audio_modality", "video_modality")


def _capabilities_of(record: dict[str, Any]) -> list[str]:
    """What one of Together's ``/v1/models`` records says the model can be bound for.

    Each record names its ``type``. A ``chat`` model is a chat model, reading images where its
    id says so (a ``-VL`` model); an ``embedding`` model is an embedding model; an ``image``
    model makes images, through Together's OpenAI-compatible images endpoint. ``language`` and
    ``code`` models are base models that complete a prompt and hold no conversation, and
    ``moderation`` and ``rerank`` models score text: none of them is offered. A type this reading
    does not name is offered only for the speech job its id names (a Whisper model transcribes),
    and never for chat.
    """
    kind = str(record.get("type") or "").lower()
    inferred = infer_capabilities(str(record.get("id") or ""))
    if kind == "chat":
        return ["chat", *[c for c in inferred if c in _READS]]
    if kind == "embedding":
        return ["embedding"]
    if kind == "image":
        return ["image_gen"]
    if kind in ("language", "code", "moderation", "rerank"):
        return []
    return [c for c in inferred if c in ("stt", "tts")]


# Registers the provider TYPE + catalog on import (the app loader imports this module).
_factory, create_provider, create_catalog = register_branded_app(
    SPEC, capabilities_of=_capabilities_of
)
