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

An app's scheduled jobs (``crons``) are agent work too: each job's agent runs at the app's tier,
so an app that declares jobs, or the ``cron`` permission, names one, and core refuses it at
install when it does not.

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
        "sends, and ask for JSON or Markdown back; it schedules no job, which at text could read "
        "nothing and file nothing",
    ),
    "ops": (
        "tools",
        "its ten-minute sweep writes the incident ledger (ops_watch, ops_claim, ops_record, "
        "ops_propose_fix), and the read tier admits only tools that read",
    ),
    "research-lab": (
        "tools",
        "its hourly cycle writes the campaign (research_next, research_record, research_report) "
        "and starts a subagent per sub-question, none of which the read tier admits",
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
    assert manifest.permissions.agent_tier == tier
    assert [e for e in manifest.validate() if "permissions.agent" in e] == []


def test_every_app_that_schedules_jobs_names_the_tier_they_run_at():
    """Each job's agent runs at its app's tier, so jobs, or the permission for them, with no tier
    could never run, and core refuses such a manifest at install. Read from the manifests as
    written, so a failure names the app on any core."""
    scheduling = {}
    for manifest in sorted(ROOT.glob("*/app.json")):
        raw = json.loads(manifest.read_text(encoding="utf-8"))
        permissions = raw.get("permissions") or {}
        if raw.get("crons") or permissions.get("cron"):
            scheduling[manifest.parent.name] = permissions.get("agent")
    assert scheduling, "no app.json declares a scheduled job: the walk read nothing"
    untiered = {app: tier for app, tier in scheduling.items() if tier not in AGENT_TIERS}
    assert untiered == {}, f"apps that schedule jobs at no agent tier: {untiered}"


@pytest.mark.parametrize("written", [True, False, "yes"])
def test_a_value_that_names_no_tier_holds_none_and_only_false_is_accepted(written):
    """What this census relies on from core's parser, the one the Store installs with: ``true``, the
    boolean the tiers replaced, and any other value that names no tier hold no tier and are refused
    by name, while ``false`` declares no agent work and is accepted."""
    manifest = AppManifest.from_dict(
        {
            "name": "probe-app",
            "version": "1.0.0",
            "displayName": "Probe App",
            "description": "Declares an agent permission.",
            "permissions": {"agent": written},
        }
    )
    assert manifest.permissions.agent_tier == ""
    refusals = [e for e in manifest.validate() if "permissions.agent" in e]
    assert len(refusals) == (0 if written is False else 1), refusals
    assert "agent" not in manifest.permissions.to_dict()
