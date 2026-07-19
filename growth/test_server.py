"""Tests for the redesigned Growth Tracker backend — artifacts + growth areas + rubric lens."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PERSONALCLAW_APP_DATA_DIR", str(tmp_path))
    sys.path.insert(0, str(Path(__file__).parent / "backend"))
    if "server" in sys.modules:
        del sys.modules["server"]
    server = importlib.import_module("server")
    importlib.reload(server)
    c = TestClient(TestServer(server.make_app()))
    await c.start_server()
    yield c, server
    await c.close()


async def test_health(env):
    c, _ = env
    assert (await (await c.get("/health")).json())["ok"] is True


async def test_default_rubric_is_neutral(env):
    c, _ = env
    d = await (await c.get("/rubric")).json()
    assert d["is_override"] is False
    assert "Execution" in d["rubric"]["dimensions"]
    assert not any("SDE" in dim or "L4" in dim for dim in d["rubric"]["dimensions"])


async def test_classify_maps_keywords(env):
    _, server = env
    dims = server.classify("Shipped and released the migration; reduced p99 latency")
    assert "Execution" in dims and "Impact" in dims


async def test_sourced_detection_via_evidence_link(env):
    _, server = env
    # a PClaw-source evidence link (non-external) is inherently sourced
    assert server.is_sourced("did a thing", [{"kind": "project", "ref": "p-1", "label": "X"}]) is True
    # external URL / PR ref in text also counts
    assert server.is_sourced("shipped PR #42", []) is True
    assert server.is_sourced("just chatted", [{"kind": "external", "ref": "", "label": ""}]) is False


async def test_artifact_crud_autoclassify_and_evidence(env):
    c, _ = env
    r = await c.post("/artifacts", json={
        "title": "Drove the caching RFC to alignment",
        "behavior": "wrote and presented the RFC; the org aligned",
        "impact": "unblocked three downstream teams",
        "evidence": [{"kind": "chat", "ref": "chat-1-abc", "label": "RFC session"},
                     {"kind": "external", "ref": "https://wiki/rfc", "label": "doc"}]})
    a = await r.json()
    assert a["id"].startswith("a_")
    assert a["sourced"] is True                      # chat evidence link
    assert a["dimensions"]                            # auto-classified
    assert a["period"]                                # derived quarter
    assert len(a["evidence"]) == 2
    aid = a["id"]
    lst = (await (await c.get("/artifacts")).json())["artifacts"]
    assert any(x["id"] == aid for x in lst)
    # patch re-classifies + preserves evidence
    a2 = await (await c.patch(f"/artifacts/{aid}", json={"impact": "reduced load, improved adoption"})).json()
    assert "Impact" in a2["dimensions"]
    await c.delete(f"/artifacts/{aid}")
    assert (await (await c.get("/artifacts")).json())["artifacts"] == []


async def test_artifact_requires_title(env):
    c, _ = env
    assert (await c.post("/artifacts", json={"behavior": "x"})).status == 400


async def test_growth_areas_crud_and_linking(env):
    c, _ = env
    ga = await (await c.post("/areas", json={"name": "Cross-team influence",
                                             "target": "Lead an org-wide initiative",
                                             "dimension": "Scope & Influence"})).json()
    assert ga["id"].startswith("ga_")
    gid = ga["id"]
    # link two artifacts to the area
    for i in range(2):
        await c.post("/artifacts", json={"title": f"influence work {i}", "area_id": gid,
                                         "behavior": "aligned the org"})
    areas = (await (await c.get("/areas")).json())["areas"]
    mine = next(a for a in areas if a["id"] == gid)
    assert mine["artifact_count"] == 2
    # filter artifacts by area
    linked = (await (await c.get(f"/artifacts?area_id={gid}")).json())["artifacts"]
    assert len(linked) == 2
    # deleting the area unlinks artifacts but keeps them
    await c.delete(f"/areas/{gid}")
    assert (await (await c.get("/areas")).json())["areas"] == []
    still = (await (await c.get("/artifacts")).json())["artifacts"]
    assert len(still) == 2 and all(x["area_id"] == "" for x in still)


async def test_sources_dismiss_roundtrip(env):
    c, _ = env
    assert (await (await c.get("/dismissed")).json())["dismissed"] == []
    await c.post("/dismissed", json={"ref": "project:p-9"})
    await c.post("/dismissed", json={"ref": "project:p-9"})  # idempotent
    d = (await (await c.get("/dismissed")).json())["dismissed"]
    assert d == ["project:p-9"]
    assert (await c.post("/dismissed", json={})).status == 400


async def test_readiness(env):
    c, _ = env
    for i in range(3):
        await c.post("/artifacts", json={"title": f"shipped feature {i}",
                                         "impact": "improved adoption",
                                         "evidence": [{"kind": "task", "ref": f"t-{i}", "label": "task"}],
                                         "date": f"2026-0{i+1}-15"})
    readiness = await (await c.get("/readiness")).json()
    assert "dimensions" in readiness and 0 <= readiness["overall_pct"] <= 100
    ex = next(d for d in readiness["dimensions"] if d["dimension"] == "Execution")
    assert ex["actual"] >= 3 and ex["status"] == "Consistent"


async def test_digest_requires_content_and_delete(env):
    c, _ = env
    assert (await c.post("/digests", json={"period": "2026-Q3", "content_md": "  "})).status == 400
    d = await (await c.post("/digests", json={"period": "2026-Q3", "content_md": "# Done"})).json()
    assert d["id"].startswith("d_")
    assert len((await (await c.get("/digests")).json())["digests"]) == 1
    await c.delete(f"/digests/{d['id']}")
    assert (await (await c.get("/digests")).json())["digests"] == []


async def test_rubric_override_validation(env):
    c, _ = env
    assert (await c.put("/rubric", json={"dimensions": []})).status == 400
    ok = {"label": "Custom", "dimensions": ["Craft"],
          "requirements": [{"code": "C1", "dim": "Craft", "threshold": 1, "keywords": ["built"]}]}
    assert (await c.put("/rubric", json=ok)).status == 200
    d = await (await c.get("/rubric")).json()
    assert d["is_override"] is True and d["rubric"]["dimensions"] == ["Craft"]
    await c.post("/rubric/reset")
    assert (await (await c.get("/rubric")).json())["is_override"] is False
