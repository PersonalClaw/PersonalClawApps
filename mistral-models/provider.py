"""Mistral AI model provider (standalone app).

Speaks the **OpenAI-compatible** inference protocol over Mistral AI's endpoint
(https://api.mistral.ai/v1). The wire client (``OpenAIProvider``) is a supported standard that lives
in core and is exposed via ``personalclaw.sdk.model``; this app carries only the
provider-specific bits — the default endpoint, the API-key env var, and its
capability set — via the shared ``register_branded_app`` helper. Models come from
live ``/v1/models`` discovery (no hardcoded catalog).

Bring your own API key (config ``api_key`` or the ``MISTRAL_API_KEY`` environment variable).
"""

from __future__ import annotations

from personalclaw.sdk.model import BrandedProviderSpec, Capability, register_branded_app

SPEC = BrandedProviderSpec(
    type="mistral",
    protocol="openai",
    default_base_url="https://api.mistral.ai/v1",
    api_key_env="MISTRAL_API_KEY",
    default_model="",  # de-hardcoded: resolved from live /v1/models discovery at start()
    capabilities=frozenset({Capability.CHAT, Capability.CODE_TOOLS, Capability.STREAMING, Capability.VISION}),
        # No hardcoded fallback (de-hardcode directive 2026-07-06): this is an
        # OpenAI-compatible provider — models come from live /v1/models discovery.
        fallback_models=(),
    notes="Mistral AI models via its OpenAI-compatible endpoint. Bring your own Mistral API key.",
)

# Registers the provider TYPE + catalog on import (the app loader imports this module).
_factory, create_provider, create_catalog = register_branded_app(SPEC)
