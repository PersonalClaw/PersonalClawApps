"""Every first-party app's agent tier is the least its agent tasks need, and is named here.

An app's ``permissions.agent`` names what its agent work may use (core's ``AGENT_TIERS``): ``text``
hands the model the task the app sends and nothing else, with no tools; ``read`` gives an agent
read-only tools; ``tools`` gives it the owner's tools, each call that needs approval asking her.
Install consent words each tier on its own, so the tier an app declares is what the owner agrees
to. It used to be one boolean, and holding it gave an app's agents every tool with nothing asked,
for apps whose tasks only ever summarise the text they send.

This census lists the first-party apps that run agent work, each with the tier its tasks need and
why. A new declaration, a dropped one or a changed one fails here until the census says why, so a
tier never widens without someone reading what the app's tasks use. The ``manifest-validate`` job
refuses a value that names no tier (``true`` among them) with core's own parser; this checks the
same per app, so a failure names the app.

Run locally exactly as CI does:

    python -m pytest .github/tests/test_agent_tiers.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from personalclaw.sdk.manifest import AGENT_TIERS, AppManifest

ROOT = Path(__file__).resolve().parents[2]

#: Each first-party app that runs agent work: the tier it declares, and why that is the least its
#: agent tasks need. Read the app's task code before changing a row.
CENSUS: dict[str, tuple[str, str]] = {
    "minutes": (
        "text",
        "its minutes and its extraction hand the model the meeting's notes and the text the page "
        "already read from the meeting's items, and ask for text or JSON back",
    ),
    "growth": (
        "text",
        "its artifact draft and its digest hand the model the evidence and the artifacts the page "
        "sends, and ask for JSON or Markdown back",
    ),
}


def _declared() -> dict[str, object]:
    """Each app whose manifest names ``permissions.agent``, with the value as written."""
    found: dict[str, object] = {}
    for manifest in sorted(ROOT.glob("*/app.json")):
        raw = json.loads(manifest.read_text(encoding="utf-8"))
        permissions = raw.get("permissions") or {}
        if "agent" in permissions:
            found[manifest.parent.name] = permissions["agent"]
    return found


def test_the_census_is_every_app_that_runs_agent_work():
    declared = _declared()
    assert declared, "no app.json was found declaring agent work: the walk read nothing"
    assert sorted(declared) == sorted(CENSUS), (
        "the apps that declare permissions.agent changed; add or drop the census row, with the "
        f"tier the app's tasks need and why — declared: {declared}"
    )


@pytest.mark.parametrize("app", sorted(CENSUS))
def test_each_app_declares_the_tier_its_tasks_need(app):
    tier, why = CENSUS[app]
    assert tier in AGENT_TIERS and why, (app, tier)
    raw = json.loads((ROOT / app / "app.json").read_text(encoding="utf-8"))
    assert raw["permissions"]["agent"] == tier, (
        f"{app} declares agent {raw['permissions']['agent']!r}; its tasks need {tier!r} ({why})"
    )
    manifest = AppManifest.from_dict(raw)
    assert manifest.permissions.agent == tier
    assert [e for e in manifest.validate() if "permissions.agent" in e] == []
