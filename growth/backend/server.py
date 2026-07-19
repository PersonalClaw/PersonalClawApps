"""Growth Tracker backend — artifact store + rubric lens (NO LLM, app-private).

Redesigned from motive: track your growth ARTIFACTS, using your real work (chat sessions,
projects/loops, tasks, knowledge you authored — read from core in the BROWSER) as evidence
ALONGSIDE your own notes. This backend owns the app-private store under
PERSONALCLAW_APP_DATA_DIR and the deterministic rubric engine; it never calls core or an LLM
(the browser does agent-run for narrative drafting + digest prose, then POSTs the result here).

Core objects:
  - artifact   : a piece of evidenced growth (title/narrative/S·B·I + evidence links + dimensions
                 + impact + period). Evidence links point at PClaw sources (chat/project/task/
                 knowledge) or external URLs — the "use your LLM work as input" spine.
  - growth_area: a deliberate goal you're working toward; artifacts link to it to show progress.
  - digest     : a generated period accomplishment doc (brag-doc) citing artifacts.
  - rubric     : the customizable scoring LENS (shipped default + optional override) — a keyword
                 classifier + coverage/consistency scoring. Demoted to Settings; scores, isn't the UI.

Routes are BARE (the gateway proxy strips /apps/growth/api and forwards the tail).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path

from aiohttp import web

DATA_DIR = Path(os.environ.get("PERSONALCLAW_APP_DATA_DIR", os.path.expanduser("~/.personalclaw/apps/growth/data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "growth.db"
RUBRIC_OVERRIDE = DATA_DIR / "rubric.json"

# Neutral, generic IC-growth rubric (NOT org-specific). The user can override via PUT.
DEFAULT_RUBRIC = {
    "label": "Individual Contributor — Growth",
    "dimensions": ["Scope & Influence", "Ambiguity", "Problem Complexity",
                   "Execution", "Impact", "Communication", "Growth & Learning"],
    "requirements": [
        {"code": "EX1", "dim": "Execution", "short": "Ships quality",
         "text": "Consistently ships high-quality, well-tested work",
         "threshold": 3, "keywords": ["shipped", "delivered", "tested", "released", "migration", "launched", "built", "fixed"]},
        {"code": "IM1", "dim": "Impact", "short": "Measurable impact",
         "text": "Delivers measurable business/customer impact",
         "threshold": 3, "keywords": ["impact", "reduced", "improved", "increased", "saved", "unblocked", "adoption"]},
        {"code": "SC1", "dim": "Scope & Influence", "short": "Cross-team scope",
         "text": "Influences beyond the immediate team",
         "threshold": 2, "keywords": ["cross-team", "org", "influenced", "aligned", "drove", "led"]},
        {"code": "AM1", "dim": "Ambiguity", "short": "Handles ambiguity",
         "text": "Operates effectively in ambiguous problem spaces",
         "threshold": 2, "keywords": ["ambiguous", "undefined", "explored", "prototyped", "scoped", "0-to-1"]},
        {"code": "PC1", "dim": "Problem Complexity", "short": "Hard problems",
         "text": "Solves complex, technically-deep problems",
         "threshold": 2, "keywords": ["complex", "architecture", "designed", "algorithm", "performance", "scale"]},
        {"code": "CM1", "dim": "Communication", "short": "Clear communication",
         "text": "Communicates clearly in writing + docs",
         "threshold": 2, "keywords": ["doc", "wrote", "presented", "rfc", "review", "mentored", "explained"]},
        {"code": "GL1", "dim": "Growth & Learning", "short": "Learns + grows",
         "text": "Actively learns and levels up",
         "threshold": 2, "keywords": ["learned", "studied", "adopted", "improved", "feedback", "grew"]},
    ],
}

_SOURCED_RE = re.compile(r"(PR #?\d+|CR-?\d+|TICKET-?\d+|#\d+|https?://|doc:|wiki)", re.I)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


_mint_counter = 0


def _mint(prefix: str) -> str:
    global _mint_counter
    _mint_counter += 1
    return f"{prefix}_{int(time.time()*1000):x}{_mint_counter:x}"


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS artifacts (
            id TEXT PRIMARY KEY, date TEXT, period TEXT, title TEXT NOT NULL,
            situation TEXT DEFAULT '', behavior TEXT DEFAULT '', impact TEXT DEFAULT '',
            narrative TEXT DEFAULT '', dimensions_json TEXT DEFAULT '[]',
            evidence_json TEXT DEFAULT '[]',      -- [{kind,ref,label}] links to PClaw sources / URLs
            area_id TEXT DEFAULT '',              -- optional growth-area link
            sourced INTEGER DEFAULT 0, source TEXT DEFAULT 'manual',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_art_area ON artifacts(area_id);
        CREATE TABLE IF NOT EXISTS growth_areas (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT DEFAULT '',
            target TEXT DEFAULT '', dimension TEXT DEFAULT '', status TEXT DEFAULT 'active',
            created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS digests (
            id TEXT PRIMARY KEY, period TEXT, content_md TEXT DEFAULT '', created_at TEXT NOT NULL);
        -- Dismissed source refs: candidates the user rejected, so the sources inbox doesn't re-offer them.
        CREATE TABLE IF NOT EXISTS dismissed_sources (
            ref TEXT PRIMARY KEY, dismissed_at TEXT NOT NULL);
        """
    )
    return db


DB = _db()


# ── rubric (the scoring lens) ─────────────────────────────────────────────────────────
def _load_rubric() -> dict:
    if RUBRIC_OVERRIDE.exists():
        try:
            r = json.loads(RUBRIC_OVERRIDE.read_text())
            if _valid_rubric(r):
                return r
        except Exception:
            pass
    return DEFAULT_RUBRIC


def _valid_rubric(r: dict) -> bool:
    return (isinstance(r, dict) and isinstance(r.get("dimensions"), list) and r["dimensions"]
            and isinstance(r.get("requirements"), list) and r["requirements"]
            and all("code" in q and "dim" in q for q in r["requirements"]))


def classify(text: str, stated_dims: list[str] | None = None) -> list[str]:
    """Map text → rubric dimensions by keyword match, capped at top-3. Zero tokens."""
    rubric = _load_rubric()
    low = text.lower()
    scored: list[tuple[int, str]] = []
    for req in rubric["requirements"]:
        hits = sum(1 for kw in req.get("keywords", []) if kw.lower() in low)
        if hits:
            scored.append((hits, req["dim"]))
    scored.sort(reverse=True)
    dims = list(dict.fromkeys(d for _, d in scored))[:3]
    if not dims and stated_dims:
        valid = set(rubric["dimensions"])
        dims = [d for d in stated_dims if d in valid][:3]
    return dims


def is_sourced(text: str, evidence: list) -> bool:
    # Evidence links to PClaw sources (kind != external) always count as sourced; else scan text/urls.
    if any(isinstance(e, dict) and e.get("kind") and e.get("kind") != "external" for e in (evidence or [])):
        return True
    joined = text + " " + " ".join(
        (e.get("ref", "") + " " + e.get("label", "")) if isinstance(e, dict) else str(e)
        for e in (evidence or []))
    return bool(_SOURCED_RE.search(joined))


def _period_of(date: str) -> str:
    try:
        y, m = int(date[:4]), int(date[5:7])
        return f"{y}-Q{(m - 1) // 3 + 1}"
    except Exception:
        return ""


def compute_readiness() -> dict:
    """Per-dimension {actual, threshold, status, pct} + overall + gaps/singles."""
    rubric = _load_rubric()
    rows = list(DB.execute("SELECT dimensions_json, date FROM artifacts"))
    by_dim: dict[str, list[str]] = {d: [] for d in rubric["dimensions"]}
    for r in rows:
        for d in json.loads(r["dimensions_json"] or "[]"):
            if d in by_dim:
                by_dim[d].append((r["date"] or "")[:7])
    dim_threshold: dict[str, int] = {}
    for req in rubric["requirements"]:
        dim_threshold[req["dim"]] = max(dim_threshold.get(req["dim"], 0), int(req.get("threshold", 1)))
    out_dims, covered = [], 0
    for d in rubric["dimensions"]:
        months = by_dim[d]
        n = len(months)
        distinct_months = len(set(m for m in months if m))
        thr = dim_threshold.get(d, 1)
        status = ("Consistent" if n >= 3 and distinct_months >= 3
                  else "Emerging" if n >= 2 else "Single" if n == 1 else "None")
        pct = min(100, round(100 * n / thr)) if thr else 0
        if n >= thr:
            covered += 1
        out_dims.append({"dimension": d, "actual": n, "threshold": thr, "status": status, "pct": pct})
    overall = round(100 * covered / len(rubric["dimensions"])) if rubric["dimensions"] else 0
    return {"dimensions": out_dims, "overall_pct": overall,
            "gaps": [d["dimension"] for d in out_dims if d["status"] == "None"],
            "singles": [d["dimension"] for d in out_dims if d["status"] == "Single"]}


# ── serializers ─────────────────────────────────────────────────────────────────────
def _artifact(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "date": r["date"], "period": r["period"], "title": r["title"],
            "situation": r["situation"], "behavior": r["behavior"], "impact": r["impact"],
            "narrative": r["narrative"], "dimensions": json.loads(r["dimensions_json"] or "[]"),
            "evidence": json.loads(r["evidence_json"] or "[]"), "area_id": r["area_id"],
            "sourced": bool(r["sourced"]), "source": r["source"],
            "created_at": r["created_at"], "updated_at": r["updated_at"]}


def _area(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "name": r["name"], "description": r["description"],
            "target": r["target"], "dimension": r["dimension"], "status": r["status"],
            "created_at": r["created_at"]}


def _bad(msg: str, status: int = 400):
    return web.json_response({"error": msg}, status=status)


async def _json(request):
    try:
        b = await request.json()
        return b if isinstance(b, dict) else None
    except Exception:
        return None


# ── health ────────────────────────────────────────────────────────────────────────────
async def health(request):
    return web.json_response({"ok": True})


# ── artifacts ───────────────────────────────────────────────────────────────────────────
async def list_artifacts(request):
    q = "SELECT * FROM artifacts WHERE 1=1"
    args: list = []
    if request.query.get("dimension"):
        q += " AND dimensions_json LIKE ?"; args.append(f'%{request.query["dimension"]}%')
    if request.query.get("area_id"):
        q += " AND area_id=?"; args.append(request.query["area_id"])
    if request.query.get("period"):
        q += " AND period=?"; args.append(request.query["period"])
    q += " ORDER BY date DESC, created_at DESC"
    return web.json_response({"artifacts": [_artifact(r) for r in DB.execute(q, args)]})


def _insert_artifact(b: dict, source: str) -> dict:
    aid = _mint("a")
    date = b.get("date") or _today()
    title = (b.get("title") or "").strip()
    narrative = b.get("narrative", "")
    text = " ".join([title, b.get("situation", ""), b.get("behavior", ""), b.get("impact", ""), narrative])
    dims = classify(text, b.get("dimensions"))
    evidence = b.get("evidence") or []
    now = _now()
    DB.execute(
        "INSERT INTO artifacts (id,date,period,title,situation,behavior,impact,narrative,"
        "dimensions_json,evidence_json,area_id,sourced,source,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (aid, date, _period_of(date), title, b.get("situation", ""), b.get("behavior", ""),
         b.get("impact", ""), narrative, json.dumps(dims), json.dumps(evidence),
         b.get("area_id", ""), 1 if is_sourced(text, evidence) else 0, source, now, now))
    return _artifact(DB.execute("SELECT * FROM artifacts WHERE id=?", (aid,)).fetchone())


async def create_artifact(request):
    b = await _json(request)
    if b is None or not (b.get("title") or "").strip():
        return _bad("title is required")
    # If this artifact was drafted FROM a PClaw source, mark that ref accepted (out of the inbox).
    return web.json_response(_insert_artifact(b, b.get("source", "manual")))


async def patch_artifact(request):
    aid = request.match_info["id"]
    b = await _json(request)
    if b is None:
        return _bad("invalid JSON")
    r = DB.execute("SELECT * FROM artifacts WHERE id=?", (aid,)).fetchone()
    if not r:
        return _bad("not found", 404)
    title = b.get("title", r["title"])
    situation = b.get("situation", r["situation"])
    behavior = b.get("behavior", r["behavior"])
    impact = b.get("impact", r["impact"])
    narrative = b.get("narrative", r["narrative"])
    evidence = b.get("evidence", json.loads(r["evidence_json"] or "[]"))
    area_id = b.get("area_id", r["area_id"])
    text = " ".join([title, situation, behavior, impact, narrative])
    dims = classify(text, b.get("dimensions") or json.loads(r["dimensions_json"] or "[]"))
    DB.execute("UPDATE artifacts SET title=?,situation=?,behavior=?,impact=?,narrative=?,"
               "dimensions_json=?,evidence_json=?,area_id=?,sourced=?,updated_at=? WHERE id=?",
               (title, situation, behavior, impact, narrative, json.dumps(dims), json.dumps(evidence),
                area_id, 1 if is_sourced(text, evidence) else 0, _now(), aid))
    return web.json_response(_artifact(DB.execute("SELECT * FROM artifacts WHERE id=?", (aid,)).fetchone()))


async def delete_artifact(request):
    DB.execute("DELETE FROM artifacts WHERE id=?", (request.match_info["id"],))
    return web.json_response({"ok": True})


# ── growth areas (goals) ────────────────────────────────────────────────────────────────
async def list_areas(request):
    rows = DB.execute("SELECT * FROM growth_areas ORDER BY created_at")
    areas = [_area(r) for r in rows]
    # attach artifact counts per area
    counts: dict[str, int] = {}
    for r in DB.execute("SELECT area_id, COUNT(*) c FROM artifacts WHERE area_id != '' GROUP BY area_id"):
        counts[r["area_id"]] = r["c"]
    for a in areas:
        a["artifact_count"] = counts.get(a["id"], 0)
    return web.json_response({"areas": areas})


async def create_area(request):
    b = await _json(request)
    if b is None or not (b.get("name") or "").strip():
        return _bad("name is required")
    aid = _mint("ga")
    DB.execute("INSERT INTO growth_areas (id,name,description,target,dimension,status,created_at) "
               "VALUES (?,?,?,?,?,?,?)",
               (aid, b["name"].strip(), b.get("description", ""), b.get("target", ""),
                b.get("dimension", ""), b.get("status", "active"), _now()))
    return web.json_response(_area(DB.execute("SELECT * FROM growth_areas WHERE id=?", (aid,)).fetchone()))


async def patch_area(request):
    gid = request.match_info["id"]
    b = await _json(request)
    if b is None:
        return _bad("invalid JSON")
    r = DB.execute("SELECT * FROM growth_areas WHERE id=?", (gid,)).fetchone()
    if not r:
        return _bad("not found", 404)
    DB.execute("UPDATE growth_areas SET name=?,description=?,target=?,dimension=?,status=? WHERE id=?",
               (b.get("name", r["name"]), b.get("description", r["description"]), b.get("target", r["target"]),
                b.get("dimension", r["dimension"]), b.get("status", r["status"]), gid))
    return web.json_response(_area(DB.execute("SELECT * FROM growth_areas WHERE id=?", (gid,)).fetchone()))


async def delete_area(request):
    gid = request.match_info["id"]
    DB.execute("DELETE FROM growth_areas WHERE id=?", (gid,))
    DB.execute("UPDATE artifacts SET area_id='' WHERE area_id=?", (gid,))  # unlink, don't delete artifacts
    return web.json_response({"ok": True})


# ── sources inbox: dismissed refs (the browser mines PClaw activity; we just remember rejections) ──
async def list_dismissed(request):
    rows = DB.execute("SELECT ref FROM dismissed_sources")
    return web.json_response({"dismissed": [r["ref"] for r in rows]})


async def dismiss_source(request):
    b = await _json(request)
    if b is None or not (b.get("ref") or "").strip():
        return _bad("ref is required")
    DB.execute("INSERT INTO dismissed_sources (ref,dismissed_at) VALUES (?,?) "
               "ON CONFLICT(ref) DO NOTHING", (b["ref"].strip(), _now()))
    return web.json_response({"ok": True})


# ── scoring ───────────────────────────────────────────────────────────────────────────
async def readiness(request):
    return web.json_response(compute_readiness())


# ── rubric routes ───────────────────────────────────────────────────────────────────────
async def get_rubric(request):
    return web.json_response({"rubric": _load_rubric(), "is_override": RUBRIC_OVERRIDE.exists()})


async def put_rubric(request):
    b = await _json(request)
    if b is None or not _valid_rubric(b):
        return _bad("invalid rubric (need non-empty dimensions + requirements with code/dim)")
    RUBRIC_OVERRIDE.write_text(json.dumps(b, indent=2))
    return web.json_response({"ok": True})


async def reset_rubric(request):
    if RUBRIC_OVERRIDE.exists():
        RUBRIC_OVERRIDE.unlink()
    return web.json_response({"ok": True, "rubric": DEFAULT_RUBRIC})


# ── digests ─────────────────────────────────────────────────────────────────────────────
async def list_digests(request):
    rows = DB.execute("SELECT * FROM digests ORDER BY created_at DESC")
    return web.json_response({"digests": [
        {"id": r["id"], "period": r["period"], "content_md": r["content_md"], "created_at": r["created_at"]}
        for r in rows]})


async def create_digest(request):
    b = await _json(request)
    if b is None:
        return _bad("invalid JSON")
    content = (b.get("content_md") or "").strip()
    if not content:
        return _bad("content_md is required")
    did = _mint("d")
    DB.execute("INSERT INTO digests (id,period,content_md,created_at) VALUES (?,?,?,?)",
               (did, b.get("period", ""), content, _now()))
    return web.json_response({"ok": True, "id": did})


async def delete_digest(request):
    DB.execute("DELETE FROM digests WHERE id=?", (request.match_info["id"],))
    return web.json_response({"ok": True})


def make_app() -> web.Application:
    app = web.Application()
    r = app.router
    r.add_get("/health", health)
    # artifacts
    r.add_get("/artifacts", list_artifacts)
    r.add_post("/artifacts", create_artifact)
    r.add_patch("/artifacts/{id}", patch_artifact)
    r.add_delete("/artifacts/{id}", delete_artifact)
    # growth areas
    r.add_get("/areas", list_areas)
    r.add_post("/areas", create_area)
    r.add_patch("/areas/{id}", patch_area)
    r.add_delete("/areas/{id}", delete_area)
    # sources inbox
    r.add_get("/dismissed", list_dismissed)
    r.add_post("/dismissed", dismiss_source)
    # scoring
    r.add_get("/readiness", readiness)
    # rubric
    r.add_get("/rubric", get_rubric)
    r.add_put("/rubric", put_rubric)
    r.add_post("/rubric/reset", reset_rubric)
    # digests
    r.add_get("/digests", list_digests)
    r.add_post("/digests", create_digest)
    r.add_delete("/digests/{id}", delete_digest)
    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8798"))
    web.run_app(make_app(), host="127.0.0.1", port=port, print=None)
