"""``acp:codex`` bundle — OpenAI Codex as a removable ACP agent provider.

Drives the OpenAI Codex CLI through the ACP adapter
``@agentclientprotocol/codex-acp`` (same ACP JSON-RPC protocol as Claude Code's
adapter — both share the core ``ZedAdapterDialect`` shape: int
``protocolVersion``, model via ``session/set_config_option``, no ``set_mode``).

Codex manages its own configuration and auth, so this bundle does **not** apply
the ``CLAUDE_CONFIG_DIR`` isolation the Claude bundle needs — it just resolves
the adapter binary, selects the ``codex`` dialect, and registers the provider.
The host PreToolUse deny gate + four-tier approval still apply (every Codex tool
arrives as an ACP ``session/request_permission`` routed through the same gate),
so no Codex-specific permission hardening is required here.

When the Codex binary cannot be resolved the provider registers nothing and
probes as unavailable — the correct behaviour for an unconfigured CLI.
"""

from __future__ import annotations

import logging
import os
import shutil

from personalclaw.sdk.acp import (
    is_npx_fallback,
    node_manager_bin_globs,
    provision_acp_adapter,
    resolve_acp_cli,
)
from personalclaw.sdk.acp import register_acp_cli_entry

logger = logging.getLogger(__name__)

# ── identity ──────────────────────────────────────────────────────────────
CLI = "codex"
DIALECT = "codex"
# The extension/bundle that owns this runtime (UI join key — see claude_code).
EXTENSION = "codex-agent"

_ACP_BIN_ENV = "CODEX_ACP_BIN"
_ACP_BIN_NAMES = ["codex-acp"]
_ACP_NPM_PKG = "@agentclientprotocol/codex-acp"

# The codex-acp adapter drives an EXTERNAL Codex CLI: it spawns
# ``<CODEX_PATH ?? "codex"> app-server`` and speaks that codex's own app-server
# JSON-RPC — so it inherits whatever auth that codex uses (OpenAI, ChatGPT, or
# cloud-managed creds — no assumption). If it can't find one it
# falls back to a BUNDLED public ``@openai/codex`` that requires OpenAI auth and
# fails ``initialize`` with "Authentication required". On a daemon with a minimal
# PATH a node-manager/npm-global codex is invisible, so we resolve the host codex
# here and forward it as ``CODEX_PATH`` — the exact env var the adapter reads (NOT
# ``CODEX_EXECUTABLE``, which it ignores). Override → which → node-manager globs.
_CODEX_PATH_ENV = "CODEX_PATH"
_CODEX_BIN_NAMES = ["codex"]

# ── model selection ─────────────────────────────────────────────────────────
# No hardcoded model list or default id (de-hardcode directive). The codex-acp
# adapter advertises the LIVE model set via the ``session/new`` handshake, so the
# picker is populated by real discovery — a static curated list here would only go
# stale. When the user pins no model, the empty pin flows to core's
# ``acp/client.DEFAULT_MODEL = "auto"`` sentinel, whose dialect guard SKIPS
# ``session/set_model`` so the Codex CLI uses its OWN current default.


def resolve_command(*, provision: bool = False) -> list[str] | None:
    """Resolve the ``codex-acp`` launch argv (or ``None`` if unresolved).

    When *provision* is set and the only resolution would be the fragile
    ``npx -y`` fallback (the adapter isn't installed anywhere), install the
    adapter under a Node ≥20 interpreter into the managed prefix and re-resolve
    to that on-disk binary — turning "fetch-and-run every spawn via npx" into a
    durable, version-safe install. Falls back to the npx argv if provisioning
    can't run (e.g. no Node ≥20), preserving today's best-effort behavior.
    """
    argv = resolve_acp_cli(
        env_var=_ACP_BIN_ENV,
        bin_names=_ACP_BIN_NAMES,
        npm_pkg=_ACP_NPM_PKG,
    )
    if provision and is_npx_fallback(argv):
        provisioned = provision_acp_adapter(_ACP_NPM_PKG, _ACP_BIN_NAMES)
        if provisioned:
            # Re-resolve so the managed-prefix binary (now on the search path)
            # is picked instead of npx.
            return resolve_acp_cli(
                env_var=_ACP_BIN_ENV,
                bin_names=_ACP_BIN_NAMES,
                npm_pkg=_ACP_NPM_PKG,
            )
    return argv


def _resolve_codex_exec() -> str:
    """Resolve the underlying Codex CLI the adapter delegates to (override →
    PATH → node-manager bin dirs). Empty string when none found."""
    override = os.environ.get(_CODEX_PATH_ENV, "").strip()
    if override:
        return override
    from glob import glob
    from pathlib import Path

    for name in _CODEX_BIN_NAMES:
        found = shutil.which(name)
        if found:
            return found
    for name in _CODEX_BIN_NAMES:
        for pattern in node_manager_bin_globs():
            for hit in sorted(glob(str(Path(pattern) / name))):
                if not os.path.isdir(hit) and os.access(hit, os.X_OK):
                    return hit
    return ""


def _build_env() -> dict[str, str]:
    """Spawn env for the codex-acp adapter: forward the resolved host Codex CLI as
    ``CODEX_PATH`` so the adapter drives IT (inheriting its auth) rather than
    falling back to its bundled OpenAI-auth codex — even on a minimal daemon
    PATH where bare ``codex`` isn't resolvable."""
    env: dict[str, str] = {}
    codex_exec = _resolve_codex_exec()
    if codex_exec:
        env[_CODEX_PATH_ENV] = codex_exec
    return env


def login_command() -> list[str]:
    """Suggested sign-in argv for the Sign-in terminal: ``codex login``.

    Codex manages its own auth; ``codex login`` runs its interactive sign-in.
    The terminal is freeform so the user can substitute any variant.
    """
    codex_exec = _resolve_codex_exec()
    return [codex_exec or "codex", "login"]


def create_provider(config: dict | None = None):
    """Bundle factory — register the ``acp:codex`` AgentProvider entry.

    Returns ``None`` (agents are config/registry-based — same contract as the
    ``native-agents`` bundle); registration is the side effect.
    """
    config = config or {}

    bin_override = str(config.get("acp_bin", "") or "").strip()
    if bin_override:
        os.environ[_ACP_BIN_ENV] = bin_override
    # No hardcoded default — an unset pin flows through to core's "auto" sentinel
    # (dialect skips set_model → CLI uses its own current default). De-hardcode.
    model = str(config.get("model", "") or "").strip()

    # Provision the codex-acp adapter on first registration when it isn't
    # installed anywhere (would otherwise run via the fragile npx fallback):
    # install it under a Node >= 20 into the managed prefix so every spawn uses a
    # durable, version-safe on-disk binary. Best-effort — falls back to npx if a
    # new-enough Node isn't available.
    command = resolve_command(provision=True)
    env = _build_env() if command else {}
    register_acp_cli_entry(
        cli=CLI,
        dialect=DIALECT,
        command=command,
        model=model,
        env=env,
        extension=EXTENSION,
        login_command=login_command(),
        # codex-acp is a thin protocol shim — the actual model turn is delegated
        # to the separate Codex CLI. The ACP initialize succeeds without it
        # (npx fetches the adapter), so declare the engine requirement and let
        # the vendor-neutral probe report not_found when it is absent rather
        # than a hollow "ready" that fails on the first prompt.
        requires_executable={
            "label": _CODEX_BIN_NAMES[0],
            "env_var": _CODEX_PATH_ENV,
            "path": _resolve_codex_exec(),
        },
    )
    return None
