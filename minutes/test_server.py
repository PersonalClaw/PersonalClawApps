"""Tests for the redesigned Minutes backend — composite meeting + participants + outputs + extractions."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PERSONALCLAW_APP_DATA_DIR", str(tmp_path))
    sys.path.insert(0, str(Path(__file__).parent / "backend"))
    if "server" in sys.modules:
        del sys.modules["server"]
    server = importlib.import_module("server")
    importlib.reload(server)
    c = TestClient(TestServer(server.make_app()))
    await c.start_server()
    yield c
    await c.close()


async def test_health(client):
    assert (await (await client.get("/health")).json())["ok"] is True


async def test_meeting_composite_shape(client):
    m = await (await client.post("/meetings", json={"title": "Q3 Planning", "tags": ["planning"]})).json()
    assert m["id"].startswith("mtg_")
    # composite shape: participants + counts present from the start
    assert m["participants"] == [] and m["output_count"] == 0 and m["open_action_count"] == 0
    assert m["project_id"] == "" and m["notes"] == ""
    # patch project binding + notes
    m2 = await (await client.patch(f"/meetings/{m['id']}", json={"project_id": "p-1", "notes": "hi"})).json()
    assert m2["project_id"] == "p-1" and m2["notes"] == "hi"


async def test_members_add_remove(client):
    mid = (await (await client.post("/meetings", json={"title": "M"})).json())["id"]
    m = await (await client.post(f"/meetings/{mid}/members", json={"item_id": "kn-1", "role": "recording"})).json()
    assert m["member_ids"] == ["kn-1"] and m["member_roles"]["kn-1"] == "recording"
    await client.delete(f"/meetings/{mid}/members/kn-1")
    m = await (await client.get(f"/meetings/{mid}")).json()
    assert m["member_ids"] == []


async def test_participants_crud_speaker_and_roster(client):
    mid = (await (await client.post("/meetings", json={"title": "M"})).json())["id"]
    p = await (await client.post(f"/meetings/{mid}/participants",
                                 json={"name": "Jordan", "speaker_label": "SPEAKER_00", "role": "manager",
                                       "entity_ref": "Jordan"})).json()
    assert p["id"].startswith("prt_") and p["speaker_label"] == "SPEAKER_00" and p["entity_ref"] == "Jordan"
    # roster picked up the name
    roster = (await (await client.get("/roster")).json())["roster"]
    assert "Jordan" in {r["name"] for r in roster}
    # re-map the speaker label
    p2 = await (await client.patch(f"/meetings/{mid}/participants/{p['id']}", json={"speaker_label": "SPEAKER_01"})).json()
    assert p2["speaker_label"] == "SPEAKER_01"
    # meeting reflects the participant
    m = await (await client.get(f"/meetings/{mid}")).json()
    assert len(m["participants"]) == 1
    await client.delete(f"/meetings/{mid}/participants/{p['id']}")
    assert (await (await client.get(f"/meetings/{mid}/participants")).json())["participants"] == []


async def test_builtin_templates_seeded_and_copy_on_edit(client):
    tpls = (await (await client.get("/templates")).json())["templates"]
    assert "standard-minutes" in {t["id"] for t in tpls}
    forked = await (await client.patch("/templates/standard-minutes", json={"name": "My Minutes"})).json()
    assert forked["builtin"] is False and forked["id"] != "standard-minutes"
    bi = next(t for t in (await (await client.get("/templates")).json())["templates"] if t["id"] == "standard-minutes")
    assert bi["name"] == "Standard Minutes"  # built-in intact


async def test_builtin_not_deletable_custom_is(client):
    assert (await client.delete("/templates/standard-minutes")).status == 400
    t = await (await client.post("/templates", json={"name": "Retro", "prompt": "recap"})).json()
    await client.delete(f"/templates/{t['id']}")
    assert not any(x["id"] == t["id"] for x in (await (await client.get("/templates")).json())["templates"])


async def test_outputs_multiple_per_meeting_edit_delete(client):
    mid = (await (await client.post("/meetings", json={"title": "M"})).json())["id"]
    assert (await client.post(f"/meetings/{mid}/outputs", json={"content_md": "  "})).status == 400  # empty rejected
    o1 = await (await client.post(f"/meetings/{mid}/outputs", json={"template_name": "Standard Minutes",
                                  "content_md": "## Minutes\nDid things.", "title": "Full minutes",
                                  "action_items": [{"id": "ai_1", "text": "Ben drafts RFC", "task_id": None}]})).json()
    o2 = await (await client.post(f"/meetings/{mid}/outputs", json={"template_name": "Action Items",
                                  "content_md": "- do X"})).json()
    outs = (await (await client.get(f"/meetings/{mid}/outputs")).json())["outputs"]
    assert len(outs) == 2  # MULTIPLE outputs per meeting
    assert (await (await client.get(f"/meetings/{mid}")).json())["output_count"] == 2
    # edit marks edited
    e = await (await client.patch(f"/meetings/{mid}/outputs/{o1['id']}", json={"content_md": "edited"})).json()
    assert e["edited"] is True and e["content_md"] == "edited"
    await client.delete(f"/meetings/{mid}/outputs/{o2['id']}")
    assert len((await (await client.get(f"/meetings/{mid}/outputs")).json())["outputs"]) == 1


async def test_extractions_consolidated_and_task_link(client):
    mid = (await (await client.post("/meetings", json={"title": "M"})).json())["id"]
    # bulk-add a mixed batch (the generate step posts dates+actions+followups+decisions)
    r = await (await client.post(f"/meetings/{mid}/extractions", json={"items": [
        {"kind": "date", "text": "Launch review 2026-08-01"},
        {"kind": "action", "text": "Ben drafts the RFC", "assignee": "Ben", "due": "2026-07-20"},
        {"kind": "followup", "text": "Circle back on budget"},
        {"kind": "decision", "text": "Adopt the new caching layer"},
        {"kind": "bogus", "text": "ignored"},          # unknown kind dropped
        {"kind": "action", "text": ""},                 # empty dropped
    ]})).json()
    assert r["added"] == 4
    exts = (await (await client.get(f"/meetings/{mid}/extractions")).json())["extractions"]
    assert {e["kind"] for e in exts} == {"date", "action", "followup", "decision"}
    assert (await (await client.get(f"/meetings/{mid}")).json())["open_action_count"] == 1
    # link an action to a created task + mark done
    action = next(e for e in exts if e["kind"] == "action")
    e2 = await (await client.patch(f"/meetings/{mid}/extractions/{action['id']}", json={"task_id": "t-42", "done": True})).json()
    assert e2["task_id"] == "t-42" and e2["done"] is True
    assert (await (await client.get(f"/meetings/{mid}")).json())["open_action_count"] == 0
    await client.delete(f"/meetings/{mid}/extractions/{action['id']}")
    assert len((await (await client.get(f"/meetings/{mid}/extractions")).json())["extractions"]) == 3


async def test_delete_meeting_cascades(client):
    mid = (await (await client.post("/meetings", json={"title": "M"})).json())["id"]
    await client.post(f"/meetings/{mid}/participants", json={"name": "A"})
    await client.post(f"/meetings/{mid}/outputs", json={"content_md": "x"})
    await client.post(f"/meetings/{mid}/extractions", json={"items": [{"kind": "date", "text": "d"}]})
    await client.delete(f"/meetings/{mid}")
    assert (await (await client.get(f"/meetings/{mid}/participants")).json())["participants"] == []
    assert (await (await client.get(f"/meetings/{mid}/outputs")).json())["outputs"] == []
    assert (await (await client.get(f"/meetings/{mid}/extractions")).json())["extractions"] == []
