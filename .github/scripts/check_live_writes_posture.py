#!/usr/bin/env python3
"""Repo rail: every channel app's ``send()`` honors DISABLE_LIVE_WRITES (§1.4).

A channel ``send()`` is the platform's most irreversible write — an outward message a
human sees before any undo could run. Core refuses that class of write when
``PERSONALCLAW_DISABLE_LIVE_WRITES`` is set (``net.fetch`` non-GET egress,
``delete_model``), but core cannot enforce it inside an app bundle: the transport owns
its own wire call. So the enforcement is this rail.

What it checks, per app whose manifest declares ``provider.type == "channel"``:

1. the transport module named by ``provider.implementation`` calls
   ``live_writes_disabled()`` INSIDE its ``async def send`` body — asserted on the AST,
   not on a text match, so a mention in a docstring or a sibling method cannot satisfy
   it, and neither can a call that sits in ``health()``;
2. a ``writes.py`` sits beside that transport;
3. that ``writes.py`` spells the fail-safe token set EXACTLY as core does. Each app
   carries its own copy (an installed bundle has no sibling to import and the core
   symbol is not an SDK export), so "all four agree" is a property that has to be
   checked rather than assumed. Each bundle's own suite additionally pins its parse
   against core's live symbol; this rail is the cheap structural half that runs
   without core installed.

**Vacuity floor.** A rail that matches nothing reads as clean. Two guards: the four
channel apps known to exist at the time of writing MUST all be discovered, and at
least that many must be found. A fifth channel app is welcome — the per-app loop then
holds it to the same contract — but a channel app DISAPPEARING, an ``app.json``
``provider`` block changing shape, or a glob that stops matching turns this red
instead of green.

Run locally exactly as CI does:

    python .github/scripts/check_live_writes_posture.py
"""

from __future__ import annotations

import ast
import json
import pathlib
import sys

#: The env var whose spelling every copy must share. Not this rail's to rename.
ENV_VAR = "PERSONALCLAW_DISABLE_LIVE_WRITES"

#: The guard function a channel ``send()`` must consult.
GUARD_FN = "live_writes_disabled"

#: Core's fail-safe token set (``personalclaw.guardrails.flags``). Every other value —
#: unknown token, empty string, typo — must parse as "guard ON".
CANONICAL_FALSE = frozenset({"0", "false", "no", "off", "disable", "disabled", "n", "f"})

#: The channel apps that exist today. Named so a DISAPPEARING app reds this rail rather
#: than shrinking it to a vacuous pass. Adding a channel app does not require editing
#: this set — the discovery loop already holds any new one to the same contract.
KNOWN_CHANNEL_APPS = frozenset({"telegram-channel", "discord-channel", "slack-channel",
                                "email-channel"})


def transport_path(app: pathlib.Path, implementation: str) -> pathlib.Path:
    """``"discord_runtime.transport:create_provider"`` → ``discord-channel/discord_runtime/transport.py``."""
    module = implementation.split(":", 1)[0]
    return app.joinpath(*module.split(".")).with_suffix(".py")


def send_calls_the_guard(tree: ast.AST) -> bool:
    """Does an ``async def send`` in this module call :data:`GUARD_FN` in its own body?

    Asserted on the AST so neither a docstring mention nor a call in a NEIGHBOURING
    method (``health``, ``test``) can satisfy the rail — a guard in the wrong method
    suppresses nothing.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef) or node.name != "send":
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call):
                fn = inner.func
                name = getattr(fn, "id", None) or getattr(fn, "attr", None)
                if name == GUARD_FN:
                    return True
    return False


def declared_false_tokens(tree: ast.AST) -> frozenset[str] | None:
    """The string set assigned to ``_EXPLICIT_FALSE``, or ``None`` when absent."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "_EXPLICIT_FALSE" not in names:
            continue
        try:
            # frozenset({...}) — evaluate the literal set argument only.
            call = node.value
            if isinstance(call, ast.Call) and len(call.args) == 1:
                return frozenset(ast.literal_eval(call.args[0]))
            return frozenset(ast.literal_eval(call))
        except Exception:  # noqa: BLE001 — an unevaluatable literal is a failure, not a crash
            return None
    return None


def main() -> int:
    root = pathlib.Path(__file__).resolve().parents[2]
    problems: list[str] = []
    found: set[str] = set()

    for manifest in sorted(root.glob("*/app.json")):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 — manifest-validate owns parse errors
            problems.append(f"{manifest}: unreadable ({type(exc).__name__}: {exc})")
            continue
        provider = data.get("provider") or {}
        if provider.get("type") != "channel":
            continue

        app = manifest.parent
        found.add(app.name)

        implementation = str(provider.get("implementation") or "")
        if not implementation:
            problems.append(f"{app.name}: manifest declares a channel but no implementation")
            continue

        transport = transport_path(app, implementation)
        if not transport.is_file():
            problems.append(f"{app.name}: transport module not found at {transport}")
            continue

        tree = ast.parse(transport.read_text(encoding="utf-8"), filename=str(transport))
        if not send_calls_the_guard(tree):
            problems.append(
                f"{app.name}: {transport.relative_to(root)} — async def send() does not call "
                f"{GUARD_FN}(). A channel send is a live, irreversible outward write; it must "
                f"return a typed refusal while {ENV_VAR} is set."
            )

        writes = transport.with_name("writes.py")
        if not writes.is_file():
            problems.append(f"{app.name}: no writes.py beside {transport.relative_to(root)}")
            continue

        tokens = declared_false_tokens(ast.parse(writes.read_text(encoding="utf-8"),
                                                filename=str(writes)))
        if tokens is None:
            problems.append(f"{app.name}: {writes.relative_to(root)} declares no _EXPLICIT_FALSE set")
        elif tokens != CANONICAL_FALSE:
            problems.append(
                f"{app.name}: {writes.relative_to(root)} — _EXPLICIT_FALSE drifted from core's. "
                f"extra={sorted(tokens - CANONICAL_FALSE)} missing={sorted(CANONICAL_FALSE - tokens)}. "
                f"A guard that disagrees with the platform about whether writes are on is worse "
                f"than no guard."
            )

    # ── the vacuity floor ──
    missing = KNOWN_CHANNEL_APPS - found
    if missing:
        problems.append(
            f"vacuity floor: expected channel apps not discovered: {sorted(missing)}. "
            f"Either an app was removed (update KNOWN_CHANNEL_APPS in the same change) or "
            f"the manifest discovery stopped matching — a rail that matches nothing passes."
        )
    if len(found) < len(KNOWN_CHANNEL_APPS):
        problems.append(
            f"vacuity floor: only {len(found)} channel app(s) inspected, expected at least "
            f"{len(KNOWN_CHANNEL_APPS)}"
        )

    if problems:
        print("live-writes posture: FAIL")
        for p in problems:
            print(f"  {p}")
        return 1
    print(f"OK: {len(found)} channel app(s) honor {ENV_VAR} in send(): {', '.join(sorted(found))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
