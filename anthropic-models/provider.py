"""Anthropic model provider (standalone app).

Speaks the **Anthropic-compatible** inference protocol — the wire client
(``AnthropicProvider``) is a supported standard that lives in core and is exposed via
``personalclaw.sdk.model``. This app owns the provider-specific bits: API-key auth,
config, its capability descriptor, and registration.
"""

from __future__ import annotations

import os
from typing import Any

from personalclaw.sdk.model import (
    AnthropicProvider,
    Capability,
    ConnectionResult,
    Credential,
    CredentialMissing,
    ModelCatalog,
    ModelInfo,
    ModelProvider,
    ProviderCapability,
    ProviderEntry,
    ProviderResolutionError,
    get_default_registry,
)

ANTHROPIC_CAPABILITY = ProviderCapability(
    type="anthropic",
    capabilities=frozenset(
        {
            Capability.CHAT,
            Capability.CODE_TOOLS,
            Capability.STREAMING,
            Capability.VISION,
        }
    ),
    supports_streaming=True,
    supports_tools=True,
    supports_embeddings=False,
    supports_vision=True,
    max_context_tokens=0,  # model-dependent
    notes="Anthropic Messages API via the anthropic SDK; no embeddings.",
)


def _factory(
    *,
    entry: ProviderEntry,
    session_key: str | None = None,
    **kwargs: object,
) -> ModelProvider:
    """Construct an :class:`AnthropicProvider` from a :class:`ProviderEntry` (registry
    contract). Stateless — ``session_key`` ignored."""
    del session_key

    cred: Credential | None = None
    if entry.credential:
        store = kwargs.get("credential_store")
        if store is None:
            raise CredentialMissing(
                f"Anthropic provider entry {entry.name!r} declares credential "
                f"{entry.credential!r} but no credential_store was passed to build()"
            )
        cred = store.resolve(entry.credential)  # type: ignore[attr-defined]
        if cred is None or cred.secret is None:
            raise CredentialMissing(f"Anthropic credential {entry.credential!r} is not configured")

    options = dict(entry.options or {})

    # Fallback: inline api_key from options (set by the "Add instance" UI) or env var.
    if cred is None:
        inline_key = options.pop("api_key", "") or os.environ.get("ANTHROPIC_API_KEY", "")
        if inline_key:
            cred = Credential(name="anthropic", kind="api_key", secret=inline_key, source="file")
    base_url_value = options.pop("base_url", None)
    base_url = str(base_url_value) if base_url_value is not None else None
    max_tokens_value = options.pop("max_tokens", 4096)
    max_tokens = int(max_tokens_value) if isinstance(max_tokens_value, int) else 4096

    # A ``model`` kwarg (threaded by ``registry.build(name, model=…)``) overrides the
    # entry's pinned model — a per-use-case caller (e.g. one_shot_completion's
    # reasoning axis) must be able to pin the active model, or it would silently use
    # the entry default.
    _model_override = kwargs.get("model")
    model = str(_model_override) if _model_override else entry.model

    return AnthropicProvider(
        model=model,
        credential=cred,
        base_url=base_url,
        max_tokens=max_tokens,
        extra_options=options,
    )


def create_provider(config: dict[str, Any]) -> AnthropicProvider:
    """Build an :class:`AnthropicProvider` from a model-extension instance config — the
    app-factory path (``provider_bridge`` fallback). API key comes from the instance
    config or the ``ANTHROPIC_API_KEY`` environment variable."""
    api_key = config.get("api_key", "") or os.environ.get("ANTHROPIC_API_KEY", "")
    cred = Credential(name="anthropic", kind="api_key", secret=api_key, source="file")
    return AnthropicProvider(
        # No hardcoded model id (de-hardcode directive). Unpinned → resolve from the
        # curated catalog by family preference (a family preference, not a pinned id).
        model=config.get("model") or config.get("default_model") or _pick_default_model(),
        credential=cred,
        base_url=config.get("endpoint") or None,
    )


# ── Catalog (discovery + connectivity) ────────────────────────────────────
#
# The Anthropic Messages API exposes no models-list endpoint, so the catalog is a
# curated list of the current Claude models (this used to be hardcoded in the core
# discovery handler; it belongs with the provider it describes). Bring-your-own key
# via config or the ANTHROPIC_API_KEY env; connectivity is reported from whether a
# key is present (there is no cheap unauthenticated probe).

# Curated Claude catalog. The Messages API has no models-list endpoint, so — per the
# de-hardcode directive — this is the one place a model list is allowed to be
# hardcoded, and it is sourced by INTERNET SEARCH of the current Anthropic model
# docs (platform.claude.com/docs/en/docs/about-claude/models/overview), not from
# memory. Refreshed 2026-07-06. Current family first so the picker surfaces today's
# models and _pick_default_model() resolves the newest per family; still-available
# legacy ids follow for accounts pinned to them. All current + Claude-4 models
# support text + image input (vision) per the docs' capability note.
#
# Excluded deliberately: claude-mythos-5 / claude-mythos-preview (invitation-only
# Project Glasswing — no self-serve access, so it must not appear in a picker).
_ANTHROPIC_MODELS: list[dict[str, Any]] = [
    # Current models.
    {"id": "claude-fable-5", "capabilities": ["chat", "image_modality"]},
    {"id": "claude-opus-4-8", "capabilities": ["chat", "image_modality"]},
    {"id": "claude-sonnet-5", "capabilities": ["chat", "image_modality"]},
    {"id": "claude-haiku-4-5", "capabilities": ["chat", "image_modality"]},
    # Legacy models — still available; kept for back-compat with pinned accounts.
    {"id": "claude-opus-4-7", "capabilities": ["chat", "image_modality"]},
    {"id": "claude-opus-4-6", "capabilities": ["chat", "image_modality"]},
    {"id": "claude-sonnet-4-6", "capabilities": ["chat", "image_modality"]},
    {"id": "claude-sonnet-4-5", "capabilities": ["chat", "image_modality"]},
    {"id": "claude-opus-4-5", "capabilities": ["chat", "image_modality"]},
    # Deprecated (retires 2026-08-05) but still callable until then.
    {"id": "claude-opus-4-1", "capabilities": ["chat", "image_modality"]},
]

# Family preference for the unpinned default (create_provider fallback). Returns the
# FIRST catalog id matching the earliest-preferred family token — so the default is
# DERIVED from the curated list (no separately-hardcoded default id), and tracks the
# list forward automatically as models are refreshed. Opus leads per the docs'
# "start with Claude Opus 4.8" guidance.
_DEFAULT_MODEL_PREFERENCE = ("opus", "sonnet", "haiku", "fable")


def _pick_default_model() -> str:
    """Resolve the unpinned default model id from the curated catalog by family
    preference. Falls back to the first catalog entry, then "" if the list is empty."""
    ids = [str(m["id"]) for m in _ANTHROPIC_MODELS]
    for family in _DEFAULT_MODEL_PREFERENCE:
        for model_id in ids:
            if family in model_id:
                return model_id
    return ids[0] if ids else ""


class AnthropicCatalog(ModelCatalog):
    """Curated Claude model catalog (the Messages API has no models endpoint)."""

    def __init__(self, api_key: str = "") -> None:
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")

    async def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(id=m["id"], name=m["id"], capabilities=list(m["capabilities"]))
            for m in _ANTHROPIC_MODELS
        ]

    async def test_connection(self) -> ConnectionResult:
        # No unauthenticated health endpoint; report on key presence + the
        # (static) catalog so the UI can distinguish configured vs not.
        if not self._api_key:
            return ConnectionResult(ok=False, detail="No Anthropic API key configured")
        return ConnectionResult(ok=True, model_count=len(_ANTHROPIC_MODELS))


def create_catalog(options: dict[str, Any] | None = None, *, model: str = "") -> AnthropicCatalog:
    """Catalog factory (registry contract) — build discovery from entry options."""
    del model
    return AnthropicCatalog(api_key=str((options or {}).get("api_key") or ""))


# Register the provider type on import (the app loader imports this module).
try:
    get_default_registry().register_type(ANTHROPIC_CAPABILITY, _factory)
except ProviderResolutionError:
    pass  # already registered (idempotent against reload)

# The discovery/connectivity axis (register_catalog is idempotent — last wins).
get_default_registry().register_catalog("anthropic", create_catalog)
