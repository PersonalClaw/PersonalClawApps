"""Claude subscription model provider (standalone app) — rides the Claude Code CLI's login.

The reference **subscription-credential** model provider: Anthropic bills a Claude Code
subscription per SEAT, not per token, so there is no API key for the user to paste. They
already ran the vendor's own CLI sign-in on this machine, and that CLI holds a bearer token
in a credential store IT owns. This app declares WHERE that store is and which keys hold
the token; core reads it READ-ONLY at build time and hands the provider a credential.

Nothing else is special. The wire client (``AnthropicProvider``) is core's supported
Anthropic-Messages standard, sessions/models/catalogs flow through the ordinary branded-app
path (``register_branded_app``), and **no agent runtime is involved** — this is a plain
model provider that happens to resolve its credential from a CLI's store instead of a key.

Two declarations do the whole job (see ``personalclaw.sdk.provider_helpers``):

* a :class:`SubscriptionSource` — the store's paths, the key walk to the token, the expiry
  stamp, and the app's OWN login sentence (core never names a vendor's login verb);
* ``BrandedProviderSpec.credential_source`` — naming that source, with **no**
  ``api_key_env``, because this provider has no API key at any layer.

Not signed in is not an error: core's resolver fails soft, and the extensions list greys
the app out with this app's ``login_hint`` — derived from the declared
``credential_source``, so no ``availability()`` hook is written here.

READ-ONLY, always. This app never writes, refreshes, repairs or deletes another tool's
credential store; an expired token is reported as not-signed-in and the user re-runs
``claude login`` themselves. The token is never logged, never copied into app state and
never persisted — it goes straight into the wire client for the call.
"""

from __future__ import annotations

from typing import Any

from personalclaw.sdk.model import (
    BrandedProviderSpec,
    Capability,
    ModelProvider,
    ProviderEntry,
    register_branded_app,
)
from personalclaw.sdk.provider_helpers import (
    SubscriptionSource,
    register_subscription_source,
)

#: The registered source id this app's spec names in ``credential_source``.
CREDENTIAL_SOURCE = "claude-code"

# The Claude Code CLI's own OAuth record. Candidate paths are tried in order, first
# usable token wins; core ``~``-expands and ``$VAR``-expands each one, so an operator who
# relocated the CLI's config with CLAUDE_CONFIG_DIR is covered, and an unset variable
# simply fails to open and falls through to the next candidate.
#
# LIMITATION, stated plainly: core's resolver reads JSON FILE stores only. A macOS install
# where Claude Code keeps its token in the login Keychain and writes no
# ``.credentials.json`` therefore resolves as *not signed in* — the app greys out with the
# hint below rather than pretending. Reading a Keychain item would need a core-side
# resolver that can shell out to ``security``, which is core's business, not an app's.
SOURCE = SubscriptionSource(
    id=CREDENTIAL_SOURCE,
    login_hint="sign in with `claude login` first",
    credential_files=(
        "$CLAUDE_CONFIG_DIR/.credentials.json",
        "~/.claude/.credentials.json",
        "~/.config/claude/.credentials.json",
    ),
    token_path=("claudeAiOauth", "accessToken"),
    expires_at_path=("claudeAiOauth", "expiresAt"),
    expires_at_unit="ms",  # the record stamps expiry in epoch milliseconds
)
register_subscription_source(SOURCE)

# The Anthropic Messages API exposes no models-list route, so the picker is fed from a
# curated list. Per the de-hardcode directive this is NOT a second source of model truth:
# it mirrors the CURRENT rows of the sibling ``anthropic-models`` app's curated catalog
# (anthropic-models/provider.py, sourced from the Anthropic model docs, refreshed
# 2026-07-06) — the same ids the same wire serves. Which of them a given subscription tier
# may call is the vendor's business: an id your plan doesn't include fails at the wire with
# Anthropic's own error, exactly as it would with an API key.
_CURATED_MODELS: tuple[dict[str, Any], ...] = (
    {"id": "claude-fable-5", "capabilities": ["chat", "image_modality"]},
    {"id": "claude-opus-4-8", "capabilities": ["chat", "image_modality"]},
    {"id": "claude-sonnet-5", "capabilities": ["chat", "image_modality"]},
    {"id": "claude-haiku-4-5", "capabilities": ["chat", "image_modality"]},
)

# Family preference for the unpinned default. The default id is DERIVED from the curated
# list (same discipline as anthropic-models' ``_pick_default_model``) so refreshing the
# list moves the default with it and no id is separately hardcoded.
_DEFAULT_MODEL_PREFERENCE = ("opus", "sonnet", "haiku", "fable")


def _pick_default_model() -> str:
    """The unpinned default model id, resolved from the curated list by family."""
    ids = [str(m["id"]) for m in _CURATED_MODELS]
    for family in _DEFAULT_MODEL_PREFERENCE:
        for model_id in ids:
            if family in model_id:
                return model_id
    return ids[0] if ids else ""


SPEC = BrandedProviderSpec(
    type="claude_subscription",
    protocol="anthropic",
    default_base_url="",  # empty → the anthropic SDK's own official base
    api_key_env="",  # deliberately EMPTY: this provider has no API key to fall back to
    default_model=_pick_default_model(),
    max_tokens=4096,  # the Anthropic wire requires a max_tokens
    capabilities=frozenset(
        {Capability.CHAT, Capability.CODE_TOOLS, Capability.STREAMING, Capability.VISION}
    ),
    fallback_models=_CURATED_MODELS,
    credential_source=CREDENTIAL_SOURCE,
    notes=(
        "Claude models over an existing Claude Code subscription sign-in; the CLI's own "
        "credential store is read read-only and no API key is used."
    ),
)

# Registers the provider TYPE + catalog on import (the app loader imports this module).
# The trio's own ``create_provider`` is DISCARDED on purpose — see below — and deleted
# rather than left bound, so no unused alternative build path exists in this module.
_factory, _branded_create_provider, create_catalog = register_branded_app(SPEC)
del _branded_create_provider


def create_provider(config: dict[str, Any] | None = None) -> ModelProvider:
    """Build the provider from an instance config — the manifest's ``implementation``.

    Routed through the registry factory (``_factory``) rather than the branded helper's
    own config-path builder, because THAT builder resolves only ``config["api_key"]`` and
    ``spec.api_key_env`` and would hand this app the anonymous placeholder every time (a
    provider that authenticates with nothing). ``_factory`` is where the full credential
    order lives — entry credential → options api_key → **subscription source** →
    ``api_key_env`` → placeholder — so the config path and the registry path resolve the
    CLI's token identically, and neither can be configured with a separate API key
    (there is no ``api_key`` setting on this app, and its ``api_key_env`` is empty).
    """
    cfg = dict(config or {})
    options: dict[str, object] = {}
    endpoint = str(cfg.get("endpoint") or cfg.get("base_url") or "")
    if endpoint:
        options["endpoint"] = endpoint
    max_tokens = cfg.get("max_tokens")
    if isinstance(max_tokens, int) and not isinstance(max_tokens, bool):
        options["max_tokens"] = max_tokens
    return _factory(
        entry=ProviderEntry(
            name=SPEC.type,
            type=SPEC.type,
            model=str(cfg.get("model") or cfg.get("default_model") or SPEC.default_model),
            options=options,
            declared_capabilities=SPEC.capabilities,
        )
    )
