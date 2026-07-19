"""Generic Anthropic-compatible endpoint provider (standalone app).

The "bring your own Anthropic-compatible endpoint" app: point it at any server that
speaks the Anthropic Messages protocol by supplying the base URL + API key. It
registers the ``anthropic_compatible`` provider TYPE — the type the Settings → "Add
provider → Anthropic-Compatible" flow persists — so this app is installed by default.

The Anthropic wire has no models-list endpoint, so discovery falls back to the
configured default model. The wire client (``AnthropicProvider``) lives in core and
is exposed via ``personalclaw.sdk.model``.
"""

from __future__ import annotations

from personalclaw.sdk.model import BrandedProviderSpec, Capability, register_branded_app

SPEC = BrandedProviderSpec(
    type="anthropic_compatible",
    protocol="anthropic",
    default_base_url="",           # user MUST supply the endpoint (no default host)
    api_key_env="ANTHROPIC_API_KEY",
    default_model="",
    max_tokens=4096,               # the Anthropic wire requires a max_tokens
    capabilities=frozenset({
        Capability.CHAT, Capability.CODE_TOOLS, Capability.STREAMING, Capability.VISION,
    }),
    fallback_models=(),            # no models endpoint; picker uses the configured model
    notes="Any Anthropic-compatible (Messages API) endpoint; no models-list endpoint.",
)

# Registers the provider TYPE + catalog on import (the app loader imports this module).
_factory, create_provider, create_catalog = register_branded_app(SPEC)
