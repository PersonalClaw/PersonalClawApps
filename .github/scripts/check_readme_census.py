#!/usr/bin/env python3
"""Repo rail: the README's app census must match the tree.

The README opens with a census — a headline bundle count and a per-category
breakdown — and it is the first thing a reader trusts. It is also the one fact in
this repo that no app's own PR owns: nine consecutive app PRs each correctly left
it alone, and it drifted to "47 apps" over a tree of 64 bundles, with category
counts that summed to 56. A census nobody owns needs a machine to own it.

What this checks, all derived from ``*/app.json``:

1. **The headline.** ``**<N> app bundles**`` must equal the number of
   ``*/app.json`` files. One bundle = one directory = one manifest.
2. **Every category count.** Each census bullet declares its machine key and its
   count as ``- **<Label>** (`<key>`, <N>)``. ``<N>`` must equal the number of
   entries the tree puts under ``<key>``.
3. **No category in either direction only.** A key present in the tree but not in
   the README (a whole capability type gone invisible — how `sandbox` and
   `trigger` were lost) fails; so does a README key the tree no longer has.
4. **Every bundle is named.** Each bundle's directory name must appear somewhere
   in the README as a code span, so a new app cannot land unlisted even when its
   category count happens to be right.

**Bundles vs. providers.** These differ, and conflating them is what made the old
census disagree with its own arithmetic. A bundle may contribute two providers —
``companion`` ships a ``tool`` and a ``trigger``, ``slack-channel`` a ``channel``
and an ``inbox`` — so category entries (66) exceed bundles (64). The headline
counts bundles; the categories count providers; both are checked here, which is
the only way the two numbers stay reconcilable.

**Keys.** A key is a ``provider.type`` (``model``, ``search``, ``tool``, …) with
two pseudo-types for the bundles that contribute no provider at all:

* ``backend+ui`` — no provider, installs on the server (``growth``, ``minutes``).
* ``client-install`` — no provider, ``platform.installMode: "client"``, i.e. it
  installs on the operator's own machine (``browser-connector``,
  ``menu-bar-companion``).

**Vacuity floor.** A rail that matches nothing reads as clean. At least
MIN_BUNDLES manifests and MIN_CATEGORIES census bullets must be discovered — if
the manifest shape or the README section moves, this turns red rather than green.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"

# "**64 app bundles**" — the headline, counted in bundles (directories).
HEADLINE = re.compile(r"\*\*(\d+) app bundles\*\*")

# "- **Model providers** (`model`, 23) — ..." — one census bullet.
CATEGORY = re.compile(r"^- \*\*(?P<label>[^*]+)\*\* \(`(?P<key>[^`]+)`, (?P<count>\d+)\)")

MIN_BUNDLES = 40
MIN_CATEGORIES = 8


def census(root: Path) -> tuple[list[str], dict[str, list[str]]]:
    """Return (bundle names, {category key: [bundle names]}) read from the tree."""
    names: list[str] = []
    by_key: dict[str, list[str]] = {}
    for manifest in sorted(root.glob("*/app.json")):
        name = manifest.parent.name
        names.append(name)
        data = json.loads(manifest.read_text(encoding="utf-8"))
        keys: list[str] = []
        # `provider` is the primary contribution; `providers` is the additional
        # ones (a bundle may ship both — that is the whole bundles/providers gap).
        primary = data.get("provider")
        if isinstance(primary, dict) and primary.get("type"):
            keys.append(primary["type"])
        for extra in data.get("providers") or []:
            if isinstance(extra, dict) and extra.get("type"):
                keys.append(extra["type"])
        if not keys:
            client = (data.get("platform") or {}).get("installMode") == "client"
            keys = ["client-install" if client else "backend+ui"]
        for key in keys:
            by_key.setdefault(key, []).append(name)
    return names, by_key


def main() -> int:
    failures: list[str] = []
    names, tree = census(ROOT)
    text = README.read_text(encoding="utf-8")

    headline = HEADLINE.search(text)
    if headline is None:
        failures.append(
            'README has no "**<N> app bundles**" headline — the census must state '
            "its own bundle total for this rail to check it"
        )
    elif int(headline.group(1)) != len(names):
        failures.append(
            f"README headline says {headline.group(1)} app bundles, but the tree has "
            f"{len(names)} */app.json"
        )

    declared: dict[str, int] = {}
    for line in text.splitlines():
        m = CATEGORY.match(line)
        if m:
            key = m.group("key")
            if key in declared:
                failures.append(f"README declares category '{key}' twice")
            declared[key] = int(m.group("count"))

    for key in sorted(set(declared) | set(tree)):
        actual = len(tree.get(key, ()))
        if key not in declared:
            failures.append(
                f"category '{key}' has {actual} entr{'y' if actual == 1 else 'ies'} in "
                f"the tree ({', '.join(tree[key])}) but no README bullet — add one, because "
                f"a capability type with no bullet is invisible to every reader"
            )
        elif key not in tree:
            failures.append(
                f"README declares category '{key}' ({declared[key]}) but the tree has "
                f"no such bundle — drop the bullet"
            )
        elif declared[key] != actual:
            failures.append(
                f"category '{key}': README says {declared[key]}, tree has {actual} "
                f"({', '.join(tree[key])})"
            )

    for name in names:
        if "`" + name + "`" not in text:
            failures.append(
                f"bundle '{name}' is never named in the README — list it under its "
                f"category bullet"
            )

    if len(names) < MIN_BUNDLES:
        failures.append(
            f"vacuity floor: only {len(names)} bundle(s) discovered (expected >= "
            f"{MIN_BUNDLES}) — did the */app.json layout move?"
        )
    if len(declared) < MIN_CATEGORIES:
        failures.append(
            f"vacuity floor: only {len(declared)} census bullet(s) parsed from the "
            f"README (expected >= {MIN_CATEGORIES}) — did the bullet format move? "
            'Expected lines like: - **Model providers** (`model`, 23) — ...'
        )

    if failures:
        print(f"README census: {len(failures)} violation(s)")
        for f in failures:
            print("  -", f)
        return 1
    entries = sum(len(v) for v in tree.values())
    print(
        f"README census: clean ({len(names)} bundles, {entries} category entries "
        f"across {len(declared)} categories)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
