"""``acp:kiro-cli`` bundle — the kiro CLI as a removable ACP agent provider.

kiro-cli speaks ACP in the protocol shape the core ``DefaultDialect`` encodes
(date-string ``protocolVersion``, ``session/set_mode`` agent activation,
``session/set_model``). The dialect id ``"kiro-cli"`` maps to that default shape in
the committed core registry, so this bundle needs no protocol code at all — just
binary resolution.

This is the bundle where any kiro-specific knowledge belongs (per the
vendor-specific-in-bundles-only rule). It resolves the ``kiro-cli`` binary (from
``KIRO_CLI_BIN``, else ``$PATH``) and launches it in ACP stdio mode via the ``acp``
subcommand (``kiro-cli acp``). kiro-cli is a native binary (not an npm package), so
there is no ``npx`` fallback.

On a machine without the binary the provider registers nothing and probes as
unavailable, so its card says so instead of offering a toggle that cannot work.
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


def resolve_command() -> list[str] | None:
    """Resolve the ``kiro-cli acp`` launch argv (env override → PATH).

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

    kiro-cli is a native binary installed separately, so on a machine without it
    the provider is simply absent. The extension list surfaces this via
    :func:`personalclaw.providers.loader.load_availability` so the card greys out +
    can't be enabled instead of letting the user toggle a provider that will only
    ever probe as unavailable. Vendor-specific presence logic lives ONLY here, in
    the removable bundle.
    """
    if resolve_command():
        return True, ""
    return False, "kiro-cli binary not found on this machine (put it on PATH, or set KIRO_CLI_BIN)."


def login_command(command: list[str] | None = None) -> list[str]:
    """Suggested sign-in argv for the Sign-in terminal: ``kiro-cli login``.

    Derives the binary from the resolved launch argv (so it matches the same
    ``kiro-cli`` the ACP runtime uses, wherever it was resolved from) and swaps the
    ``acp`` subcommand for ``login``. The terminal is freeform so the user can run
    whatever other sign-in step their setup needs instead.
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
    )
    return None
