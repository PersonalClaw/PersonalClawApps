#!/usr/bin/env python3
"""Repo rail: one convention for ``advanced`` and ``required`` settings fields (AP-10).

The host renders every ``provider.settingsSchema`` with the same form widget, so an
uneven vocabulary reads as arbitrary: brave-search folds ``timeout_secs`` behind the
Advanced disclosure while a peer's identical field sits on the first screen. The
convention this rail pins is the one the catalog already practices by majority
(documented in docs/app-creation-guide.md, "Advanced and required — the one
convention"):

1. **Optional tuning fields fold.** A property whose name marks it as tuning/override
   class — ``timeout_secs``, an ``endpoint``/``*_endpoint``/``base_url`` override, a
   ``*_bin`` binary path — and which is NOT in the schema's ``required`` array must
   carry ``x-meta.tags: ["advanced"]``. A first-run user never needs it; the Advanced
   fold is where it lives.
2. **Required fields never fold.** A property listed in ``required`` must NOT be
   tagged ``advanced`` — required means the app cannot mount without it, and hiding a
   mandatory field behind a disclosure is a form that fails silently. (An ``endpoint``
   in ``required`` — the openai-compatible/ollama/vllm/searxng class, where pointing
   at your server IS the app — is therefore correctly untagged: requiredness, not the
   field name, is the discriminator.)
3. **``api_key`` is never required.** Every model/search provider falls back to an
   env var; marking one app's key required would make identical forms disagree about
   the same fact. The fallback is the documented convention, not an accident.

**Vacuity floor.** A rail that matches nothing reads as clean. Guards: at least
MIN_SCHEMAS schemas must be discovered, and every app in KNOWN_ADVANCED_ADOPTERS must
still exist and carry at least one ``advanced`` tag — if the manifest shape moves and
this scan stops seeing tags, the rail turns red instead of green.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Tuning/override name classes (rule 1). Deliberately narrow: only names whose class
# the catalog has already voted on. Judgment calls that aren't mechanically decidable
# (e.g. whether a model picker is advanced) stay prose-only in the style guide.
ADVANCED_CLASS = re.compile(r"(^timeout(_secs)?$|(^|_)endpoint$|^base_url$|_bin$)")

MIN_SCHEMAS = 40
KNOWN_ADVANCED_ADOPTERS = {"brave-search", "openai-models", "claude-code-agent"}


def main() -> int:
    failures: list[str] = []
    schemas = 0
    adopters_seen: set[str] = set()

    for manifest in sorted(ROOT.glob("*/app.json")):
        app = manifest.parent.name
        data = json.loads(manifest.read_text(encoding="utf-8"))
        schema = (data.get("provider") or {}).get("settingsSchema") or {}
        props = schema.get("properties") or {}
        if not props:
            continue
        schemas += 1
        required = set(schema.get("required") or [])

        for name, spec in props.items():
            tags = ((spec.get("x-meta") or {}).get("tags")) or []
            is_advanced = "advanced" in tags

            if is_advanced:
                adopters_seen.add(app)

            if ADVANCED_CLASS.search(name) and name not in required and not is_advanced:
                failures.append(
                    f"{app}: optional tuning field '{name}' must carry "
                    f'x-meta.tags ["advanced"] (rule 1)'
                )
            if name in required and is_advanced:
                failures.append(
                    f"{app}: required field '{name}' must not be tagged advanced — "
                    f"a mandatory field can't hide behind the fold (rule 2)"
                )
            if name == "api_key" and name in required:
                failures.append(
                    f"{app}: 'api_key' must not be required — the env-var fallback "
                    f"is the catalog convention (rule 3)"
                )

    if schemas < MIN_SCHEMAS:
        failures.append(
            f"vacuity floor: only {schemas} settingsSchema blocks discovered "
            f"(expected >= {MIN_SCHEMAS}) — did provider.settingsSchema move?"
        )
    missing = KNOWN_ADVANCED_ADOPTERS - adopters_seen
    if missing:
        failures.append(
            f"vacuity floor: known advanced-tag adopters not seen carrying the tag: "
            f"{sorted(missing)} — did x-meta.tags move?"
        )

    if failures:
        print(f"settings-schema posture: {len(failures)} violation(s)")
        for f in failures:
            print("  -", f)
        return 1
    print(f"settings-schema posture: clean ({schemas} schemas checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
