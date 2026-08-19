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

from personalclaw.sdk.model import (
    BrandedProviderSpec,
    Capability,
    PromptCache,
    register_branded_app,
)

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
    # EXPLICIT - protocol="anthropic" builds core's AnthropicProvider, which already
    # declares EXPLICIT (llm/anthropic.py) and translates the neutral marker into
    # `cache_control` on the hinted content block. The runtime posture is EXPLICIT whatever
    # this spec says, so any other value would make ProviderCapability contradict the
    # provider it describes - the one failure mode worse than declaring nothing.
    prompt_cache=PromptCache.EXPLICIT,
    notes="Any Anthropic-compatible (Messages API) endpoint; no models-list endpoint.",
)

# Registers the provider TYPE + catalog on import (the app loader imports this module).
_factory, create_provider, create_catalog = register_branded_app(SPEC)
