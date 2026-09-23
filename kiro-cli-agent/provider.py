"""``acp:kiro-cli`` bundle — the kiro CLI as a removable ACP agent provider.

kiro-cli is an Amazon-internal CLI that speaks ACP in the protocol shape the core
``DefaultDialect`` encodes (date-string ``protocolVersion``, ``session/set_mode``
agent activation, ``session/set_model``). The dialect id ``"kiro-cli"`` maps to
that default shape in the committed core registry, so this bundle needs no
protocol code at all — just binary resolution.

This is the bundle where any Amazon/AIM/kiro-specific knowledge belongs (per the
vendor-specific-in-bundles-only rule). It resolves the ``kiro-cli`` binary
(commonly ``~/.toolbox/bin/kiro-cli`` or on ``$PATH``) and launches it in ACP
stdio mode via the ``acp`` subcommand (``kiro-cli acp``). kiro-cli is a native
binary (not an npm package), so there is no ``npx`` fallback.

The binary is absent on a generic OSS machine, so the provider registers nothing
and probes as unavailable there — correct for an internal-only CLI.

kiro-cli sandboxes its own child process, which is why this bundle declares
``self_sandboxing=True`` — see :data:`SELF_SANDBOXING` for the measurement.
"""

from __future__ import annotations

import logging
import os

from personalclaw.sdk.acp import resolve_acp_cli
from personalclaw.sdk.acp import register_acp_cli_entry

logger = logging.getLogger(__name__)

# ── identity ──────────────────────────────────────────────────────────────
CLI = "kiro-cli"
# kiro-cli speaks the baseline ACP shape, so it selects the core "default"
# dialect — no vendor-specific dialect id lives in the neutral core.
DIALECT = "default"
# The extension/bundle that owns this runtime (UI join key — see claude_code).
EXTENSION = "kiro-cli-agent"

_BIN_ENV = "KIRO_CLI_BIN"
_BIN_NAMES = ["kiro-cli"]
# kiro-cli enters ACP stdio-protocol mode via the `acp` subcommand.
_ACP_SUBCOMMAND = ["acp"]

# kiro-cli initializes its OWN OS-level sandbox around the child it execs, and that
# inner `sandbox_apply` is refused inside the host's generated seatbelt profile. (Not
# because macOS forbids nesting as such — a bare `(allow default)` profile nests fine;
# it is the host profile's `deny` rules that forbid it.) Under the host's default
# `sandbox-exec` wrap the CLI therefore dies during startup instead of speaking ACP:
#
#   sandbox initialization failed: Operation not permitted
#   Error: Failed to spawn child process
#   Caused by: Invalid argument (os error 22)
#
# It writes that to stderr, nothing to stdout, and exits 1 — so the host's only
# protocol-level symptom was ``handshake failed: ACP stdout EOF``, with no hint that a
# sandbox was the cause. Measured as a paired control on kiro-cli 2.23.0 / macOS 27.0:
# the same argv, env, cwd and ``start_new_session`` spawn fails wrapped and completes
# ``initialize`` unwrapped (349-byte response, empty stderr).
#
# Declaring the fact here — not a sandbox level — keeps this vendor-specific and lets
# the core derive ``sandbox_mode="off"``. The CLI's own sandbox still confines the
# child, and every ACP tool call still crosses the host's PreToolUse deny gate and
# four-tier approval, which is where tool authority actually lives.
SELF_SANDBOXING = True


def resolve_command() -> list[str] | None:
    """Resolve the ``kiro-cli acp`` launch argv (env override → PATH/toolbox).

    No npm fallback (native binary). The ``acp`` subcommand is appended to a
    resolved binary; a full-argv ``KIRO_CLI_BIN`` override is honoured verbatim.
    """
    return resolve_acp_cli(
        env_var=_BIN_ENV,
        bin_names=_BIN_NAMES,
        npm_pkg=None,
        subcommand=_ACP_SUBCOMMAND,
    )


def availability() -> tuple[bool, str]:
    """Whether this provider can run on this machine, + a UI reason if not.

    kiro-cli is an Amazon-internal native binary that isn't published anywhere
    public, so on a generic OSS machine it's simply absent. The extension list
    surfaces this via :func:`personalclaw.providers.loader.load_availability`
    so the card greys out + can't be enabled instead of letting the user toggle
    a provider that will only ever probe as unavailable. Vendor-specific
    presence logic lives ONLY here, in the removable bundle.
    """
    if resolve_command():
        return True, ""
    return False, "kiro-cli binary not found on this machine (Amazon-internal CLI)."


def login_command(command: list[str] | None = None) -> list[str]:
    """Suggested sign-in argv for the Sign-in terminal: ``kiro-cli login``.

    Derives the binary from the resolved launch argv (so it matches the same
    ``kiro-cli`` the ACP runtime uses, ``~/.toolbox/bin`` and all) and swaps the
    ``acp`` subcommand for ``login``. The terminal is freeform so the user can
    substitute any Amazon-internal auth variant (e.g. ``mwinit``-driven flows).
    """
    argv = command if command is not None else resolve_command()
    binary = argv[0] if argv else "kiro-cli"
    return [binary, "login"]


def create_provider(config: dict | None = None):
    """Bundle factory — register the ``acp:kiro-cli`` AgentProvider entry.

    Returns ``None`` (agents are config/registry-based — same contract as the
    ``native-agents`` bundle); registration is the side effect.
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
        # kiro-cli applies its own OS sandbox; the host's cannot nest around it.
        self_sandboxing=SELF_SANDBOXING,
    )
    return None
