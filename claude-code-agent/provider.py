"""``acp:claude-code`` bundle — Claude Code as a removable ACP agent provider.

Drives Anthropic's Claude Code through the canonical ACP adapter
``@agentclientprotocol/claude-agent-acp`` (the renamed home of the former
``@zed-industries/claude-code-acp``; built on ``@agentclientprotocol/sdk``, which
speaks newline-delimited JSON — the ``ClaudeCodeDialect`` selects that framing).
This module owns everything Claude-specific so the core ACP layer never names Claude:

* **Binary resolution** — env ``CLAUDE_CODE_ACP_BIN`` → ``claude-agent-acp`` on
  PATH / node-manager dirs → ``npx -y @agentclientprotocol/claude-agent-acp`` (via
  the neutral :func:`personalclaw.acp.cli_resolve.resolve_acp_cli`). The adapter
  delegates the model turn to the Claude Code CLI, which it locates via the
  ``CLAUDE_CODE_EXECUTABLE`` env we resolve here (override → ``which claude``).
* **Dialect** — ``"claude-code"`` (the committed core ``ClaudeCodeDialect``:
  int ``protocolVersion``, model via ``session/set_config_option``, no
  ``set_mode``). Selected EXPLICITLY via ``options["dialect"]`` — never inferred
  from the command basename (an ``npx`` launch would otherwise yield
  ``acp:npx``).
* **Config isolation** (the E12 §6 security control, opt-in via
  ``PERSONALCLAW_CC_ISOLATE=1``) — points ``CLAUDE_CONFIG_DIR`` at a
  PersonalClaw-owned dir and writes a ``0600`` ``settings.json`` that strips
  inherited ``permissions.allow/ask`` and ``defaultMode`` so every Claude tool
  routes back through the host approval gate rather than Claude's own
  auto-approve engine. Fails closed (never overwrites the operator's real
  ``~/.claude``). Off by default so existing local Claude auth works out-of-box.
* **Model catalogue** — a small curated Claude list, Opus 4.8 default.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from personalclaw.sdk.acp import (
    is_npx_fallback,
    provision_acp_adapter,
    resolve_acp_cli,
)
from personalclaw.sdk.acp import register_acp_cli_entry

logger = logging.getLogger(__name__)

# ── identity ──────────────────────────────────────────────────────────────
CLI = "claude-code"
DIALECT = "claude-code"
# The extension/bundle that owns this runtime — the UI joins the readiness row
# back to this enable/config card by name.
EXTENSION = "claude-code-agent"

# Env override + npm package for the ACP adapter binary.
_ACP_BIN_ENV = "CLAUDE_CODE_ACP_BIN"
_ACP_BIN_NAMES = ["claude-agent-acp"]
_ACP_NPM_PKG = "@agentclientprotocol/claude-agent-acp"

# Env override for the underlying Claude Code CLI the adapter shells out to.
_CLAUDE_EXEC_ENV = "CLAUDE_CODE_EXECUTABLE"
_CLAUDE_BIN_NAMES = ["claude"]

# ── model selection ─────────────────────────────────────────────────────────
# No hardcoded model list or default id (de-hardcode directive). The ACP adapter
# advertises the LIVE model set via the ``session/new`` handshake (see
# ``AcpAgentProvider.discover`` → ``result.models``), so the picker is populated
# by real discovery — a static curated list here would only go stale (the class
# of hazard behind the Bedrock default-id bug). When the user pins no model, the
# empty pin flows to core's ``acp/client.DEFAULT_MODEL = "auto"`` sentinel, whose
# dialect guard SKIPS ``session/set_model`` so the Claude CLI uses its OWN current
# default — deferring to the tool rather than pinning a name that ages out.


# ── config isolation (E12 §6 — Claude-only security hardening) ──────────────

# settings.json keys stripped from the seeded isolated config — Claude's own
# permission engine must not silently auto-approve tools; PreToolUse + the host
# gate own that decision. (We strip the whole ``permissions.allow``/``ask`` and
# ``defaultMode`` plus plugin/marketplace bloat.)
_STRIP_TOP_LEVEL_KEYS = (
    "enabledPlugins",
    "extraKnownMarketplaces",
    "enabledMcpjsonServers",
    "disabledMcpjsonServers",
)
_STRIP_PERMISSION_KEYS = ("allow", "ask", "defaultMode")


def _claude_config_root() -> Path:
    """The PersonalClaw-owned isolated ``CLAUDE_CONFIG_DIR``.

    Deterministic + recomputable with zero persisted state (honours
    ``PERSONALCLAW_HOME`` via :func:`config_dir`). An explicit
    ``CLAUDE_CONFIG_DIR`` env override wins so an operator can pin a location.
    """
    override = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if override:
        return Path(override)
    try:
        from personalclaw.sdk.util import config_dir

        base = config_dir()
    except Exception:
        base = Path(os.path.expanduser("~")) / ".personalclaw"
    return base / "cc-config"


def _seed_isolated_config(root: Path) -> None:
    """Seed ``<root>/settings.json`` (``0600``) with permissions stripped.

    Copies the operator's real ``~/.claude/settings.json`` (if any) but removes
    the keys that would let Claude auto-approve tools, then writes ``0600``
    (the file may carry credential-refresh commands). Fails **closed**: if the
    isolated root resolves to the real ``~/.claude`` it skips entirely rather
    than strip-and-overwrite the operator's config.
    """
    import json

    real_home = Path(os.path.expanduser("~")) / ".claude"
    try:
        if root.resolve() == real_home.resolve():
            logger.warning(
                "acp:claude-code: isolated config root resolves to ~/.claude — "
                "skipping seed (fail closed, never overwrite operator config)."
            )
            return
    except Exception:
        # Resolution failed — fail closed, do not risk clobbering real config.
        return

    root.mkdir(parents=True, exist_ok=True)
    settings_path = root / "settings.json"

    # Start from the operator's real settings so creds/models/env carry over,
    # then strip the auto-approve surface.
    base: dict = {}
    real_settings = real_home / "settings.json"
    if real_settings.is_file():
        try:
            loaded = json.loads(real_settings.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                base = loaded
        except Exception:
            logger.debug("acp:claude-code: could not read real settings.json", exc_info=True)

    for key in _STRIP_TOP_LEVEL_KEYS:
        base.pop(key, None)
    perms = base.get("permissions")
    if isinstance(perms, dict):
        for key in _STRIP_PERMISSION_KEYS:
            perms.pop(key, None)
        # Keep only an (optional) deny list — every other tool now prompts.
        base["permissions"] = {k: v for k, v in perms.items() if k == "deny"}

    try:
        from personalclaw.sdk.util import atomic_write

        atomic_write(settings_path, json.dumps(base, indent=2) + "\n")
    except Exception:
        # Fall back to a plain write so isolation still applies if atomic_write
        # is unavailable in some context.
        settings_path.write_text(json.dumps(base, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(settings_path, 0o600)
    except OSError:
        logger.debug("acp:claude-code: chmod 0600 failed on %s", settings_path, exc_info=True)


def _resolve_claude_exec() -> str:
    """Resolve the underlying Claude Code CLI the adapter delegates to.

    Override (``CLAUDE_CODE_EXECUTABLE``) wins; else ``which claude``. Empty
    string when none found — the caller decides whether that is fatal (the
    readiness probe reports ``not_found``; ``_build_env`` simply leaves the var
    unset so the adapter's own native-binary error surfaces rather than guessing
    a bad path).
    """
    claude_exec = os.environ.get(_CLAUDE_EXEC_ENV, "").strip()
    if claude_exec:
        return claude_exec
    for name in _CLAUDE_BIN_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return ""


def _build_env() -> dict[str, str]:
    """Spawn env for the adapter: optional isolated config dir + resolved Claude binary.

    Config isolation is OFF by default (the spawned Claude reuses the operator's
    real ``~/.claude``, so existing Keychain auth works out-of-box). Opt IN to the
    hardened isolated ``CLAUDE_CONFIG_DIR`` via ``PERSONALCLAW_CC_ISOLATE=1``.
    """
    env: dict[str, str] = {}

    # Opt-in hardened isolation (see docstring). Default off → reuse ~/.claude.
    isolate = os.environ.get("PERSONALCLAW_CC_ISOLATE", "0").strip().lower()
    if isolate in ("1", "true", "yes", "on"):
        root = _claude_config_root()
        try:
            _seed_isolated_config(root)
            env["CLAUDE_CONFIG_DIR"] = str(root)
        except Exception:
            logger.warning("acp:claude-code: config isolation seed failed", exc_info=True)

    # Forward the resolved Claude binary so the adapter finds it even on a
    # minimal daemon PATH. If unresolved, leave unset (see _resolve_claude_exec).
    claude_exec = _resolve_claude_exec()
    if claude_exec:
        env[_CLAUDE_EXEC_ENV] = claude_exec

    return env


def resolve_command(*, provision: bool = False) -> list[str] | None:
    """Resolve the ``claude-agent-acp`` launch argv (or ``None`` if unresolved).

    When *provision* is set and the only resolution would be the fragile
    ``npx -y`` fallback, install the adapter under a Node >= 20 into the managed
    prefix and re-resolve to that on-disk binary (see the codex bundle's twin).
    """
    argv = resolve_acp_cli(
        env_var=_ACP_BIN_ENV,
        bin_names=_ACP_BIN_NAMES,
        npm_pkg=_ACP_NPM_PKG,
    )
    if provision and is_npx_fallback(argv):
        if provision_acp_adapter(_ACP_NPM_PKG, _ACP_BIN_NAMES):
            return resolve_acp_cli(
                env_var=_ACP_BIN_ENV,
                bin_names=_ACP_BIN_NAMES,
                npm_pkg=_ACP_NPM_PKG,
            )
    return argv


def login_command() -> list[str]:
    """Suggested sign-in argv for the Sign-in terminal: ``claude /login``.

    Claude self-authenticates via its own ``/login`` flow (OAuth / API key);
    PersonalClaw stores no key. We pre-type the resolved ``claude`` binary so
    the user lands in the auth flow; the terminal is freeform so they can edit
    it (e.g. ``claude setup-token``) for any non-standard method.
    """
    claude_exec = os.environ.get(_CLAUDE_EXEC_ENV, "").strip()
    if not claude_exec:
        for name in _CLAUDE_BIN_NAMES:
            found = shutil.which(name)
            if found:
                claude_exec = found
                break
    return [claude_exec or "claude", "/login"]


def create_provider(config: dict | None = None):
    """Bundle factory — register the ``acp:claude-code`` AgentProvider entry.

    Invoked by the extension system's ``agent``-type handler on enable (with the
    bundle's settings config). Resolves the adapter argv + Claude binary, applies
    config isolation, and publishes the ``acp_agent`` registry entry. Returns
    ``None`` (agents are config/registry-based — same contract as the
    ``native-agents`` bundle); registration is the side effect.
    """
    config = config or {}

    # Optional settings overrides (binary path + default model).
    bin_override = str(config.get("acp_bin", "") or "").strip()
    if bin_override:
        os.environ[_ACP_BIN_ENV] = bin_override
    # No hardcoded default — an unset pin flows through to core's "auto" sentinel
    # (dialect skips set_model → CLI uses its own current default). De-hardcode.
    model = str(config.get("model", "") or "").strip()

    # Provision the adapter under a Node >= 20 when it would otherwise only run
    # via the fragile npx fallback (see resolve_command / the codex twin).
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
        # claude-agent-acp delegates the model turn to the separate Claude Code
        # CLI (CLAUDE_CODE_EXECUTABLE). The adapter handshakes via npx without
        # it, so declare the engine requirement and let the probe report
        # not_found when `claude` is absent rather than a hollow "ready".
        requires_executable={
            "label": _CLAUDE_BIN_NAMES[0],
            "env_var": _CLAUDE_EXEC_ENV,
            "path": _resolve_claude_exec(),
        },
    )
    return None
