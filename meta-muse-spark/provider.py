"""Meta Muse Spark model provider (standalone app).

Speaks the **OpenAI-compatible** inference protocol over the Meta AI API
(https://api.meta.ai/v1). The wire client (``OpenAIProvider``) lives in core and is exposed via
``personalclaw.sdk.model``; this app carries only what is Meta's — the default endpoint, the
API-key env var, its capability set and the one model it knows of — through the shared
``register_branded_app`` helper.

So an instance's Test connection probes the instance's own Base URL, as its calls do, with the
strict model listing, and a failure says its own cause (unreachable, refused by Denied hosts, a
rejected key) instead of "check key". The picker offers the models the endpoint lists for the
key, or the one model below when the listing can't be read.

Bring your own API key (config ``api_key`` or the ``META_MODEL_API_KEY`` environment variable).
"""

from __future__ import annotations

from personalclaw.sdk.model import (
    BrandedProviderSpec,
    Capability,
    PromptCache,
    register_branded_app,
)

META_BASE_URL = "https://api.meta.ai/v1"

SPEC = BrandedProviderSpec(
    type="meta_muse_spark",
    protocol="openai",
    default_base_url=META_BASE_URL,
    api_key_env="META_MODEL_API_KEY",
    default_model="",  # no curated pick: a call names its binding or the instance's Default Model
    # No tools capability is declared and none is implemented here: the app registers
    # chat/streaming/vision only, so the flag must not claim otherwise.
    capabilities=frozenset({Capability.CHAT, Capability.STREAMING, Capability.VISION}),
    # Offered when the endpoint's listing can't be read (unreachable, refused, no list).
    fallback_models=(
        {
            "id": "muse-spark-1.1",
            "name": "muse-spark-1.1",
            "capabilities": ["chat", "image_modality", "streaming"],
        },
    ),
    # NONE - no published prompt-caching behaviour for this endpoint that the app can
    # cite, so nothing substantiates AUTOMATIC. NONE is the honest default.
    prompt_cache=PromptCache.NONE,
    notes="Meta Muse Spark via the Meta AI API (OpenAI-compatible); 1M context.",
)

# Registers the provider TYPE + catalog on import (the app loader imports this module).
_factory, create_provider, create_catalog = register_branded_app(SPEC)
