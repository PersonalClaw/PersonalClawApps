"""OpenAI model provider (standalone app).

Speaks the **OpenAI-compatible** inference protocol — the wire client
(``OpenAIProvider``) is a supported standard that lives in core and is exposed via
``personalclaw.sdk.model``. This app owns the provider-specific bits: API-key auth,
config, its capability descriptor, and registration. (An OpenAI-compatible endpoint
with different auth/config — vllm, lmstudio, together, … — is its own app on the same
protocol client.)
"""

from __future__ import annotations

import os
from typing import Any

from personalclaw.sdk.model import (
    Capability,
    ConnectionResult,
    Credential,
    CredentialMissing,
    MediaCatalog,
    MediaModel,
    ModelCatalog,
    ModelInfo,
    ModelProvider,
    OpenAIProvider,
    ProviderCapability,
    ProviderEntry,
    ProviderResolutionError,
    get_default_registry,
    openai_compatible_list_models,
    register_media_catalog,
)

OPENAI_CAPABILITY = ProviderCapability(
    type="openai",
    capabilities=frozenset(
        {
            Capability.CHAT,
            Capability.CODE_TOOLS,
            Capability.STREAMING,
            Capability.EMBEDDING,
            Capability.VISION,
        }
    ),
    supports_streaming=True,
    supports_tools=True,
    supports_embeddings=True,
    supports_vision=True,
    max_context_tokens=0,  # model-dependent
    notes="OpenAI Chat Completions + Embeddings via the openai SDK.",
)


def _factory(
    *,
    entry: ProviderEntry,
    session_key: str | None = None,
    **kwargs: object,
) -> ModelProvider:
    """Construct an :class:`OpenAIProvider` from a :class:`ProviderEntry` (registry
    contract). ``session_key`` is accepted for parity but OpenAI is stateless."""
    del session_key  # unused — OpenAI provider is stateless.

    cred: Credential | None = None
    if entry.credential:
        store = kwargs.get("credential_store")
        if store is None:
            raise CredentialMissing(
                f"OpenAI provider entry {entry.name!r} declares credential "
                f"{entry.credential!r} but no credential_store was passed to build()"
            )
        cred = store.resolve(entry.credential)  # type: ignore[attr-defined]
        if cred is None or cred.secret is None:
            raise CredentialMissing(f"OpenAI credential {entry.credential!r} is not configured")

    options = dict(entry.options or {})

    # Fallback: inline api_key from options (set by the "Add instance" UI) or env var.
    if cred is None:
        inline_key = options.pop("api_key", "") or os.environ.get("OPENAI_API_KEY", "")
        if inline_key:
            cred = Credential(name="openai", kind="api_key", secret=inline_key, source="file")
    base_url_value = options.pop("base_url", None)
    base_url = str(base_url_value) if base_url_value is not None else None
    max_tokens_value = options.pop("max_tokens", None)
    max_tokens = int(max_tokens_value) if isinstance(max_tokens_value, int) else None

    # A ``model`` kwarg (threaded by ``registry.build(name, model=…)``) overrides the
    # entry's pinned model — a per-use-case caller (e.g. one_shot_completion's
    # reasoning axis, which resolves the active model from active_models.json) must
    # be able to pin the model, or it would silently use the entry default.
    _model_override = kwargs.get("model")
    model = str(_model_override) if _model_override else entry.model

    # The embedding use-case binding arrives as a build kwarg — the embedder
    # constructs its provider WITH the bound model (embed() takes no per-call model).
    _emb_model = kwargs.get("embedding_model")
    if _emb_model:
        options["embedding_model"] = str(_emb_model)

    return OpenAIProvider(
        model=model,
        credential=cred,
        base_url=base_url,
        max_tokens=max_tokens,
        extra_options=options,
    )


def create_provider(config: dict[str, Any]) -> OpenAIProvider:
    """Build an :class:`OpenAIProvider` from a model-extension instance config — the
    app-factory path (``provider_bridge`` fallback). API key comes from the instance
    config or the ``OPENAI_API_KEY`` environment variable."""
    api_key = config.get("api_key", "") or os.environ.get("OPENAI_API_KEY", "")
    cred = Credential(name="openai", kind="api_key", secret=api_key, source="file")
    return OpenAIProvider(
        # Empty when unpinned → OpenAIProvider.start() resolves it from live
        # /v1/models discovery (no hardcoded model name — de-hardcode directive).
        model=config.get("model") or config.get("default_model") or "",
        credential=cred,
        base_url=config.get("endpoint") or None,
    )


# ── Catalog (discovery + connectivity) ────────────────────────────────────


class OpenAICatalog(ModelCatalog):
    """Lists models from the OpenAI-compatible ``/v1/models`` endpoint. Config-only
    (endpoint + api_key from the entry options / OPENAI_API_KEY env)."""

    def __init__(self, endpoint: str = "", api_key: str = "") -> None:
        self._endpoint = endpoint or ""
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")

    async def list_models(self) -> list[ModelInfo]:
        return await openai_compatible_list_models(self._endpoint, self._api_key)

    async def test_connection(self) -> ConnectionResult:
        if not self._api_key and not self._endpoint:
            return ConnectionResult(ok=False, detail="No API key or endpoint configured")
        models = await self.list_models()
        if not models:
            return ConnectionResult(ok=False, detail="No models returned (check key/endpoint)")
        return ConnectionResult(ok=True, model_count=len(models))


def create_catalog(options: dict[str, Any] | None = None, *, model: str = "") -> OpenAICatalog:
    """Catalog factory (registry contract) — build discovery from entry options."""
    del model
    opts = options or {}
    return OpenAICatalog(endpoint=str(opts.get("endpoint") or ""), api_key=str(opts.get("api_key") or ""))


# Register the provider type on import (the app loader imports this module, so the
# type + capability are wired into the default registry when the app is installed).
try:
    get_default_registry().register_type(OPENAI_CAPABILITY, _factory)
except ProviderResolutionError:
    pass  # already registered (idempotent against reload)

# The discovery/connectivity axis (register_catalog is idempotent — last wins).
get_default_registry().register_catalog("openai", create_catalog)


# ── OpenAI media-model catalogs (stt / tts / image-gen) ───────────────────────
# The OpenAI-compatible audio/images PROTOCOL clients live in core; this app owns
# OpenAI's VENDOR catalog + unpinned defaults, contributed under the ``openai``
# provider type. Core's remote adapters look these up by type (no hard-coded OpenAI
# model ids / api.openai.com host-sniff in core). A different-vendor openai-compatible
# endpoint (Alibaba, Groq, …) contributes its own (or none → user pins a model).
register_media_catalog(
    "stt", "openai",
    MediaCatalog(
        models=(
            MediaModel(name="whisper-1", description="OpenAI Whisper (transcription)"),
            MediaModel(name="gpt-4o-transcribe", description="OpenAI GPT-4o transcription"),
        ),
        default_model="whisper-1",
    ),
)
register_media_catalog(
    "tts", "openai",
    MediaCatalog(
        models=(
            MediaModel(name="tts-1", description="OpenAI TTS (standard)"),
            MediaModel(name="tts-1-hd", description="OpenAI TTS (HD)"),
            MediaModel(name="gpt-4o-mini-tts", description="OpenAI GPT-4o-mini TTS"),
        ),
        default_model="tts-1",
    ),
)
register_media_catalog(
    "image_gen", "openai",
    MediaCatalog(
        models=(
            MediaModel(name="gpt-image-1", description="OpenAI gpt-image-1 (generation + editing)",
                       extra={"sizes": ["1024x1024", "1536x1024", "1024x1536", "auto"], "supports_edit": True}),
            MediaModel(name="dall-e-3", description="OpenAI DALL-E 3 (generation only)",
                       extra={"sizes": ["1024x1024", "1792x1024", "1024x1792"], "supports_edit": False}),
            MediaModel(name="dall-e-2", description="OpenAI DALL-E 2 (generation + editing)",
                       extra={"sizes": ["256x256", "512x512", "1024x1024"], "supports_edit": True}),
        ),
        default_model="gpt-image-1",
    ),
)
