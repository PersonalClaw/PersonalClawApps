"""vLLM provider — OpenAI-compatible local inference server.

vLLM exposes an OpenAI Chat Completions / Embeddings-compatible HTTP
surface, so this provider is implemented as a thin subclass of
:class:`personalclaw.providers.openai.OpenAIProvider` that simply requires
a ``base_url`` pointing at the local vLLM server. All streaming, tool,
and embedding behavior is inherited unchanged.

The ``openai`` SDK is imported lazily inside the inherited
:meth:`OpenAIProvider.__init__` (Provider SDK Lazy Import). Importing this
module MUST NOT pull ``openai`` into ``sys.modules``. Only constructing a
:class:`VLLMProvider` instance triggers the SDK import.

A factory is registered with the default :class:`ProviderRegistry` on module
import — the app loader imports this module when the app is enabled, which is
what wires the ``vllm`` provider type into the registry (without the SDK side
effect).
"""

import logging

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
    openai_compatible_list_models,
)

logger = logging.getLogger(__name__)

# vLLM deployments are commonly unauth'd. The upstream OpenAI client
# constructor still requires a populated ``api_key``; passing this
# placeholder keeps construction working when no credential is configured.
# The vLLM server ignores the API key in unauth'd mode.
_PLACEHOLDER_API_KEY = "unused"


class VLLMProvider(OpenAIProvider):
    """ModelProvider for an OpenAI-compatible vLLM server.

    Subclasses :class:`OpenAIProvider` and inherits ``start``,
    ``shutdown``, ``stream``, ``complete``, ``embed``, ``approve_tool``,
    ``reject_tool``, ``context_usage_pct``, and ``cancel`` unchanged — along
    with ``supports_tools=True`` — because vLLM speaks the same OpenAI wire
    protocol. The only divergence is that ``base_url`` is required (vLLM is
    always a custom endpoint) and ``credential`` is optional (vLLM is
    typically unauth'd).
    """

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        credential: Credential | None = None,
        max_tokens: int | None = None,
        extra_options: dict[str, object] | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("VLLMProvider requires a base_url")

        # vLLM servers commonly run unauth'd. Synthesize a placeholder
        # credential so the upstream OpenAI client construction (which
        # requires a populated secret) succeeds. The placeholder is
        # ignored by the vLLM server.
        cred = credential
        if cred is None or not cred.secret:
            cred = Credential(
                name="vllm-anon",
                kind="none",
                secret=_PLACEHOLDER_API_KEY,
                source="none",
            )

        super().__init__(
            model=model,
            credential=cred,
            base_url=base_url,
            max_tokens=max_tokens,
            extra_options=extra_options,
        )


# ── Capability descriptor ────────────────────────────────────────────────
#
# Mirrors :data:`personalclaw.providers.openai.OPENAI_CAPABILITY` because vLLM
# speaks the same wire protocol. Per design § A.5 individual capabilities
# (tools, embeddings, vision) are model-dependent on a vLLM server, so
# concrete Provider_Entry rows should declare only the subset their
# deployment actually supports — the descriptor here is the maximum
# envelope the type can offer.
VLLM_CAPABILITY = ProviderCapability(
    type="vllm",
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
    notes=(
        "vLLM OpenAI-compatible server; capabilities are model-dependent — "
        "entries should declare only what the deployment supports."
    ),
)


# ── Factory ──────────────────────────────────────────────────────────────


def _factory(
    *,
    entry: ProviderEntry,
    session_key: str | None = None,
    **kwargs: object,
) -> ModelProvider:
    """Construct a :class:`VLLMProvider` from a :class:`ProviderEntry`.

    ``session_key`` is accepted for parity with the registry contract but
    vLLM is stateless — we ignore it.

    Credential resolution mirrors :func:`personalclaw.providers.openai._factory`
    but with one difference: vLLM deployments may be unauth'd, so an entry
    without a declared credential is allowed and produces ``cred=None``.
    When the entry DOES declare a credential, the same strictness applies
    as for OpenAI — a missing store or unset secret is a config bug and
    raises :class:`CredentialMissing`.

    ``base_url`` is required and read from ``entry.options.base_url``. A
    missing or empty value raises :class:`ProviderResolutionError`.
    """
    del session_key  # unused — vLLM provider is stateless.

    cred: Credential | None = None
    if entry.credential:
        store = kwargs.get("credential_store")
        if store is None:
            raise CredentialMissing(
                f"vllm provider entry {entry.name!r} declares credential "
                f"{entry.credential!r} but no credential_store was passed to build()"
            )
        cred = store.resolve(entry.credential)  # type: ignore[attr-defined]
        if cred is None or cred.secret is None:
            raise CredentialMissing(f"vllm credential {entry.credential!r} is not configured")

    options = dict(entry.options or {})
    # Accept BOTH keys: the Add-instance flow persists the URL under ``endpoint``
    # (the settingsSchema field), while callers may also pass ``base_url``. Pop
    # both unconditionally so whichever is set wins and neither leaks into
    # extra_options → the SDK client ("unexpected keyword argument"). base_url
    # wins if both are present. Without accepting ``endpoint``, a UI-configured
    # instance failed to build and silently fell back to the default provider.
    _base = options.pop("base_url", None)
    _endpoint = options.pop("endpoint", None)
    base_url_value = _base or _endpoint
    if not base_url_value:
        raise ProviderResolutionError(
            f"vllm provider entry {entry.name!r} requires options.base_url (or options.endpoint)"
        )
    base_url = str(base_url_value)

    max_tokens_value = options.pop("max_tokens", None)
    max_tokens = int(max_tokens_value) if isinstance(max_tokens_value, int) else None

    # Pop ``default_model`` (the settingsSchema field the Add-instance flow persists)
    # so it (a) serves as the model fallback and (b) does NOT leak into extra_options
    # → the openai SDK create() ("unexpected keyword argument 'default_model'").
    _default_model = options.pop("default_model", None)

    # A ``model`` kwarg (threaded by ``registry.build(name, model=…)``) overrides the
    # entry's pinned model — a per-use-case caller (e.g. one_shot_completion's
    # reasoning axis) must be able to pin the active model, or it would silently use
    # the entry default. Fall back to the entry's pinned model, then the configured
    # default_model.
    _model_override = kwargs.get("model")
    model = str(_model_override or entry.model or _default_model or "")

    # The embedding use-case binding arrives as a build kwarg — the embedder
    # constructs its provider WITH the bound model (embed() takes no per-call model).
    _emb_model = kwargs.get("embedding_model")
    if _emb_model:
        options["embedding_model"] = str(_emb_model)

    return VLLMProvider(
        model=model,
        credential=cred,
        base_url=base_url,
        max_tokens=max_tokens,
        extra_options=options,
    )


def create_provider(config: dict) -> "VLLMProvider":
    """Build a VLLMProvider from a model-extension instance config (the provider_bridge
    fallback path). vLLM needs a base_url (the local server); auth is optional."""
    base_url = config.get("endpoint") or config.get("base_url") or "http://localhost:8000"
    return VLLMProvider(
        model=config.get("model") or config.get("default_model") or "",
        base_url=base_url,
    )


# ── Registration ─────────────────────────────────────────────────────────
#
# Register on import — the app loader imports this module when the app is
# enabled, wiring the type into the default registry. The ``try``/``except``
# makes the registration idempotent against module reload in tests; the
# registry itself remains strict and rejects duplicate types in normal use.
# ── Catalog (discovery + connectivity) ────────────────────────────────────


class VLLMCatalog(ModelCatalog):
    """Lists models from a vLLM server's OpenAI-compatible ``/v1/models`` endpoint.
    vLLM is a local server (endpoint required, auth typically absent)."""

    def __init__(self, endpoint: str = "http://localhost:8000", api_key: str = "") -> None:
        self._endpoint = endpoint or "http://localhost:8000"
        self._api_key = api_key or ""

    async def list_models(self) -> list[ModelInfo]:
        return await openai_compatible_list_models(self._endpoint, self._api_key)

    async def test_connection(self) -> ConnectionResult:
        models = await self.list_models()
        if not models:
            return ConnectionResult(ok=False, detail=f"vLLM server unreachable or serving no models at {self._endpoint}")
        return ConnectionResult(ok=True, model_count=len(models))


def create_catalog(options: dict | None = None, *, model: str = "") -> VLLMCatalog:
    """Catalog factory (registry contract) — build discovery from entry options."""
    del model
    opts = options or {}
    endpoint = str(opts.get("endpoint") or opts.get("base_url") or "http://localhost:8000")
    return VLLMCatalog(endpoint=endpoint, api_key=str(opts.get("api_key") or ""))


try:
    get_default_registry().register_type(VLLM_CAPABILITY, _factory)
except ProviderResolutionError:
    logger.debug("vllm provider type already registered with default registry")

# The discovery/connectivity axis (register_catalog is idempotent — last wins).
get_default_registry().register_catalog("vllm", create_catalog)
