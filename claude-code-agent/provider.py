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
* **Config isolation** (the E12 §6 security control, ON unless the
  ``isolated_config`` setting turns it off) — points ``CLAUDE_CONFIG_DIR`` at
  ``<PersonalClaw home>/cc-config``, which starts EMPTY: nothing is copied from
  the operator's ``~/.claude`` or from a ``CLAUDE_CONFIG_DIR`` they set, so no
  inherited ``permissions.allow``/``ask``/``defaultMode`` can auto-approve a tool
  and every Claude tool routes back through the host approval gate. Claude signs
  in once for that config (the Sign-in command targets it).
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


# ── what picks the provider ─────────────────────────────────────────────────
#: The variables Claude Code reads to run on Amazon Bedrock or Google Vertex AI, in which region
#: and with which models. An agent CLI gets no variable of the gateway's it is not handed, so
#: these are declared by name and core passes each one set in the gateway's environment. None of
#: them is a credential: Claude Code takes its keys from its own sign-in, or from the AWS or
#: Google credential files its profile names, and core refuses a credential-shaped name anyway.
PROVIDER_ENV: tuple[str, ...] = (
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "AWS_PROFILE",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL_AWS_REGION",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLOUD_ML_REGION",
    "ANTHROPIC_VERTEX_PROJECT_ID",
)


# ── config isolation (E12 §6 — Claude-only security hardening) ──────────────
#
# The spawned Claude gets a config root of its own that starts EMPTY. It used to be seeded from
# the operator's real ``~/.claude/settings.json`` with the auto-approve keys stripped, and only
# when ``PERSONALCLAW_CC_ISOLATE=1`` was set: off by default, the spawned Claude kept every
# ``permissions.allow`` rule the operator had, while the app said it was isolated. And a
# ``CLAUDE_CONFIG_DIR`` the operator had set became the "isolated" root and was rewritten.

#: The setting that turns isolation off (``settingsSchema.isolated_config``).
_ISOLATED_SETTING = "isolated_config"


def _claude_config_root() -> Path:
    """The PersonalClaw-owned ``CLAUDE_CONFIG_DIR`` the spawned Claude runs with.

    Always under the PersonalClaw home, never a ``CLAUDE_CONFIG_DIR`` the operator set: that is
    their own Claude config, and the whole point is that none of it comes along.
    """
    from personalclaw.sdk.util import config_dir

    return config_dir() / "cc-config"


def _prepare_isolated_config(root: Path) -> None:
    """Create ``root`` with an empty ``settings.json`` (``0600``) if it has none.

    Nothing is copied into it. An existing ``settings.json`` is left as it is, so what the
    operator chose to add to THIS config later is kept.
    """
    root.mkdir(parents=True, exist_ok=True)
    settings_path = root / "settings.json"
    if settings_path.exists():
        return
    from personalclaw.sdk.util import atomic_write

    atomic_write(settings_path, "{}\n")
    try:
        os.chmod(settings_path, 0o600)
    except OSError:
        logger.debug("acp:claude-code: chmod 0600 failed on %s", settings_path, exc_info=True)


def _isolated(config: dict | None) -> bool:
    """Whether the spawned Claude runs with its own config: yes unless the setting says no."""
    value = (config or {}).get(_ISOLATED_SETTING, True)
    return value is not False and str(value).strip().lower() not in ("false", "0", "no", "off")


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


def _build_env(*, isolated: bool = True) -> dict[str, str]:
    """Spawn env for the adapter: the isolated config dir + the resolved Claude binary.

    ``isolated`` (the ``isolated_config`` setting, on by default) points ``CLAUDE_CONFIG_DIR`` at
    PersonalClaw's own empty config. Off, the spawned Claude uses the operator's ``~/.claude``,
    auto-approve rules included, which is what the setting's help says.
    """
    env: dict[str, str] = {}

    if isolated:
        # Not caught: a config that cannot be prepared must not quietly become the operator's.
        root = _claude_config_root()
        _prepare_isolated_config(root)
        env["CLAUDE_CONFIG_DIR"] = str(root)

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


def login_command(*, isolated: bool = True) -> list[str]:
    """Suggested sign-in argv for the Sign-in terminal: ``claude /login``.

    Claude self-authenticates via its own ``/login`` flow (OAuth / API key);
    PersonalClaw stores no key. We pre-type the resolved ``claude`` binary so
    the user lands in the auth flow; the terminal is freeform so they can edit
    it (e.g. ``claude setup-token``) for any non-standard method. With isolation on,
    the sign-in lands in the isolated config the spawned Claude reads, not ``~/.claude``.
    """
    claude_exec = _resolve_claude_exec() or "claude"
    if isolated:
        return ["env", f"CLAUDE_CONFIG_DIR={_claude_config_root()}", claude_exec, "/login"]
    return [claude_exec, "/login"]


def create_provider(config: dict | None = None):
    """Bundle factory — register the ``acp:claude-code`` AgentProvider entry.

    Invoked by the extension system's ``agent``-type handler on enable (with the
    bundle's settings config). Resolves the adapter argv + Claude binary, applies
    config isolation (``isolated_config``, on unless set off), and publishes the
    ``acp_agent`` registry entry. Returns
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
    isolated = _isolated(config)
    env = _build_env(isolated=isolated) if command else {}

    register_acp_cli_entry(
        cli=CLI,
        dialect=DIALECT,
        command=command,
        model=model,
        env=env,
        env_passthrough=list(PROVIDER_ENV),
        extension=EXTENSION,
        login_command=login_command(isolated=isolated),
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
