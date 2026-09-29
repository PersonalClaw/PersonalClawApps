#!/usr/bin/env python3
"""Repo rail: every app's README says the licence its LICENSE file and its app.json say.

An app ships three statements of one licence: the ``LICENSE`` file, ``app.json``'s ``license``
(the Store shows it) and the README's License section. Nothing tied them together, and two drifted:
``rsync-sync`` and ``s3-sync`` shipped an MIT ``LICENSE`` and an MIT manifest under a README that
said Apache-2.0.

What this checks, for every ``*/app.json``:

1. The ``LICENSE`` file's licence (read from its first line) is the one ``app.json`` names.
2. A README License section names that licence, and no other. A README without one makes no
   claim, and is not held to one.

**Vacuity floor.** At least :data:`MIN_BUNDLES` manifests, and :data:`MIN_SECTIONS` README
License sections, must be read, and the detector is checked against the shapes it tells apart
before anything is read: a rail that matches nothing reads as clean.

Run locally exactly as CI does:

    python .github/scripts/check_readme_license.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

MIN_BUNDLES = 60
MIN_SECTIONS = 50

#: The licences an app here is released under, each as it is written in the three places.
#: A README may write Apache-2.0 as "Apache 2.0" or "Apache License".
LICENSES = {
    "MIT": re.compile(r"\bMIT\b"),
    "Apache-2.0": re.compile(r"\bApache(?:-2\.0| 2\.0| License)\b"),
    "BSD-3-Clause": re.compile(r"\bBSD-3-Clause\b"),
    "MPL-2.0": re.compile(r"\bMPL-2\.0\b"),
}

#: A README's License section: the heading and what follows it up to the next heading.
SECTION = re.compile(r"^##+\s*Licen[cs]e\s*$\n(?P<body>.*?)(?=^#|\Z)", re.M | re.S)


def named(text: str) -> set[str]:
    """The licences *text* names."""
    return {spdx for spdx, pattern in LICENSES.items() if pattern.search(text)}


def problems(root: Path) -> tuple[list[str], int, int]:
    """``(problems, bundles read, README sections read)``."""
    found: list[str] = []
    bundles = sections = 0
    for manifest in sorted(root.glob("*/app.json")):
        bundle = manifest.parent
        bundles += 1
        declared = str(json.loads(manifest.read_text(encoding="utf-8")).get("license") or "")
        license_file = bundle / "LICENSE"
        if not license_file.is_file():
            found.append(f"{bundle.name}: no LICENSE file")
            continue
        first = (license_file.read_text(encoding="utf-8").strip().splitlines() or [""])[0]
        shipped = named(first)
        if shipped != {declared}:
            found.append(
                f"{bundle.name}: LICENSE says {'/'.join(sorted(shipped)) or first!r}, and "
                f"app.json says {declared or 'none'}"
            )
            continue
        readme = bundle / "README.md"
        section = SECTION.search(readme.read_text(encoding="utf-8")) if readme.is_file() else None
        if section is None:
            continue
        sections += 1
        said = named(section.group("body"))
        if said != {declared}:
            found.append(
                f"{bundle.name}: its README's License section says "
                f"{'/'.join(sorted(said)) or 'no licence this rail knows'}, and its LICENSE and "
                f"app.json say {declared}"
            )
    return found, bundles, sections


def _self_test() -> list[str]:
    """The detector against the shapes it tells apart."""
    shapes = [
        ("MIT License", {"MIT"}),
        ("MIT — see `LICENSE`.", {"MIT"}),
        ("MIT — see the apps repo [LICENSE](../LICENSE).", {"MIT"}),
        ("Apache-2.0. See `LICENSE`.", {"Apache-2.0"}),
        ("Licensed under the Apache License, Version 2.0", {"Apache-2.0"}),
        ("Apache 2.0", {"Apache-2.0"}),
        ("See `LICENSE`.", set()),
        ("SUBMITTED to the maintainers", set()),
    ]
    return [
        f"{text!r} read as {named(text)}, not {want}" for text, want in shapes if named(text) != want
    ]


def main() -> int:
    broken = _self_test()
    if broken:
        print("readme-license: the detector is broken:\n  " + "\n  ".join(broken))
        return 1
    found, bundles, sections = problems(ROOT)
    if bundles < MIN_BUNDLES or sections < MIN_SECTIONS:
        print(
            f"readme-license: read {bundles} app.json and {sections} README License sections, "
            f"under the floor of {MIN_BUNDLES} and {MIN_SECTIONS}: did the layout move?"
        )
        return 1
    if found:
        print("readme-license: FAIL\n  " + "\n  ".join(found))
        return 1
    print(f"OK: {bundles} app(s); {sections} README License section(s) say what LICENSE says")
    return 0


if __name__ == "__main__":
    sys.exit(main())
