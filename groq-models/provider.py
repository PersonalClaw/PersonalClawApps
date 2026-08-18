"""Groq model provider (standalone app).

Speaks the **OpenAI-compatible** inference protocol over Groq's endpoint
(https://api.groq.com/openai/v1). The wire client (``OpenAIProvider``) is a supported standard that lives
in core and is exposed via ``personalclaw.sdk.model``; this app carries only the
provider-specific bits — the default endpoint, the API-key env var, and its
capability set — via the shared ``register_branded_app`` helper. Models come from
live ``/v1/models`` discovery (no hardcoded catalog).

Bring your own API key (config ``api_key`` or the ``GROQ_API_KEY`` environment variable).
"""

from __future__ import annotations

from personalclaw.sdk.model import (
    BrandedProviderSpec,
    Capability,
    PromptCache,
    register_branded_app,
)

SPEC = BrandedProviderSpec(
    type="groq",
    protocol="openai",
    default_base_url="https://api.groq.com/openai/v1",
    api_key_env="GROQ_API_KEY",
    default_model="",  # de-hardcoded: resolved from live /v1/models discovery at start()
    capabilities=frozenset({Capability.CHAT, Capability.CODE_TOOLS, Capability.STREAMING}),
        # No hardcoded fallback (de-hardcode directive 2026-07-06): this is an
        # OpenAI-compatible provider — models come from live /v1/models discovery.
        fallback_models=(),
    # NONE - Groq's caching IS automatic and cannot be disabled, but Groq's own docs scope
    # it to a handful of models ("openai/gpt-oss-20b", "-120b", "-safeguard-20b"; the
    # OpenRouter matrix says Kimi K2). This app pins default_model="" and resolves the
    # model from live /v1/models discovery, so the served model is unknown at declaration
    # time and a provider-wide AUTOMATIC would promise hits most selections never get.
    # Revisit if the posture contract ever grows a per-model axis.
    prompt_cache=PromptCache.NONE,
    notes="Groq LPU inference (OpenAI-compatible), very low latency. Bring your own Groq API key.",
)

# Registers the provider TYPE + catalog on import (the app loader imports this module).
_factory, create_provider, create_catalog = register_branded_app(SPEC)
