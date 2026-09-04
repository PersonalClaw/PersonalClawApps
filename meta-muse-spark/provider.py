"""Meta Muse Spark model provider (standalone app).

Uses the Meta AI API which is OpenAI-compatible. Base URL: https://api.meta.ai/v1
Model: muse-spark-1.1 (text/image/pdf/video input, text output, 1M context).

Since the API is OpenAI-compatible, this provider reuses the OpenAI SDK client
with a custom base_url pointed at Meta's endpoint.
"""

from __future__ import annotations

import os
from typing import Any

from personalclaw.sdk.model import (
    Capability,
    ConnectionResult,
    Credential,
    CredentialMissing,
    ModelCatalog,
    ModelInfo,
    ModelProvider,
    OpenAIProvider,
    openai_compatible_list_models,
    PromptCache,
    ProviderCapability,
    ProviderEntry,
    ProviderResolutionError,
    get_default_registry,
)

META_BASE_URL = "https://api.meta.ai/v1"

META_CAPABILITY = ProviderCapability(
    type="meta_muse_spark",
    capabilities=frozenset(
        {
            Capability.CHAT,
            Capability.STREAMING,
            Capability.VISION,
        }
    ),
    supports_streaming=True,
    supports_tools=True,
    supports_embeddings=False,
    supports_vision=True,
    max_context_tokens=1_048_576,
    # NONE - no published prompt-caching behaviour for this endpoint that the app can
    # cite, so nothing substantiates AUTOMATIC. NONE is the honest default.
    prompt_cache=PromptCache.NONE,
    notes="Meta Muse Spark via the Meta AI API (OpenAI-compatible); 1M context.",
)

# Static catalog — Meta currently offers one model.
_MODELS = [
    ModelInfo(
        id="muse-spark-1.1",
        name="muse-spark-1.1",
        capabilities=["chat", "image_modality", "streaming"],
    ),
]


def _factory(
    *,
    entry: ProviderEntry,
    session_key: str | None = None,
    **kwargs: object,
) -> ModelProvider:
    """Construct an OpenAIProvider pointed at Meta's endpoint."""
    del session_key

    cred: Credential | None = None
    if entry.credential:
        store = kwargs.get("credential_store")
        if store is None:
            raise CredentialMissing(
                f"Meta provider entry {entry.name!r} declares credential "
                f"{entry.credential!r} but no credential_store was passed to build()"
            )
        cred = store.resolve(entry.credential)
        if cred is None or cred.secret is None:
            raise CredentialMissing(f"Meta credential {entry.credential!r} is not configured")

    options = dict(entry.options or {})

    # Fallback: inline api_key from options or env var.
    if cred is None:
        inline_key = options.pop("api_key", "") or os.environ.get("META_MODEL_API_KEY", "")
        if inline_key:
            cred = Credential(name="meta", kind="api_key", secret=inline_key, source="file")

    base_url = str(options.pop("base_url", META_BASE_URL))

    _model_override = kwargs.get("model")
    model = str(_model_override) if _model_override else (entry.model or "muse-spark-1.1")

    return OpenAIProvider(
        model=model,
        credential=cred,
        base_url=base_url,
        max_tokens=None,
        extra_options=options,
    )


def create_provider(config: dict[str, Any]) -> "OpenAIProvider":
    """Build from a multi-instance config dict (the app-factory path)."""
    api_key = config.get("api_key", "") or os.environ.get("META_MODEL_API_KEY", "")
    cred = Credential(name="meta", kind="api_key", secret=api_key, source="file")
    return OpenAIProvider(
        model=config.get("model") or config.get("default_model") or "muse-spark-1.1",
        credential=cred,
        base_url=config.get("endpoint") or META_BASE_URL,
        max_tokens=None,
        extra_options={},
    )


def create_catalog(options: dict[str, Any] | None = None, *, model: str = "") -> "MuseSparkCatalog":
    """Catalog factory (registry contract) — build discovery from entry options."""
    del model
    opts = options or {}
    return MuseSparkCatalog(api_key=str(opts.get("api_key") or ""))


class MuseSparkCatalog(ModelCatalog):
    """Static catalog (Meta currently offers one model) + a connectivity probe.

    ``list_models`` never touches the network — it runs on hot Settings GETs.
    ``test_connection`` probes ``GET {base}/models`` through the ``net.fetch``
    egress chokepoint, like every other outbound discovery call.
    """

    def __init__(self, api_key: str = "") -> None:
        self._api_key = api_key or os.environ.get("META_MODEL_API_KEY", "")

    async def list_models(self) -> list[ModelInfo]:
        return list(_MODELS)

    async def test_connection(self) -> ConnectionResult:
        if not self._api_key:
            return ConnectionResult(ok=False, detail="No API key configured")
        live = await openai_compatible_list_models(
            META_BASE_URL, self._api_key, default_base=META_BASE_URL
        )
        if not live:
            return ConnectionResult(ok=False, detail="No models returned (check key)")
        return ConnectionResult(ok=True, model_count=len(live))


# Register on import.
try:
    get_default_registry().register_type(META_CAPABILITY, _factory)
except ProviderResolutionError:
    pass

get_default_registry().register_catalog("meta_muse_spark", create_catalog)
