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


def create_catalog() -> ModelCatalog:
    """Return the static Meta model catalog."""
    return ModelCatalog(models=list(_MODELS))


# Register on import.
try:
    get_default_registry().register_type(META_CAPABILITY, _factory)
except ProviderResolutionError:
    pass

get_default_registry().register_catalog("meta_muse_spark", create_catalog)


def test_connection(config: dict[str, Any]) -> ConnectionResult:
    """Test connectivity to the Meta AI API."""
    api_key = config.get("api_key", "") or os.environ.get("META_MODEL_API_KEY", "")
    if not api_key:
        return ConnectionResult(ok=False, status="error", message="No API key configured")
    try:
        import httpx
        resp = httpx.get(
            f"{META_BASE_URL}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            models = data.get("data", [])
            return ConnectionResult(
                ok=True, status="connected",
                message=f"Connected — {len(models)} model(s) available",
            )
        return ConnectionResult(
            ok=False, status="error",
            message=f"API returned {resp.status_code}: {resp.text[:100]}",
        )
    except Exception as exc:
        return ConnectionResult(ok=False, status="error", message=str(exc)[:200])
