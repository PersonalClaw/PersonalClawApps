"""The companion's reminders, its day brief and its watchlist go off while PersonalClaw runs.

The companion mints its rows with no next fire and leaves the arming and the firing to core. Core
armed such a row only when the gateway started, so a reminder asked for in chat while it ran sat on
the Automations page counting down, and never rang; its watch was never looked at, because core's
file-watch loop read core's own automations file alone; and the day brief waited for a restart.
And once core did arm a reminder, this store hid the row the moment core wrote down its fire, which
core writes BEFORE it runs one, so there was nothing left to run.

Each test drives core's own clock loop (its tick, dispatch, runner and recorder) or its file-watch
pass over this app's real trigger store, with a fixed clock and core's Dashboard Notification
action ringing a bell. No model, no network.
"""

from __future__ import annotations

import shutil
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from personalclaw.gateway import GatewayOrchestrator
from personalclaw.triggers import file_poll
from personalclaw.triggers import registry as TREG
from personalclaw.triggers import routing as ROUTE
from personalclaw.triggers import service as SVC
from personalclaw.triggers.store import TriggerStore

import companion as companion_mod
from provider import create_provider, create_trigger_store

ZONE = "Europe/Berlin"


class _Bell:
    """The dashboard, as far as a fire reaches it: the bell."""

    def __init__(self) -> None:
        self.notes: list[dict[str, Any]] = []

    def notify(self, kind, title, body="", *, meta=None, raised_by_app=""):
        self.notes.append({"kind": kind, "title": title, "body": body})

    def push_refresh(self, *keys: str) -> None:
        return None

    def broadcast_ws(self, *_a: Any, **_k: Any) -> None:
        return None

    @property
    def said(self) -> list[tuple[str, str]]:
        return [(note["title"], note["body"]) for note in self.notes]


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Core's home and this app's data dir, both the test's own."""
    pchome = tmp_path / "pchome"
    pchome.mkdir()
    monkeypatch.setenv("PERSONALCLAW_HOME", str(pchome))
    data = tmp_path / "companion-data"
    monkeypatch.setattr(companion_mod, "app_data_dir", lambda name: data)
    return pchome


@pytest.fixture
def bell(monkeypatch: pytest.MonkeyPatch) -> _Bell:
    from personalclaw.action_providers import notify_provider

    state = _Bell()
    monkeypatch.setattr(
        notify_provider, "get_action_services", lambda: SimpleNamespace(state=state)
    )
    return state


@pytest.fixture(autouse=True)
def _registry():
    """Core's provider registry and its write-back quarantine are process-wide."""
    for name in list(TREG.registered_stores()):
        TREG.unregister_trigger_store(name)
    ROUTE.clear_quarantine()
    yield
    for name in list(TREG.registered_stores()):
        TREG.unregister_trigger_store(name)
    ROUTE.clear_quarantine()


@pytest.fixture
def kitchen():
    """A real folder to watch, named so the watch-path grammar takes it (one segment per word)."""
    folder = Path("/tmp") / f"companion-watch-{uuid.uuid4().hex}" / "Kitchen"
    folder.mkdir(parents=True)
    yield folder
    shutil.rmtree(folder.parent, ignore_errors=True)


def _installed(settings: dict[str, Any]) -> Any:
    """The app's trigger store, registered with core as enabling the app registers it."""
    store = create_trigger_store(settings)
    TREG.register_trigger_store(store.name, store)
    return store


async def _tick_at(monkeypatch: pytest.MonkeyPatch, bell: _Bell, now: float) -> None:
    """One pass of core's clock loop at ``now``: its tick, dispatch, runner and recorder."""
    import personalclaw.triggers.loop as clock_loop

    tick_once = clock_loop.tick_once

    async def one_tick(store, *, runner, sessions=None, base_dir=None, on_missed=None, **_kw):
        await tick_once(
            store, runner=runner, sessions=sessions, base_dir=base_dir, now=now, on_missed=on_missed
        )

    monkeypatch.setattr(clock_loop, "run_forever", one_tick)
    orch = object.__new__(GatewayOrchestrator)
    orch.sessions = SimpleNamespace(_sessions={}, enqueue=lambda *_a, **_k: False)
    orch.dashboard_state = bell
    await orch._clock_loop()


def _in_zone(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=ZoneInfo(ZONE))


@pytest.mark.asyncio
async def test_a_reminder_set_while_personalclaw_runs_goes_off_at_its_time(
    home: Path, bell: _Bell, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 Before: nothing rang at its time, and the list then said it was delivered."""
    settings = {"reminders": True, "timezone": ZONE}
    store = _installed(settings)
    start = float(int(time.time()))
    SVC.boot(TriggerStore(base_dir=home), now=start)

    at = (_in_zone(start) + timedelta(minutes=10)).replace(second=0, microsecond=0)
    tools = create_provider(settings)
    made = await tools.invoke(
        "companion_remind", {"title": "Put the bins out", "at": at.strftime("%Y-%m-%dT%H:%M")}
    )
    assert made.success, made.error
    row_id = made.metadata["trigger_id"]

    await _tick_at(monkeypatch, bell, start + 5)
    armed = store.get(row_id)
    assert armed is not None and SVC.to_epoch(armed.trigger.next_fire_at) == at.timestamp()
    assert bell.said == []

    await _tick_at(monkeypatch, bell, at.timestamp() + 1)
    assert bell.said == [("Companion reminder", "Put the bins out")]
    # It went off, so it is an automation no more, and the list says it was delivered.
    assert store.get(row_id) is None
    listed = await tools.invoke("companion_list", {})
    assert "delivered" in listed.output
    assert ROUTE.quarantine_report() == {}
    assert [r.trigger.id for r in TriggerStore(base_dir=home).load()] == []

    await _tick_at(monkeypatch, bell, at.timestamp() + 120)
    assert len(bell.said) == 1, "it goes off once"


@pytest.mark.asyncio
async def test_a_day_brief_set_while_personalclaw_runs_nudges_at_its_hour(
    home: Path, bell: _Bell, monkeypatch: pytest.MonkeyPatch
) -> None:
    start = float(int(time.time()))
    SVC.boot(TriggerStore(base_dir=home), now=start)
    hour = (_in_zone(start) + timedelta(hours=1)).replace(second=0, microsecond=0)
    store = _installed({"day_brief": hour.strftime("%H:%M"), "timezone": ZONE})
    (row,) = store.load()

    await _tick_at(monkeypatch, bell, start + 5)
    assert SVC.to_epoch(store.get(row.trigger.id).trigger.next_fire_at) == hour.timestamp()
    await _tick_at(monkeypatch, bell, hour.timestamp() + 1)
    assert [title for title, _ in bell.said] == ["Day brief"]
    # The same hour tomorrow, kept in this app's store.
    after = SVC.to_epoch(store.get(row.trigger.id).trigger.next_fire_at)
    assert after > hour.timestamp() + 23 * 3600


@pytest.mark.asyncio
async def test_a_watched_folder_speaks_up_when_a_file_really_changes(
    home: Path, bell: _Bell, kitchen: Path
) -> None:
    """🔴 Before: core's watch loop never polled the app's watch, and a new PDF said nothing."""
    settings = {"watchlist": True, "timezone": ZONE}
    store = _installed(settings)
    (kitchen / "menu.pdf").write_bytes(b"%PDF-1.4 menu")
    made = await create_provider(settings).invoke(
        "companion_watch", {"path": f"{kitchen}/*.pdf", "label": "Kitchen PDFs"}
    )
    assert made.success, made.error
    gateway = object.__new__(GatewayOrchestrator)
    gateway.dashboard_state = bell

    # Core's watch pass, with the store its loop hands it: core's own.
    assert file_poll.poll_all(TriggerStore(base_dir=home)) == [], "its first look only records"
    (kitchen / "recipe.pdf").write_bytes(b"%PDF-1.4 recipe")
    fired = file_poll.poll_all(TriggerStore(base_dir=home))
    assert [p["trigger_id"] for p in fired] == [made.metadata["trigger_id"]]
    for payload in fired:
        await gateway._fire_file_trigger(payload)
    assert bell.said == [("Companion watch", "Kitchen PDFs")]
    assert store.get(made.metadata["trigger_id"]).trigger.run_count == 1
    assert file_poll.poll_all(TriggerStore(base_dir=home)) == [], "no change, no fire"
