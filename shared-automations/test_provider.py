"""Shared-automations store tests — pure filesystem, no gateway and no firing.

Every case drives a ``tmp_path`` file, so nothing reads or writes a real shared folder and nothing
touches a real ``triggers.json``.

What is covered here: the six-method store contract, the read-side tolerance (missing / malformed /
bare-list / broken-row), the WRITE-BACK round trip core depends on (``upsert`` really persists
``next_fire_at`` and ``get`` really reads it back — a store that failed this would be quarantined by
core rather than allowed to fire every tick), atomicity leftovers, the change-notification, and the
shipped example file including its ``alice`` rows.

What is NOT covered here, deliberately: whether core arms these rows. That is core's arm path, tested
at the ``TriggerService`` seam in the core repo's ``tests/test_triggers_ownership.py`` — an app test
asserting it would be asserting somebody else's code through a keyhole.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from provider import STORE_VERSION, SharedAutomationsStore, create_provider

from personalclaw.sdk.triggers import Trigger

FIXTURE = Path(__file__).with_name("team-automations.example.json")


def _row(tid="t1", *, author="", next_at="", kind="clock", **over):
    row = {
        "id": tid,
        "name": f"T-{tid}",
        "kind": kind,
        "enabled": True,
        "author": author,
        "spec": {"kind": "interval", "interval_secs": 3600},
        "workflow": {"provider": "run-prompt", "config": {"message": "go"}},
        "capabilities": {"providers": ["run-prompt"]},
        "next_fire_at": next_at,
    }
    row.update(over)
    return row


def _write(path: Path, rows, *, bare=False):
    payload = rows if bare else {"version": STORE_VERSION, "triggers": rows}
    path.write_text(json.dumps(payload), encoding="utf-8")


# ── the factory + the manifest's contract ────────────────────────────────────────────────


def test_the_factory_builds_from_settings_and_expands_the_path(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAMDIR", str(tmp_path))
    store = create_provider({"path": "$TEAMDIR/automations.json"})
    assert store.base_dir == tmp_path
    assert store.name == "shared-automations"


def test_an_unconfigured_store_constructs_and_serves_nothing():
    """An app whose setting is not filled in yet must install, not crash — and contribute no rows."""
    store = create_provider({})
    assert store.load() == []
    assert store.list_triggers() == []
    assert store.get("anything") is None
    assert store.changed_on_disk() is False


def test_the_store_exposes_every_method_core_calls():
    store = create_provider({})
    for method in ("load", "list_triggers", "get", "upsert", "delete", "changed_on_disk"):
        assert callable(getattr(store, method)), method
    assert isinstance(store.base_dir, Path)


# ── reads ────────────────────────────────────────────────────────────────────────────────


def test_load_returns_rows_with_their_authors(tmp_path):
    path = tmp_path / "a.json"
    _write(path, [_row("mine"), _row("hers", author="alice")])
    store = SharedAutomationsStore(str(path))
    rows = store.load()
    assert [r.trigger.id for r in rows] == ["mine", "hers"]
    assert [r.trigger.author for r in rows] == ["", "alice"]
    assert all(r.ok for r in rows)


def test_a_missing_file_serves_no_rows_rather_than_raising(tmp_path):
    """An unmounted synced folder must cost this app's rows and nothing else."""
    store = SharedAutomationsStore(str(tmp_path / "not-there.json"))
    assert store.load() == []


def test_a_malformed_file_serves_no_rows_rather_than_raising(tmp_path):
    path = tmp_path / "a.json"
    path.write_text("{ this is not json", encoding="utf-8")
    assert SharedAutomationsStore(str(path)).load() == []


def test_a_bare_json_list_is_read_too(tmp_path):
    """So a team can share a copy of somebody's triggers.json unchanged."""
    path = tmp_path / "a.json"
    _write(path, [_row("bare")], bare=True)
    assert [r.trigger.id for r in SharedAutomationsStore(str(path)).load()] == ["bare"]


def test_a_broken_row_stays_visible_and_inert(tmp_path):
    """Dropping it would make a typo indistinguishable from a trigger nobody created."""
    path = tmp_path / "a.json"
    _write(path, [_row("broken", spec={"kind": "cron", "cron": "not a cron"})])
    rows = SharedAutomationsStore(str(path)).load()
    assert len(rows) == 1
    assert rows[0].errors
    assert rows[0].trigger.enabled is False


def test_list_triggers_filters_by_kind_and_can_exclude_broken(tmp_path):
    path = tmp_path / "a.json"
    _write(
        path,
        [
            _row("ok"),
            _row("other", kind="file", spec={"path": str(tmp_path)}),
            _row("bad", spec={"kind": "cron", "cron": "nope"}),
        ],
    )
    store = SharedAutomationsStore(str(path))
    assert [t.id for t in store.list_triggers(kind="clock")] == ["ok", "bad"]
    assert [t.id for t in store.list_triggers(include_broken=False)] == ["ok", "other"]


def test_get_finds_one_row_and_returns_none_for_the_rest(tmp_path):
    path = tmp_path / "a.json"
    _write(path, [_row("here")])
    store = SharedAutomationsStore(str(path))
    assert store.get("here").trigger.id == "here"
    assert store.get("elsewhere") is None
    assert store.get("") is None


# ── write-back: the property core verifies ───────────────────────────────────────────────


def test_upsert_persists_next_fire_at_and_get_reads_it_back(tmp_path):
    """THE contract behind an app-served row firing once instead of every tick.

    Core writes the advanced schedule here and then re-reads the row to confirm the timestamp moved.
    A store that accepted this write and kept the old value is quarantined by core — so this test is
    the difference between an app whose automations fire and one whose rows go quiet after one fire.
    """
    path = tmp_path / "a.json"
    _write(path, [_row("sched", next_at="2026-08-18T09:00:00+00:00")])
    store = SharedAutomationsStore(str(path))

    row = store.get("sched").trigger
    row.next_fire_at = "2026-08-18T10:00:00+00:00"
    row.run_count = 1
    row.last_fired_at = "2026-08-18T09:00:01+00:00"
    store.upsert(row)

    reread = SharedAutomationsStore(str(path)).get("sched").trigger
    assert reread.next_fire_at == "2026-08-18T10:00:00+00:00"
    assert reread.run_count == 1
    assert reread.last_fired_at == "2026-08-18T09:00:01+00:00"


def test_upsert_replaces_by_id_rather_than_appending_a_second_row(tmp_path):
    """Two rows under one id in a shared file is the divergence this store must never create."""
    path = tmp_path / "a.json"
    _write(path, [_row("dup", next_at="2026-01-01T00:00:00+00:00")])
    store = SharedAutomationsStore(str(path))
    row = store.get("dup").trigger
    row.next_fire_at = "2027-01-01T00:00:00+00:00"
    store.upsert(row)
    assert len(store.load()) == 1
    assert store.get("dup").trigger.next_fire_at == "2027-01-01T00:00:00+00:00"


def test_upsert_creates_the_file_and_its_parents(tmp_path):
    path = tmp_path / "nested" / "deeper" / "a.json"
    store = SharedAutomationsStore(str(path))
    store.upsert(Trigger(id="fresh", name="Fresh", kind="clock"))
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == STORE_VERSION
    assert store.get("fresh").trigger.id == "fresh"


def test_upsert_leaves_no_temp_file_behind(tmp_path):
    """A synced folder mid-write is worse than a missing one, so the write is a rename."""
    path = tmp_path / "a.json"
    store = SharedAutomationsStore(str(path))
    store.upsert(Trigger(id="x", name="X", kind="clock"))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.json"]


def test_upsert_on_an_unconfigured_store_refuses_loudly(tmp_path):
    """Silently swallowing the write is the one failure that costs the owner a fire."""
    store = create_provider({})
    try:
        store.upsert(Trigger(id="x", name="X", kind="clock"))
    except RuntimeError as exc:
        assert "no file configured" in str(exc)
    else:  # pragma: no cover - the assertion above is the point
        raise AssertionError("an unconfigured store must refuse a write, not pretend")


def test_delete_removes_the_row_and_reports_whether_it_was_there(tmp_path):
    path = tmp_path / "a.json"
    _write(path, [_row("gone"), _row("stays")])
    store = SharedAutomationsStore(str(path))
    assert store.delete("gone") is True
    assert store.delete("gone") is False
    assert [t.id for t in store.list_triggers()] == ["stays"]


def test_delete_really_removes_it_from_disk(tmp_path):
    """Core re-reads after a delete: a row left behind holding an elapsed schedule is a storm."""
    path = tmp_path / "a.json"
    _write(path, [_row("retire")])
    SharedAutomationsStore(str(path)).delete("retire")
    assert SharedAutomationsStore(str(path)).get("retire") is None


# ── the change notification ──────────────────────────────────────────────────────────────


def test_changed_on_disk_reports_a_write_by_someone_else(tmp_path):
    path = tmp_path / "a.json"
    _write(path, [_row("one")])
    store = SharedAutomationsStore(str(path))
    store.load()
    assert store.changed_on_disk() is False
    # A teammate's machine (or a git pull) replaces the file with a different one.
    _write(path, [_row("one"), _row("two")])
    os.utime(path, (1_800_000_000, 1_800_000_000))
    assert store.changed_on_disk() is True


def test_our_own_write_is_not_reported_as_someone_elses(tmp_path):
    path = tmp_path / "a.json"
    _write(path, [_row("one")])
    store = SharedAutomationsStore(str(path))
    store.load()
    store.upsert(Trigger(id="two", name="Two", kind="clock"))
    assert store.changed_on_disk() is False


# ── the shipped example ──────────────────────────────────────────────────────────────────


def test_the_example_file_parses_cleanly(tmp_path):
    store = SharedAutomationsStore(str(FIXTURE))
    rows = store.load()
    assert rows, "the shipped example must contain rows"
    broken = [r.trigger.id for r in rows if not r.ok]
    assert broken == [], f"the shipped example has unparseable rows: {broken}"


def test_the_example_covers_a_workflow_an_agent_a_prompt_an_action_and_a_chain():
    """The four things a shared automation can fire, plus one automation chained off another."""
    rows = SharedAutomationsStore(str(FIXTURE)).load()
    providers = {r.trigger.workflow.get("provider") for r in rows if r.trigger.author == ""}
    assert {"run-workflow", "invoke-agent", "run-prompt", "create-task", "notify"} <= providers
    kinds = {r.trigger.kind for r in rows if r.trigger.author == ""}
    assert "run_completed" in kinds


def test_the_example_ships_alice_rows_attributed_to_alice():
    """The second-username fixtures: present and named, so a reader can see what read-only means.

    They are ATTRIBUTED (``author: "alice"``) rather than merely named "alice…" — attribution is what
    core filters on. An unattributed row reads as the local owner's on every machine, which is the
    bargain stated in the SDK: a store that wants its rows treated as somebody else's must say whose.
    """
    rows = SharedAutomationsStore(str(FIXTURE)).load()
    alice = [r.trigger for r in rows if r.trigger.author == "alice"]
    assert len(alice) >= 2
    assert all(t.enabled for t in alice), "they must be ENABLED — inert by ownership, not by a toggle"
    mine = [r.trigger for r in rows if r.trigger.author == ""]
    assert mine, "the example must also contain rows the local owner can actually run"
