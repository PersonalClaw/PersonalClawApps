"""``acp:gemini-cli`` bundle — Google's Gemini CLI as a removable ACP agent provider.

Gemini CLI speaks ACP in the protocol shape the core ``DefaultDialect`` encodes, so
this bundle needs no protocol code at all — just binary resolution. The core runner
catalog (``agents/runner_catalog.json``) declares the matching row
(``runtime_id: "acp:gemini-cli"``, ``bin_names: ["gemini"]``,
``acp_args: ["--experimental-acp"]``, ``adapter: null``); this bundle is the
implementation half of that row, and the ``acp:gemini-cli`` runtime id is the join key
between them.

This is the bundle where any Google/Gemini-specific knowledge belongs (per the
vendor-specific-in-bundles-only rule):

* the binary is ``gemini`` (npm ``@google/gemini-cli``), overridable with
  ``GEMINI_CLI_EXECUTABLE``;
* ACP stdio mode is entered via the **flag** ``--experimental-acp`` — not a
  subcommand (kiro's ``kiro-cli acp``) and not a separate npm adapter binary
  (claude-code's ``claude-agent-acp``). There is no adapter package to resolve or
  pin, and the binary *is* the engine, so no ``requires_executable`` is declared;
* auth is Gemini CLI's own — Google OAuth on first interactive run, or a
  ``GEMINI_API_KEY`` in the environment. PersonalClaw stores no key.

The binary is absent on a machine that has never installed Gemini CLI, so the
provider registers nothing and probes as unavailable there rather than erroring.
"""

from __future__ import annotations

import logging
import os

from personalclaw.sdk.acp import resolve_acp_cli
from personalclaw.sdk.acp import register_acp_cli_entry

logger = logging.getLogger(__name__)

# ── identity ──────────────────────────────────────────────────────────────
CLI = "gemini-cli"
# Gemini CLI speaks the baseline ACP shape, so it selects the core "default"
# dialect — no vendor-specific dialect id lives in the neutral core. (The catalog
# row spells the same thing as an empty `dialect`.)
DIALECT = "default"
# The extension/bundle that owns this runtime (UI join key — see claude_code).
EXTENSION = "gemini-cli-agent"

# Same env var the core catalog row declares, so an operator override set for the
# catalog's probe also moves this bundle's launch argv.
_BIN_ENV = "GEMINI_CLI_EXECUTABLE"
_BIN_NAMES = ["gemini"]
# Gemini CLI enters ACP stdio-protocol mode via a FLAG, not a subcommand.
_ACP_FLAG = ["--experimental-acp"]


def resolve_command() -> list[str] | None:
    """Resolve the ``gemini --experimental-acp`` launch argv (env override → PATH).

    No npm fallback: ``npx -y @google/gemini-cli`` would download a fresh copy per
    spawn and would not share the user's OAuth state, so an unresolved binary is
    reported as unavailable instead.
    """
    argv = resolve_acp_cli(
        env_var=_BIN_ENV,
        bin_names=_BIN_NAMES,
        npm_pkg=None,
        subcommand=_ACP_FLAG,
    )
    if argv and _ACP_FLAG[0] not in argv:
        # A single-token GEMINI_CLI_EXECUTABLE override resolves to a bare binary —
        # the SDK never appends `subcommand` to an override, because for most CLIs an
        # override IS the complete argv. Gemini only speaks ACP behind the flag, so a
        # bare argv would launch the interactive REPL and never answer `initialize`.
        # Append it here; a full-argv override that already carries the flag is left
        # exactly as the operator typed it.
        argv = [*argv, *_ACP_FLAG]
    return argv


def availability() -> tuple[bool, str]:
    """Whether this provider can run on this machine, + a UI reason if not.

    Gemini CLI is a public npm package, but it is not a PersonalClaw dependency and
    is absent until the user installs it. The extension list surfaces this via
    :func:`personalclaw.providers.loader.load_availability` so the card greys out +
    can't be enabled instead of letting the user toggle a provider that will only
    ever probe as unavailable. Vendor-specific presence logic lives ONLY here, in
    the removable bundle.
    """
    if resolve_command():
        return True, ""
    return False, "gemini binary not found on this machine (install: npm i -g @google/gemini-cli)."


def login_command(command: list[str] | None = None) -> list[str]:
    """Suggested sign-in argv for the Sign-in terminal: a bare ``gemini``.

    Gemini CLI self-authenticates — the first interactive run presents its own auth
    picker (Google OAuth / Gemini API key / Vertex AI) and ``/auth`` re-runs it — so
    the suggestion is simply the resolved binary with the ACP flag stripped, which
    lands the user in that picker. There is no ``gemini login`` subcommand to
    pre-type. The terminal is freeform, so a user preferring a key can instead
    export ``GEMINI_API_KEY`` there; PersonalClaw stores no key either way.
    """
    argv = command if command is not None else resolve_command()
    binary = argv[0] if argv else "gemini"
    return [binary]


def create_provider(config: dict | None = None):
    """Bundle factory — register the ``acp:gemini-cli`` AgentProvider entry.

    Returns ``None`` (agents are config/registry-based — same contract as the
    ``native-agents`` bundle); registration is the side effect. An unresolvable
    binary registers nothing, which is how the absent case reaches the UI as "not
    available" rather than a hard error.
    """
    config = config or {}

    bin_override = str(config.get("acp_bin", "") or "").strip()
    if bin_override:
        os.environ[_BIN_ENV] = bin_override
    model = str(config.get("model", "") or "").strip()

    command = resolve_command()
    register_acp_cli_entry(
        cli=CLI,
        dialect=DIALECT,
        command=command,
        model=model,
        extension=EXTENSION,
        login_command=login_command(command),
    )
    return None
