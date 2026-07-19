"""Generic OpenAI-compatible endpoint provider (standalone app).

The "bring your own OpenAI-compatible endpoint" app: point it at ANY server that
speaks the OpenAI Chat Completions protocol (a self-hosted gateway, an unlisted
cloud, an internal proxy, LM Studio, …) by supplying the base URL + API key. It
registers the ``openai_compatible`` provider TYPE — the type the Settings → "Add
provider → OpenAI-Compatible" flow persists — so this app is installed by default.

For a KNOWN branded provider (Together, Groq, DeepSeek, Mistral, Gemini) prefer that
provider's dedicated app, which ships its endpoint + fallback catalog. This generic
app is for endpoints those don't cover. The wire client (``OpenAIProvider``) lives in
core and is exposed via ``personalclaw.sdk.model``.
"""

from __future__ import annotations

from personalclaw.sdk.model import BrandedProviderSpec, Capability, register_branded_app

SPEC = BrandedProviderSpec(
    type="openai_compatible",
    protocol="openai",
    default_base_url="",          # user MUST supply the endpoint (no default host)
    api_key_env="OPENAI_API_KEY",  # a sensible env fallback; most set the key in config
    default_model="",
    capabilities=frozenset({
        Capability.CHAT, Capability.CODE_TOOLS, Capability.STREAMING,
        Capability.EMBEDDING, Capability.VISION,
    }),
    fallback_models=(),  # unknown endpoint → discovery via /v1/models, no curated list
    notes="Any OpenAI-compatible endpoint; capabilities are endpoint/model-dependent.",
)

# Registers the provider TYPE + catalog on import (the app loader imports this module).
_factory, create_provider, create_catalog = register_branded_app(SPEC)
