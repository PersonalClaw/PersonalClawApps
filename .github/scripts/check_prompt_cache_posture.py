#!/usr/bin/env python3
"""Prompt-cache posture rail for first-party model-provider apps (PCS-8).

Every app that registers a model-provider TYPE must state a `prompt_cache` posture
EXPLICITLY, next to the evidence for it. The posture is a claim about what the upstream
service actually does, and an unbacked one is worse than `PromptCache.NONE`: `NONE` is
honest, whereas a wrong `AUTOMATIC` silently promises cache reads that never happen and a
wrong `EXPLICIT` ships a marker no adapter translates.

The four rules, each checked against the SOURCE (so an explicit `PromptCache.NONE` is
distinguishable from merely inheriting the field default, which it would not be at
runtime):

  R1  A model-provider app names `prompt_cache` in its `provider.py`.
  R2  The declaration carries an evidence comment block immediately above it, so the
      basis travels with the claim and a drive-by posture flip cannot be silent.
  R3  An app declaring EXPLICIT must OWN the translation - it reads the neutral
      `CACHE_HINT_KEY` marker - or ride core's Anthropic protocol client, which owns it.
      Declaring EXPLICIT while emitting nothing is the "marker into the void" failure.
  R4  Conversely, an app that names vendor cache syntax on the wire must declare
      EXPLICIT. Emitting a checkpoint the capability surface denies is the same lie
      pointing the other way.

Vacuity floor: this rail passes trivially the day the detector stops finding apps, so it
asserts a MINIMUM number of model-provider apps and a minimum number of postures of each
interesting kind. Deleting apps, renaming `provider.py`, or breaking the detector fails
here rather than going quiet.

Run from the repository root:  python .github/scripts/check_prompt_cache_posture.py
"""

from __future__ import annotations

import pathlib
import re
import sys

# An app registers a model-provider type if its provider.py builds either a branded spec
# (the shared helper) or a ProviderCapability (a hand-rolled provider). Derived from code,
# never a hand-maintained list - a new model app is picked up the day it lands.
_MODEL_APP_MARKERS = ("BrandedProviderSpec(", "ProviderCapability(")

# The floor. Kept BELOW today's count so ordinary growth never trips it, but high enough
# that a broken detector or a mass deletion cannot pass. Raise it deliberately, never to
# make a red go green.
_MIN_MODEL_APPS = 14
_MIN_EXPLICIT = 2      # bedrock-models (own wire) + the Anthropic-protocol apps
_MIN_AUTOMATIC = 2     # at least two substantiated always-on vendors
_MIN_NONE = 4          # the honest-unverified set must not quietly empty out

# `prompt_cache=PromptCache.X` (a spec/capability kwarg) or the
# `prompt_cache: PromptCache = PromptCache.X` class attr on a hand-rolled provider.
_DECL = re.compile(r"^\s*prompt_cache\s*(?::\s*PromptCache\s*)?=\s*PromptCache\.(\w+)", re.M)

# Vendor cache syntax. `cachePoint` is Bedrock's Converse checkpoint; `cache_control` is
# the Anthropic/OpenRouter breakpoint. Actionable literals only - not the bare word
# "cache", which legitimately appears in discovery-cache and KV-cache prose everywhere.
_VENDOR_SYNTAX = (re.compile(r"[\"']cachePoint[\"']"), re.compile(r"[\"']cache_control[\"']"))

# Evidence: at least this many comment lines directly above the declaration.
_MIN_EVIDENCE_COMMENT_LINES = 2


def _model_apps(root: pathlib.Path) -> dict[str, str]:
    """`{app_name: provider.py source}` for every app registering a model-provider type."""
    found: dict[str, str] = {}
    for provider in sorted(root.glob("*/provider.py")):
        src = provider.read_text(encoding="utf-8")
        if any(marker in src for marker in _MODEL_APP_MARKERS):
            found[provider.parent.name] = src
    return found


def _evidence_lines_above(src: str, decl_line_index: int) -> int:
    """Count the contiguous run of comment lines directly above a declaration."""
    lines = src.splitlines()
    count = 0
    i = decl_line_index - 1
    while i >= 0 and lines[i].lstrip().startswith("#"):
        count += 1
        i -= 1
    return count


def main() -> int:
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    apps = _model_apps(root)
    failures: list[str] = []
    postures: dict[str, set[str]] = {}

    for name, src in apps.items():
        matches = list(_DECL.finditer(src))
        if not matches:
            failures.append(
                f"{name}: declares no prompt_cache posture. Every model-provider app must "
                f"state one EXPLICITLY (PromptCache.NONE is a real answer - it just has to "
                f"be the recorded one), with the evidence beside it."
            )
            continue

        modes = {m.group(1) for m in matches}
        postures[name] = modes
        if len(modes) > 1:
            failures.append(
                f"{name}: declares conflicting postures {sorted(modes)}. The capability "
                f"(declarative twin) and the provider instance must agree - a capability "
                f"promising cache reads the instance never earns is worse than NONE."
            )

        # R2 - the basis travels with the claim.
        for m in matches:
            line_index = src[: m.start()].count("\n")
            if _evidence_lines_above(src, line_index) < _MIN_EVIDENCE_COMMENT_LINES:
                failures.append(
                    f"{name}: the prompt_cache declaration on line {line_index + 1} has no "
                    f"evidence comment above it (need >= {_MIN_EVIDENCE_COMMENT_LINES} comment "
                    f"lines). A posture is a claim about the upstream service; record what "
                    f"backs it - vendor documentation, a measured run, or why it is NONE."
                )

        emits_vendor_syntax = any(p.search(src) for p in _VENDOR_SYNTAX)
        reads_neutral_marker = "CACHE_HINT_KEY" in src
        # Riding core's Anthropic protocol client means core owns the translation.
        rides_anthropic_wire = 'protocol="anthropic"' in src or "AnthropicProvider" in src

        # R3 - EXPLICIT must actually place a marker somewhere.
        if "EXPLICIT" in modes and not (reads_neutral_marker or rides_anthropic_wire):
            failures.append(
                f"{name}: declares PromptCache.EXPLICIT but neither reads CACHE_HINT_KEY nor "
                f"rides core's Anthropic protocol client, so the marker core places is "
                f"translated by nobody. Either translate it in this app or declare NONE."
            )

        # R4 - vendor cache syntax on the wire must be declared.
        if emits_vendor_syntax and "EXPLICIT" not in modes:
            failures.append(
                f"{name}: emits vendor cache syntax on the wire but declares "
                f"{sorted(modes)}. A checkpoint the capability surface denies is the same "
                f"mis-declaration pointing the other way."
            )

    # ── Vacuity floor ─────────────────────────────────────────────────────────
    flat = [mode for modes in postures.values() for mode in modes]
    for label, actual, floor in (
        ("model-provider apps discovered", len(apps), _MIN_MODEL_APPS),
        ("apps with a declared posture", len(postures), _MIN_MODEL_APPS),
        ("EXPLICIT postures", flat.count("EXPLICIT"), _MIN_EXPLICIT),
        ("AUTOMATIC postures", flat.count("AUTOMATIC"), _MIN_AUTOMATIC),
        ("NONE postures", flat.count("NONE"), _MIN_NONE),
    ):
        if actual < floor:
            failures.append(
                f"VACUITY: {label} = {actual}, below the floor of {floor}. This rail passes "
                f"trivially when it inspects nothing, so a shortfall means the detector "
                f"broke or apps were removed - not that the repository got cleaner."
            )

    if failures:
        print("Prompt-cache posture rail FAILED (PCS-8):\n")
        for f in failures:
            print(f"  - {f}\n")
        return 1

    print(f"OK: {len(apps)} model-provider apps, every one declaring a prompt_cache posture.")
    for name in sorted(postures):
        print(f"    {name}: {'/'.join(sorted(postures[name]))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
