"""`spawn`, `bg` and `cron remove|pause|resume` are the owner's alone, as the README says.

The README gives an allowed user `!stop`, `!title`, `!compact` and `sessions`, and keeps everything
else the owner's. But the thread's `spawn <task>` and `bg <task>` keywords, and `cron remove`,
`pause` and `resume`, never looked at who sent them. An allowed user could delete, pause or resume
the owner's automations, and start a subagent before the turn named whose message it answered, so
the subagent's work was taken for the owner's own and her YOLO and Trust answered its calls. They
now answer the owner alone; anyone else is told they're the owner's, and nothing runs.

Driven through the app's handler; PersonalClaw's reading of who asked is real.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from slack_helpers import MockSlackClient, set_owner
from test_slack_handler import FakeProvider, FakeSessionManager

import slack_runtime.handler as H
from personalclaw.config.credentials import owner_id_credential
from personalclaw.config.loader import CRED_OWNER_ID
from personalclaw.sdk.channel import Trigger, TriggerStore, config_dir

OWNER = "U0MIRAOWNR"
COLLEAGUE = "U0JONASCOL"
CHANNEL = "C0EXAMPLE02"
THREAD = "1712793600.000500"
TRIGGER = "clock:test-job"


@pytest.fixture(autouse=True)
def _a_shared_channel(monkeypatch):
    """Slack knows its owner, and the owner allowed a colleague to talk to the agent."""
    monkeypatch.delenv(CRED_OWNER_ID, raising=False)
    monkeypatch.setenv(owner_id_credential("slack"), OWNER)
    set_owner(OWNER)
    H.set_allowed_users({OWNER, COLLEAGUE})
    yield
    set_owner("")
    H.set_allowed_users(set())


class _Subagents:
    """A subagent manager that keeps, for each spawn, whether the owner's YOLO would answer the
    work it starts: core's own reading of who asked for it, at the moment it is started."""

    max_concurrent = 3
    running: list = []

    def __init__(self) -> None:
        self.spawned: list[tuple[str, str]] = []

    def spawn(self, task, parent_session_key=""):
        from personalclaw import approval_grants, memory_writes

        asked_by = memory_writes.asked_for_work(parent_session_key)
        self.spawned.append((task, approval_grants.first_for([approval_grants.YOLO], asked_by)))
        return SimpleNamespace(id="sa-1")


def _store() -> TriggerStore:
    return TriggerStore(base_dir=config_dir())


def _seed() -> None:
    _store().upsert(
        Trigger(
            id=TRIGGER,
            name="test-job",
            kind="clock",
            enabled=True,
            spec={"kind": "cron", "expr": "0 13 * * *"},
            workflow={"inline": {"provider": "invoke-agent", "config": {"task_template": "x"}}},
            capabilities={"providers": ["invoke-agent"]},
        )
    )


async def _send(sender: str, text: str, subagents: _Subagents | None = None) -> MockSlackClient:
    slack = MockSlackClient()
    await H.handle_message(
        slack,
        FakeSessionManager(FakeProvider()),
        CHANNEL,
        text,
        THREAD,
        "m1",
        sender,
        subagent_manager=subagents,
    )
    return slack


def _said(slack: MockSlackClient) -> list[str]:
    return [str(d.get("text") or "") for kind, d in slack.actions if kind == "post"]


@pytest.mark.asyncio
@pytest.mark.parametrize("keyword", ["spawn", "bg"])
async def test_an_allowed_users_spawn_starts_nothing(keyword):
    """🔴 Red before: it started the subagent before the turn named who asked, so the owner's
    YOLO answered the work an allowed user asked for."""
    subagents = _Subagents()
    slack = await _send(COLLEAGUE, f"{keyword} tidy the release notes", subagents)

    assert subagents.spawned == []
    assert "⛔ Owner-only command." in _said(slack)


@pytest.mark.asyncio
async def test_the_owners_spawn_still_starts_her_subagent():
    subagents = _Subagents()
    await _send(OWNER, "spawn tidy the release notes", subagents)

    assert [task for task, _ in subagents.spawned] == ["tidy the release notes"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["remove", "pause", "resume"])
async def test_an_allowed_users_cron_command_changes_no_automation(action):
    """🔴 Red before: an allowed user could delete, pause or resume the owner's automations."""
    _seed()
    before = _store().get(TRIGGER)

    slack = await _send(COLLEAGUE, f"cron {action} {TRIGGER}")

    assert _store().get(TRIGGER) == before
    assert "⛔ Owner-only command." in _said(slack)


@pytest.mark.asyncio
async def test_the_owners_cron_remove_still_removes_it():
    _seed()
    await _send(OWNER, f"cron remove {TRIGGER}")

    assert _store().get(TRIGGER) is None
