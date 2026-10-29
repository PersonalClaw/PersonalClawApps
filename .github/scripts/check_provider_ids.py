#!/usr/bin/env python3
"""Every provider a first-party app declares has an id and a label, and no released id is lost.

Each provider is a capability that core keeps under ``<app>/<id>`` (PersonalClaw/PersonalClaw#3841):
its settings, secrets, switch and status are keyed by the id, so an id that changes or disappears
without saying so takes them with it. The label is the name the owner reads for it, in the Store,
the app's panel, Settings and consent.

The rules, each failing with the app and the field:

  R1  Every provider entry, the singular ``provider`` and each of ``providers[]``, has an ``id`` and
      a ``label``, and the id is kebab-case.
  R2  Ids are unique within the app.
  R3  A label is unique within the app, and is neither its id nor a bare provider type: it says in
      plain words what the capability does. The type words are the types the tree's manifests
      declare, as written and spaced: core's full list (``PROVIDER_TYPES``) is not in its SDK, and
      ``check_boundary`` lets this repository reach core only through ``personalclaw.sdk``. Core's
      own ``validate`` checks every type it knows.
  R4  The shape the released core reads (2026-10-28): the first provider stays in the singular
      ``provider`` and ``providers[]`` holds only the others. An entry that repeats the singular
      one (its id, or its type and implementation) would be registered twice there.
      PersonalClaw/PersonalClaw#5665 moves the first into ``providers[]`` at the v0.4.0 cut.
  R5  An id in ``retiredCapabilities`` is never declared again, and an old id is renamed into one
      capability only.
  R6  No released id is lost: each provider of the app's manifest at the latest ``v*`` release tag
      maps onto exactly one entry now, by the same id or by ``renamedFrom``, or is retired in
      ``retiredCapabilities``. A provider released without an id is read with the id core derives
      for it (:data:`DERIVED`, PersonalClaw/PersonalClaw#3841). The tag is read with git, so no
      change to a manifest can also change what was released. CI's ``static`` job fetches the tag
      (its checkout is shallow), and a checkout with no ``v*`` tag fails here, saying how to fetch
      one, rather than skipping the rule.
  R7  Owner-facing text names a capability by its label: a README, or a setting's title or
      description, that shows an ``<app>/<id>`` key fails.

Run from the repository root:  python .github/scripts/check_provider_ids.py
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

KEBAB = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
RELEASE_TAG = re.compile(r"v\d+(?:\.\d+)*")

#: The id core gives a provider declared without one, by type: core's
#: ``capability_ids.DERIVED_CAPABILITY_IDS`` (PersonalClaw/PersonalClaw#3841), the words these
#: apps' ids use. Ids are permanent, so this list only ever grows, with a new type.
DERIVED = {
    "action": "action",
    "agent": "agent",
    "channel": "channel",
    "inbox": "inbox",
    "knowledge": "knowledge",
    "memory": "memory",
    "model": "model",
    "notification": "notifications",
    "ocr": "ocr",
    "prompt": "prompts",
    "sandbox": "sandbox",
    "search": "search",
    "skills": "skills",
    "sync": "sync",
    "task": "tasks",
    "tool": "tools",
    "trigger": "automations",
    "trigger_source": "triggers",
    "vector_store": "vector-store",
    "workflow": "workflows",
}

FETCH_HINT = (
    "fetch the latest release tag first, as CI's static job does: "
    'git fetch --depth 1 --no-tags origin "refs/tags/<tag>:refs/tags/<tag>"'
)

#: Vacuity floor: 65 apps declare a provider today. A broken glob or parser fails here.
_MIN_APPS = 50


def _type_words(types: set[str]) -> set[str]:
    return {t.replace("_", " ") for t in types} | set(types)


def entries(manifest: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Each provider entry with the path that names it, the singular one first."""
    out: list[tuple[str, dict[str, Any]]] = []
    if isinstance(manifest.get("provider"), dict):
        out.append(("provider", manifest["provider"]))
    for i, entry in enumerate(manifest.get("providers") or []):
        if isinstance(entry, dict):
            out.append((f"providers[{i}]", entry))
    return out


def released_ids(manifest: dict[str, Any]) -> list[str]:
    """A released manifest's ids: each declared one, else the one core derives from its type."""
    out = []
    for _path, entry in entries(manifest):
        kind = str(entry.get("type") or "")
        out.append(str(entry.get("id") or DERIVED.get(kind, kind.replace("_", "-"))))
    return out


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )


def latest_tag(root: Path) -> str:
    """The latest ``v*`` release tag the checkout at *root* has, or ``""``."""
    out = _git(root, "tag", "--list", "v*", "--sort=-version:refname")
    tags = [t for t in out.stdout.split() if RELEASE_TAG.fullmatch(t)]
    return tags[0] if out.returncode == 0 and tags else ""


def released_manifest(root: Path, tag: str, app: str) -> dict[str, Any] | None:
    """*app*'s manifest at *tag*, or ``None`` when the app was not released then."""
    out = _git(root, "show", f"{tag}:{app}/app.json")
    return json.loads(out.stdout) if out.returncode == 0 else None


def _settings_text(manifest: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    for _path, entry in entries(manifest):
        props = (entry.get("settingsSchema") or {}).get("properties") or {}
        for spec in props.values():
            meta = spec.get("x-meta") or {}
            texts += [str(spec.get("title", "")), str(spec.get("description", ""))]
            texts += [str(meta.get("label", "")), str(meta.get("help", ""))]
    return texts


def app_problems(
    app: str,
    manifest: dict[str, Any],
    readme: str,
    released: dict[str, Any] | None,
    tag: str,
    types: set[str],
) -> list[str]:
    """Every rule *app*'s manifest breaks, each a sentence naming the field."""
    found: list[str] = []
    listed = entries(manifest)
    ids: dict[str, str] = {}
    labels: dict[str, str] = {}
    type_words = _type_words(types | {str(e.get("type") or "") for _p, e in listed})
    for path, entry in listed:
        cid, label = str(entry.get("id") or ""), str(entry.get("label") or "")
        if not cid:
            found.append(f"{app}: {path} has no id")
        elif not KEBAB.match(cid):
            found.append(f"{app}: {path}.id {cid!r} is not kebab-case")
        elif cid in ids:
            found.append(f"{app}: {path}.id {cid!r} is already {ids[cid]}'s")
        if not label:
            found.append(f"{app}: {path} has no label")
        else:
            folded = label.casefold()
            if folded == cid.casefold():
                found.append(f"{app}: {path}.label is its id: say what it does")
            elif folded in type_words:
                found.append(f"{app}: {path}.label {label!r} is a provider type: say what it does")
            if folded in labels:
                found.append(f"{app}: {path}.label repeats {labels[folded]}.label")
            labels.setdefault(folded, path)
        if cid:
            ids.setdefault(cid, path)
    found += _shape_problems(app, listed)
    found += _tombstone_problems(app, manifest, listed, set(ids), released, tag)
    for cid in ids:
        key = f"{app}/{cid}"
        if key in readme or any(key in text for text in _settings_text(manifest)):
            found.append(f"{app}: owner-facing text shows the key {key!r}: name it by its label")
    return found


def _shape_problems(app: str, listed: list[tuple[str, dict[str, Any]]]) -> list[str]:
    if not listed or listed[0][0] != "provider":
        return [f"{app}: the first provider must stay in the singular `provider` (#5665 moves it)"]
    first = listed[0][1]
    found = []
    for path, entry in listed[1:]:
        same_id = entry.get("id") and entry.get("id") == first.get("id")
        same_code = (entry.get("type"), entry.get("implementation")) == (
            first.get("type"),
            first.get("implementation"),
        )
        if same_id or same_code:
            found.append(f"{app}: {path} repeats the singular provider; list only the others")
    return found


def _tombstone_problems(
    app: str,
    manifest: dict[str, Any],
    listed: list[tuple[str, dict[str, Any]]],
    declared: set[str],
    released: dict[str, Any] | None,
    tag: str,
) -> list[str]:
    found = []
    retired = {str(r) for r in manifest.get("retiredCapabilities") or []}
    renamed_by: dict[str, str] = {}
    for path, entry in listed:
        for old in entry.get("renamedFrom") or []:
            if old in renamed_by:
                found.append(
                    f"{app}: {path}.renamedFrom names {old!r}, also renamed by {renamed_by[old]}"
                )
            renamed_by.setdefault(old, path)
    for cid in sorted(declared & retired):
        found.append(f"{app}: the id {cid!r} is retired (retiredCapabilities) and declared again")
    for cid in released_ids(released) if released else []:
        if cid not in declared and cid not in renamed_by and cid not in retired:
            found.append(
                f"{app}: the id {cid!r} released in {tag} is gone with no renamedFrom or "
                "retiredCapabilities entry"
            )
    return found


def problems(root: Path) -> tuple[list[str], int]:
    """Every problem in the tree at *root*, and how many apps declare a provider."""
    tag = latest_tag(root)
    if not tag:
        return [f"no v* release tag in {root}: {FETCH_HINT}"], 0
    manifests = {
        p.parent.name: json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(root.glob("*/app.json"))
    }
    types = {str(e.get("type") or "") for m in manifests.values() for _p, e in entries(m)}
    found: list[str] = []
    counted = 0
    for app, manifest in manifests.items():
        released = released_manifest(root, tag, app)
        if not entries(manifest):
            if released and entries(released):
                found.append(
                    f"{app}: declares no provider, but {tag} released {released_ids(released)}"
                )
            continue
        counted += 1
        readme = root / app / "README.md"
        text = readme.read_text(encoding="utf-8") if readme.is_file() else ""
        found += app_problems(app, manifest, text, released, tag, types)
    return found, counted


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    found, counted = problems(root)
    if counted < _MIN_APPS and counted:
        found.append(f"only {counted} apps declare a provider (expected at least {_MIN_APPS})")
    for line in found:
        print(line)
    print(f"provider-ids: {counted} apps, {len(found)} problem(s)")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
