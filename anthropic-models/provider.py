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
    PromptCache,
    ProviderCapability,
    ProviderEntry,
    ProviderResolutionError,
    get_default_registry,
)
from personalclaw.sdk.net import CONNECTOR, egress_policy_for, fetch

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
    # EXPLICIT - this app's provider IS core's AnthropicProvider, which declares EXPLICIT
    # (llm/anthropic.py) and translates the neutral marker into `cache_control` on the
    # hinted content block. Mirrored here so the declarative capability matches the
    # instance the factory actually returns.
    prompt_cache=PromptCache.EXPLICIT,
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
# The picker's model list is CURATED (the Messages API's own list endpoint is not what
# the picker offers — see the catalog comment below); connectivity is a separate axis
# and is MEASURED against ``GET /v1/models``, an authenticated route.
#
# 🪤 It used to be inferred from key PRESENCE, and that made the one answer the
# "Test connection" button exists to give unavailable. Measured on a fresh container
# install against the real api.anthropic.com: with `api_key` set to
# `sk-ant-deliberately-invalid-000`, Settings → Providers → Test connection rendered
# "Connected — 10 model(s) available", first-run setup marked the model provider
# "Ready" and advanced — while the very same gateway had already logged
# `anthropic.AuthenticationError: Error code: 401 … 'API key is invalid.'` from the
# first real turn. Every layer reported success; only the vendor disagreed.
#
# The repo's own precedent is ``openrouter-models``, whose ``test_connection``
# docstring states the rule: probe an AUTHENTICATED route, because validating a key
# by listing models "would therefore report 'connected' for a typo'd key — the one
# answer the Settings → 'Test connection' button exists to prevent."
_API_KEY_ENV = "ANTHROPIC_API_KEY"
_DEFAULT_ENDPOINT = "https://api.anthropic.com"
#: Anthropic's own SDK issues `GET /v1/models` (`anthropic.resources.models.list` →
#: `_get_api_list("/v1/models", …)`), which is how this route is sourced rather than
#: from memory. It is authenticated and free, so the probe costs nothing.
_MODELS_PATH = "/v1/models"
#: Pinned by the vendor's SDK default; an Anthropic-compatible endpoint that speaks
#: the Messages API accepts the same header.
_API_VERSION = "2023-06-01"

# Curated Claude catalog. The list is CURATED rather than discovered because the
# exclusion below is a product decision an endpoint cannot make for us — so per the
# de-hardcode directive this is the one place a model list is allowed to be hardcoded,
# and it is sourced by INTERNET SEARCH of the current Anthropic model docs
# (platform.claude.com/docs/en/docs/about-claude/models/overview), not from memory.
#
# 🪤 This comment used to justify the hardcoding with "the Messages API has no
# models-list endpoint", and that premise was false — `GET /v1/models` exists (it is
# what the vendor's own SDK calls). Believing it is what left `test_connection` with
# nothing to probe, so the false premise cost a real defect; the curation is the true
# reason and is the one recorded here.
#
# Refreshed 2026-07-06. Current family first so the picker surfaces today's
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


def _probe_headers(key: str) -> dict[str, str]:
    return {"x-api-key": key, "anthropic-version": _API_VERSION}


def _status_message(status: int) -> str:
    """Map the probe's HTTP status to a message that names the next action.

    Keyed on the STATUS CODE only, never the body: a compatible proxy in front of the
    Messages API is free to word its own envelope however it likes, and a
    body-matching implementation would relay that wording as if Anthropic had said it.
    """
    if status in (401, 403):
        return (
            "Anthropic rejected the API key. Check the key in Settings → Providers, "
            f"or {_API_KEY_ENV}."
        )
    if status == 429:
        return "Anthropic rate-limited the key check — the key may be valid; retry shortly."
    if status == 404:
        return (
            "The endpoint answered 404 for the model list, so it is not an "
            "Anthropic-compatible Messages API. Check the Base URL in Settings → Providers."
        )
    if status >= 500:
        return f"Anthropic returned {status} for the key check — retry shortly."
    return f"The key check returned HTTP {status}."


class AnthropicCatalog(ModelCatalog):
    """Curated Claude model catalog, with a MEASURED connectivity axis.

    ``list_models`` is the curated picker list; ``test_connection`` makes a real
    authenticated request, so a key the vendor rejects is reported as rejected.
    """

    def __init__(self, api_key: str = "", endpoint: str = "") -> None:
        self._api_key = api_key or os.environ.get(_API_KEY_ENV, "")
        # The settings schema exposes a Base URL, and `create_provider` honours it — so
        # the probe has to hit the endpoint the provider will actually call. Ignoring it
        # tested api.anthropic.com and told the user their compatible proxy was fine.
        self._endpoint = (endpoint or _DEFAULT_ENDPOINT).rstrip("/")

    async def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(id=m["id"], name=m["id"], capabilities=list(m["capabilities"]))
            for m in _ANTHROPIC_MODELS
        ]

    async def test_connection(self) -> ConnectionResult:
        if not self._api_key:
            return ConnectionResult(
                ok=False, detail=f"No Anthropic API key configured (set it or {_API_KEY_ENV})"
            )
        try:
            # The probe goes through the SDK's guarded `fetch`, so the operator's egress
            # config applies. Note the asymmetry: the chat path is the vendor SDK's own
            # client and is NOT egress-policed, so a LAN-hosted compatible endpoint can
            # chat while this probe is denied (CONNECTOR is public-hosts-only). That is
            # the same posture `openrouter-models` runs, and `allow_hosts` is the
            # documented opt-in — the refusal names the endpoint so it is actionable.
            resp = await fetch(
                f"{self._endpoint}{_MODELS_PATH}",
                policy=egress_policy_for(CONNECTOR),
                method="GET",
                headers=_probe_headers(self._api_key),
            )
        except Exception as exc:  # noqa: BLE001 — unreachable endpoint / blocked egress
            return ConnectionResult(
                ok=False, detail=f"Could not reach {self._endpoint}: {str(exc)[:200]}"
            )
        if resp.status != 200:
            return ConnectionResult(ok=False, detail=_status_message(resp.status))
        # The key is real. The count reported is the CURATED list, because that is what
        # the picker offers — reporting the endpoint's own count would name models this
        # app will not surface.
        return ConnectionResult(ok=True, model_count=len(_ANTHROPIC_MODELS))


def create_catalog(options: dict[str, Any] | None = None, *, model: str = "") -> AnthropicCatalog:
    """Catalog factory (registry contract) — build discovery from entry options."""
    del model
    opts = options or {}
    return AnthropicCatalog(
        api_key=str(opts.get("api_key") or ""),
        endpoint=str(opts.get("endpoint") or opts.get("base_url") or ""),
    )


# Register the provider type on import (the app loader imports this module).
try:
    get_default_registry().register_type(ANTHROPIC_CAPABILITY, _factory)
except ProviderResolutionError:
    pass  # already registered (idempotent against reload)

# The discovery/connectivity axis (register_catalog is idempotent — last wins).
get_default_registry().register_catalog("anthropic", create_catalog)
