#!/usr/bin/env python3
"""Repo rail: every channel app adopts the ``trigger_source`` seam, and none hand-rolls glue.

CHANNEL-EXPANSION CE-10. ``WF2AUT-8`` shipped the app-registered trigger-source seam and
nothing required anybody to use it, so adoption sat at 0/4 with a test fixture as the only
implementer anywhere. This rail is what stops that recurring: it MEASURES adoption over the
channel apps it DISCOVERS, rather than asserting a count somebody has to remember to bump.

**Why a repo rail and not one app's test.** The ratio is a cross-app fact and the ``tests``
job runs pytest PER bundle, so no single bundle's ``test_*.py`` can see the denominator —
the same reason ``check_live_writes_posture.py``, ``check_prompt_cache_posture.py`` and
``check_readme_census.py`` live here. Each app additionally proves its OWN source really
FIRES, in its own ``tests/test_trigger_source.py``; declaration is what this file measures,
behaviour is what those drive.

**It reds in BOTH directions, and that is the point.**

* An adopting app that loses its ``trigger_source`` → numerator drops, red.
* A NEW channel app that arrives without one → DENOMINATOR grows, red. A hardcoded
  ``assert adopters == 4`` would stay green here, which is exactly the trap: 4/5 reads as
  "4, as expected".

**Four outcomes, not two.** "Declares a source", "declares none", "declares an empty
providers array" and "its manifest does not parse" are four different facts, and a count that
renders the last as zero adopters is a count that lies about the thing it was built to
measure. Each is reported in its own words, and the unparseable case reds rather than
silently lowering the ratio.

**Section B: no bespoke event glue.** Adoption is only half of CE-10 — the other half is that
apps reach the bus ONLY through core's registered handler. Measured over Python NAMES via
``ast``, not raw text: a docstring that says "there is no ``emit_event`` here" is not a call
to ``emit_event``, and a text scan that counts it trains readers to ignore the rail. Note
``SourceEvent`` is NOT forbidden — it is the SDK's own payload type and an adopting app
cannot emit without it; what is forbidden is binding that name from anywhere other than
``personalclaw.sdk.trigger_source``.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: The manifest provider type that makes an app a channel app — the denominator.
CHANNEL_TYPE = "channel"
#: The type this rail requires every channel app to also declare — the numerator.
SOURCE_TYPE = "trigger_source"

#: The SDK module an app may bind ``SourceEvent`` / ``TriggerSourceProvider`` from. Any other
#: origin means the app reached around the boundary for the same symbol.
SDK_TRIGGER_SOURCE = "personalclaw.sdk.trigger_source"

#: Core-internal names an app must never CALL or reference. Every one of these is a way to
#: push an event onto the bus (or to write the trigger store) without going through the
#: registered ``TriggerSourceTypeHandler`` — which is the definition of bespoke glue.
FORBIDDEN_NAMES = frozenset(
    {
        "emit_event",
        "dispatch_event",
        "event_bus",
        "get_event_bus",
        "register_source",
        "unregister_source",
    }
)

#: Core's own trigger store file. An app that writes one has forked the source of truth for
#: what automations exist — the failure the ``trigger`` provider type exists to prevent.
FORBIDDEN_FILENAMES = frozenset({"triggers.json", "event_triggers.json"})

#: Vacuity floor. A rail that discovered no channel apps would print "clean" while measuring
#: nothing, which is how a rail rots unnoticed. Four is what shipped; the floor rises only
#: when the population does, and it never caps the denominator.
MIN_CHANNEL_APPS = 4


def _declared_types(manifest: dict) -> dict[str, dict]:
    """Every provider ``type`` the manifest declares, across BOTH declaration shapes.

    A manifest may carry the canonical singular ``provider`` object, a ``providers`` array,
    or — the vendor-completeness shape — both. Reading one shape would report a complete app
    as channel-only, which is the bug core's own conformance clause 9 documents.
    """
    entries: list = []
    single = manifest.get("provider")
    if isinstance(single, dict):
        entries.append(single)
    listed = manifest.get("providers")
    if isinstance(listed, list):
        entries.extend(listed)
    return {
        entry["type"]: entry
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("type"), str)
    }


def _resolves(app_dir: Path, implementation: str) -> str:
    """Empty if ``module:factory`` resolves to a real file and a real top-level def.

    Otherwise the reason. A declaration pointing at nothing installs fine and then does
    nothing at enable time — a *declared* adopter that cannot possibly fire, which is the
    same shape as the seam's original 0/4 problem one level down. Checked statically (no
    import) so this rail needs no core install and cannot be fooled by a stub on sys.path.
    """
    if ":" not in implementation:
        return f"implementation {implementation!r} is not 'module:factory'"
    module, _, factory = implementation.partition(":")
    path = app_dir.joinpath(*module.split(".")).with_suffix(".py")
    if not path.is_file():
        return f"implementation module {module!r} has no file at {path.relative_to(ROOT)}"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        return f"implementation module {module!r} does not parse: {exc}"
    defs = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    if factory not in defs:
        return f"implementation module {module!r} defines no top-level {factory!r}"
    return ""


def check_adoption() -> tuple[list[str], list[str]]:
    """Section A. Returns (failures, report_lines)."""
    failures: list[str] = []
    adopters: list[str] = []
    missing: list[str] = []
    empty: list[str] = []
    dead: list[str] = []
    unparseable: list[str] = []

    for manifest_path in sorted(ROOT.glob("*/app.json")):
        app = manifest_path.parent.name
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # NOT counted as a non-adopter: "this app declares no source" and "this app's
            # manifest could not be read" are different facts, and rendering the second as
            # the first is how a ratio lies. It reds on its own terms instead.
            unparseable.append(app)
            failures.append(
                f"{app}: app.json does not parse ({type(exc).__name__}: {exc}) — this app's "
                f"adoption is UNKNOWN, not zero. Fix the manifest; the ratio below excludes it"
            )
            continue
        if not isinstance(data, dict):
            unparseable.append(app)
            failures.append(f"{app}: app.json is not an object — adoption is UNKNOWN, not zero")
            continue

        declared = _declared_types(data)
        if CHANNEL_TYPE not in declared:
            continue  # not a channel app; this rail says nothing about it

        entry = declared.get(SOURCE_TYPE)
        if entry is None:
            # A THIRD state, distinct from both above: the manifest parsed, the app is a
            # channel app, and it declares no source. An empty `providers: []` lands here
            # too, and is named separately so the two are not confused in the report.
            if isinstance(data.get("providers"), list) and not data["providers"]:
                empty.append(app)
                failures.append(
                    f"{app}: declares a channel transport and an EMPTY providers[] array — "
                    f"an empty list is a declaration that registers nothing, not an absent "
                    f"one. Add a {{'type': '{SOURCE_TYPE}'}} provider (CE-10)"
                )
            else:
                missing.append(app)
                failures.append(
                    f"{app}: declares a channel transport but NO {SOURCE_TYPE} provider — "
                    f"its manifest declares {sorted(declared)}. One vendor app owns every "
                    f"seam that vendor touches; the seam is live (WF2AUT-8). See "
                    f"docs/guides/build-a-channel-app.md, 'Vendor completeness'"
                )
            continue

        implementation = entry.get("implementation")
        if not isinstance(implementation, str) or not implementation:
            # DECLARED BUT DEAD, fifth state: it counts in the DENOMINATOR (the app is a
            # channel app whose manifest read fine) but never in the numerator. Dropping it
            # from both would shrink the ratio's base and read as "everyone left has adopted".
            dead.append(app)
            failures.append(
                f"{app}: its {SOURCE_TYPE} entry declares no implementation — a type with no "
                f"factory installs and then does nothing"
            )
            continue
        reason = _resolves(manifest_path.parent, implementation)
        if reason:
            # Same fifth state: a declaration nothing can build is the trap
            # `test_manifest_types_match_handlers` exists for, one level down. DECLARED is not
            # ADOPTED, and this is the distinction that keeps the ratio honest.
            dead.append(app)
            failures.append(f"{app}: {SOURCE_TYPE} declared but unresolvable — {reason}")
            continue
        adopters.append(app)

    denominator = len(adopters) + len(missing) + len(empty) + len(dead)
    report = [
        f"trigger-source adoption: {len(adopters)}/{denominator} channel app(s)",
        f"  adopted:       {sorted(adopters) or '(none)'}",
        f"  no source:     {sorted(missing) or '(none)'}",
        f"  empty array:   {sorted(empty) or '(none)'}",
        f"  declared-dead: {sorted(dead) or '(none)'}",
        f"  unreadable:    {sorted(unparseable) or '(none)'}",
    ]
    if denominator < MIN_CHANNEL_APPS:
        failures.append(
            f"vacuity floor: only {denominator} channel app(s) discovered (expected >= "
            f"{MIN_CHANNEL_APPS}) — did the channel provider declaration move? A rail that "
            f"measures nothing prints clean"
        )
    return failures, report


def _app_python_files() -> list[Path]:
    """Every ``.py`` file inside an app bundle, tests included.

    Tests included on purpose, and the forbidden-name rule is relaxed for them below: an
    app's own suite legitimately drives core's registry to prove its source fires, and
    excluding tests outright would let real glue hide in a ``test_`` file.
    """
    out: list[Path] = []
    for manifest in sorted(ROOT.glob("*/app.json")):
        for path in sorted(manifest.parent.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            out.append(path)
    return out


def check_no_bespoke_glue() -> tuple[list[str], list[str]]:
    """Section B. Returns (failures, report_lines)."""
    failures: list[str] = []
    scanned = 0
    source_event_binders: list[str] = []

    for path in _app_python_files():
        rel = path.relative_to(ROOT)
        is_test = path.name.startswith("test_") or "tests" in path.parts
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            failures.append(f"{rel}: does not parse ({exc}) — cannot be cleared of glue")
            continue
        scanned += 1

        source_event_from: set[str] = set()
        for node in ast.walk(tree):
            # (1) Where did `SourceEvent` / `TriggerSourceProvider` come from?
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                for alias in node.names:
                    if alias.name in {"SourceEvent", "TriggerSourceProvider"}:
                        source_event_from.add(node.module)

            # (2) Forbidden core-internal names, as NAMES rather than as text.
            name = ""
            if isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.Attribute):
                name = node.attr
            if name in FORBIDDEN_NAMES and not is_test:
                failures.append(
                    f"{rel}: references core-internal {name!r} — an app reaches the bus only "
                    f"through the registered trigger_source handler (CE-10). Emit through the "
                    f"callable core hands your provider's start()"
                )

            # (3) Core's trigger store, by filename.
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in FORBIDDEN_FILENAMES and not is_test:
                    failures.append(
                        f"{rel}: names {node.value!r} — an app must not write core's trigger "
                        f"store; serve rows through a `trigger` provider instead"
                    )

        bad_origin = {m for m in source_event_from if m != SDK_TRIGGER_SOURCE}
        if bad_origin:
            failures.append(
                f"{rel}: binds SourceEvent/TriggerSourceProvider from {sorted(bad_origin)} — "
                f"the only permitted origin is {SDK_TRIGGER_SOURCE!r}"
            )
        if SDK_TRIGGER_SOURCE in source_event_from:
            source_event_binders.append(str(rel))

    report = [
        f"no-bespoke-glue: {scanned} app python file(s) scanned",
        f"  seam imports: {len(source_event_binders)} file(s) bind the SDK trigger-source "
        f"contract",
    ]
    if not source_event_binders:
        # The vacuity floor for THIS section. Zero binders means either nobody adopted the
        # seam (section A reds) or the SDK module moved and this scan is now inert — and an
        # inert glue check is indistinguishable from a clean one without this line.
        failures.append(
            f"vacuity floor: no app binds anything from {SDK_TRIGGER_SOURCE} — either "
            f"adoption is zero or the SDK module moved and this scan is inert"
        )
    return failures, report


def main() -> int:
    failures_a, report_a = check_adoption()
    failures_b, report_b = check_no_bespoke_glue()
    for line in report_a + report_b:
        print(line)
    failures = failures_a + failures_b
    if failures:
        print(f"\ntrigger-source rail: {len(failures)} violation(s)")
        for failure in failures:
            print("  -", failure)
        return 1
    print("\ntrigger-source rail: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
