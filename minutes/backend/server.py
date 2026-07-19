"""Minutes app backend — composite meeting store (app-private CRUD, no core calls, no LLM).

Redesigned from motive: a MEETING is a composite temporal object over several knowledge-type
records (audio, video, MULTIPLE videos, notes, docs), not a row with a members list + a summary
blob. This backend owns the app-private store under PERSONALCLAW_APP_DATA_DIR:

  - meeting      : title, date, tags, ordered MEMBERS (N knowledge items, each with a media role).
  - participant  : a first-class person on the meeting, optionally mapped to a diarization speaker
                   label (SPEAKER_00 → "Jordan") and to a knowledge-graph person entity. Reused via
                   the global roster.
  - output       : a generated artifact (minutes / action-items / a custom-template summary) — MANY
                   per meeting, each from a chosen template, versioned + editable.
  - extraction   : consolidated structured items pulled from the meeting — kind ∈ date | action |
                   followup | decision — editable, and (for actions) linkable to a created PClaw task.
  - template     : customizable generation template (built-ins fork copy-on-edit).

Everything that touches core Knowledge / Lexicon / Projects / Tasks / agent-run happens in the
BROWSER (which holds the app-scoped token); this backend never receives a gateway URL or token.
Routes are BARE (the gateway proxy strips /apps/minutes/api and forwards the tail).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path

from aiohttp import web

DATA_DIR = Path(os.environ.get("PERSONALCLAW_APP_DATA_DIR", os.path.expanduser("~/.personalclaw/apps/minutes/data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "meetings.db"

_ID_RE = re.compile(r"^[a-z0-9_]+$")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


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
        CREATE TABLE IF NOT EXISTS meetings (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, date TEXT,
            member_ids_json TEXT DEFAULT '[]', member_roles_json TEXT DEFAULT '{}',
            tags_json TEXT DEFAULT '[]', notes TEXT DEFAULT '',
            project_id TEXT DEFAULT '', task_list_id TEXT DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        -- Participants: first-class people on a meeting. speaker_label maps a diarization label
        -- (SPEAKER_00) to this person; entity_ref links to a knowledge-graph person entity (name).
        CREATE TABLE IF NOT EXISTS participants (
            id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, name TEXT NOT NULL,
            speaker_label TEXT DEFAULT '', role TEXT DEFAULT '', entity_ref TEXT DEFAULT '',
            created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_part_meeting ON participants(meeting_id);
        CREATE TABLE IF NOT EXISTS templates (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT DEFAULT '',
            prompt TEXT NOT NULL, output TEXT DEFAULT 'markdown', meeting_type_hint TEXT DEFAULT '',
            builtin INTEGER DEFAULT 0, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS outputs (
            id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, template_id TEXT, template_name TEXT,
            title TEXT DEFAULT '', content_md TEXT DEFAULT '', action_items_json TEXT DEFAULT '[]',
            model TEXT DEFAULT '', edited INTEGER DEFAULT 0, created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_out_meeting ON outputs(meeting_id);
        -- Consolidated extractions: dates / action items / follow-ups / decisions across the meeting.
        CREATE TABLE IF NOT EXISTS extractions (
            id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, kind TEXT NOT NULL,
            text TEXT NOT NULL, assignee TEXT DEFAULT '', due TEXT DEFAULT '',
            task_id TEXT DEFAULT '', done INTEGER DEFAULT 0, created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_ext_meeting ON extractions(meeting_id);
        CREATE TABLE IF NOT EXISTS roster (
            name TEXT PRIMARY KEY, count INTEGER DEFAULT 1, last_used TEXT NOT NULL);
        """
    )
    # Migrate a pre-redesign `meetings` table (which lacked notes/project_id/task_list_id) so an
    # existing store upgrades in place rather than 500-ing on the new serializer. Additive only.
    cols = {r["name"] for r in db.execute("PRAGMA table_info(meetings)")}
    for col in ("notes", "project_id", "task_list_id"):
        if col not in cols:
            db.execute(f"ALTER TABLE meetings ADD COLUMN {col} TEXT DEFAULT ''")
    return db


DB = _db()


# ── built-in templates ─────────────────────────────────────────────────────────────
_BUILTINS = [
    {"id": "standard-minutes", "name": "Standard Minutes",
     "description": "Full minutes: summary, decisions, action items, risks, Q&A.",
     "output": "json", "meeting_type_hint": "general",
     "prompt": ("Produce meeting minutes from the corpus. Emit JSON with keys: summary, "
                "key_points[], decisions[], action_items[{description,assignee,due_date,priority}], "
                "questions[], risks[], and finally a `minutes` markdown field LAST (so a truncated "
                "response still yields the structured fields).")},
    {"id": "action-items", "name": "Action Items Only",
     "description": "Just the action items with owners + due dates.",
     "output": "json", "meeting_type_hint": "general",
     "prompt": "Extract ONLY action items as JSON: action_items[{description,assignee,due_date,priority}]."},
    {"id": "decisions-risks", "name": "Decisions & Risks",
     "description": "Decisions made and risks raised.", "output": "markdown", "meeting_type_hint": "general",
     "prompt": "Summarize the decisions made and the risks raised, as two markdown sections."},
    {"id": "one-on-one", "name": "1:1 Notes", "description": "Manager 1:1 notes + growth areas.",
     "output": "markdown", "meeting_type_hint": "1on1",
     "prompt": "Summarize this 1:1: discussion points, feedback, growth areas, and follow-ups."},
    {"id": "standup", "name": "Standup Recap", "description": "Per-person yesterday/today/blockers.",
     "output": "markdown", "meeting_type_hint": "standup",
     "prompt": "Recap this standup per person: yesterday, today, blockers."},
]


def _seed_builtins() -> None:
    existing = {r["id"] for r in DB.execute("SELECT id FROM templates WHERE builtin=1")}
    for t in _BUILTINS:
        if t["id"] not in existing:
            DB.execute(
                "INSERT INTO templates (id,name,description,prompt,output,meeting_type_hint,builtin,created_at) "
                "VALUES (?,?,?,?,?,?,1,?)",
                (t["id"], t["name"], t["description"], t["prompt"], t["output"], t["meeting_type_hint"], _now()))


_seed_builtins()


# ── serializers ───────────────────────────────────────────────────────────────────
def _meeting(r: sqlite3.Row) -> dict:
    mid = r["id"]
    parts = [_participant(p) for p in DB.execute("SELECT * FROM participants WHERE meeting_id=? ORDER BY created_at", (mid,))]
    out_ct = DB.execute("SELECT COUNT(*) c FROM outputs WHERE meeting_id=?", (mid,)).fetchone()["c"]
    open_actions = DB.execute("SELECT COUNT(*) c FROM extractions WHERE meeting_id=? AND kind='action' AND done=0", (mid,)).fetchone()["c"]
    return {
        "id": mid, "title": r["title"], "date": r["date"],
        "member_ids": json.loads(r["member_ids_json"] or "[]"),
        "member_roles": json.loads(r["member_roles_json"] or "{}"),
        "tags": json.loads(r["tags_json"] or "[]"), "notes": r["notes"],
        "project_id": r["project_id"], "task_list_id": r["task_list_id"],
        "participants": parts, "output_count": out_ct, "open_action_count": open_actions,
        "created_at": r["created_at"], "updated_at": r["updated_at"],
    }


def _participant(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "meeting_id": r["meeting_id"], "name": r["name"],
            "speaker_label": r["speaker_label"], "role": r["role"], "entity_ref": r["entity_ref"]}


def _template(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "name": r["name"], "description": r["description"], "prompt": r["prompt"],
            "output": r["output"], "meeting_type_hint": r["meeting_type_hint"], "builtin": bool(r["builtin"])}


def _output(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "meeting_id": r["meeting_id"], "template_id": r["template_id"],
            "template_name": r["template_name"], "title": r["title"], "content_md": r["content_md"],
            "action_items": json.loads(r["action_items_json"] or "[]"), "model": r["model"],
            "edited": bool(r["edited"]), "created_at": r["created_at"]}


def _extraction(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "meeting_id": r["meeting_id"], "kind": r["kind"], "text": r["text"],
            "assignee": r["assignee"], "due": r["due"], "task_id": r["task_id"], "done": bool(r["done"]),
            "created_at": r["created_at"]}


def _bad(msg: str, status: int = 400):
    return web.json_response({"error": msg}, status=status)


async def _json(request):
    try:
        b = await request.json()
        return b if isinstance(b, dict) else None
    except Exception:
        return None


# ── health ──────────────────────────────────────────────────────────────────────
async def health(request):
    return web.json_response({"ok": True})


# ── meetings ──────────────────────────────────────────────────────────────────────
async def list_meetings(request):
    rows = DB.execute("SELECT * FROM meetings ORDER BY date DESC, created_at DESC")
    return web.json_response({"meetings": [_meeting(r) for r in rows]})


async def create_meeting(request):
    b = await _json(request)
    if b is None or not (b.get("title") or "").strip():
        return _bad("title is required")
    mid = _mint("mtg")
    now = _now()
    DB.execute(
        "INSERT INTO meetings (id,title,date,member_ids_json,member_roles_json,tags_json,notes,"
        "project_id,task_list_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (mid, b["title"].strip(), b.get("date") or now[:10], "[]", "{}", json.dumps(b.get("tags") or []),
         b.get("notes", ""), "", "", now, now))
    return web.json_response(_meeting(DB.execute("SELECT * FROM meetings WHERE id=?", (mid,)).fetchone()))


async def get_meeting(request):
    r = DB.execute("SELECT * FROM meetings WHERE id=?", (request.match_info["id"],)).fetchone()
    return web.json_response(_meeting(r)) if r else _bad("not found", 404)


async def patch_meeting(request):
    mid = request.match_info["id"]
    b = await _json(request)
    if b is None:
        return _bad("invalid JSON")
    r = DB.execute("SELECT * FROM meetings WHERE id=?", (mid,)).fetchone()
    if not r:
        return _bad("not found", 404)
    title = b.get("title", r["title"])
    date = b.get("date", r["date"])
    tags = json.dumps(b["tags"]) if "tags" in b else r["tags_json"]
    notes = b.get("notes", r["notes"])
    member_ids = json.dumps(b["member_order"]) if "member_order" in b else r["member_ids_json"]
    project_id = b.get("project_id", r["project_id"])
    task_list_id = b.get("task_list_id", r["task_list_id"])
    DB.execute("UPDATE meetings SET title=?,date=?,tags_json=?,notes=?,member_ids_json=?,"
               "project_id=?,task_list_id=?,updated_at=? WHERE id=?",
               (title, date, tags, notes, member_ids, project_id, task_list_id, _now(), mid))
    return web.json_response(_meeting(DB.execute("SELECT * FROM meetings WHERE id=?", (mid,)).fetchone()))


async def delete_meeting(request):
    mid = request.match_info["id"]
    for tbl in ("meetings", "participants", "outputs", "extractions"):
        col = "id" if tbl == "meetings" else "meeting_id"
        DB.execute(f"DELETE FROM {tbl} WHERE {col}=?", (mid,))
    return web.json_response({"ok": True})


async def add_member(request):
    mid = request.match_info["id"]
    b = await _json(request)
    if b is None or not (b.get("item_id") or "").strip():
        return _bad("item_id is required")
    r = DB.execute("SELECT * FROM meetings WHERE id=?", (mid,)).fetchone()
    if not r:
        return _bad("not found", 404)
    members = json.loads(r["member_ids_json"] or "[]")
    roles = json.loads(r["member_roles_json"] or "{}")
    item_id = b["item_id"]
    if item_id not in members:
        members.append(item_id)
    if b.get("role"):
        roles[item_id] = b["role"]
    DB.execute("UPDATE meetings SET member_ids_json=?,member_roles_json=?,updated_at=? WHERE id=?",
               (json.dumps(members), json.dumps(roles), _now(), mid))
    return web.json_response(_meeting(DB.execute("SELECT * FROM meetings WHERE id=?", (mid,)).fetchone()))


async def remove_member(request):
    mid, item_id = request.match_info["id"], request.match_info["item_id"]
    r = DB.execute("SELECT * FROM meetings WHERE id=?", (mid,)).fetchone()
    if not r:
        return _bad("not found", 404)
    members = [m for m in json.loads(r["member_ids_json"] or "[]") if m != item_id]
    roles = json.loads(r["member_roles_json"] or "{}"); roles.pop(item_id, None)
    DB.execute("UPDATE meetings SET member_ids_json=?,member_roles_json=?,updated_at=? WHERE id=?",
               (json.dumps(members), json.dumps(roles), _now(), mid))
    return web.json_response({"ok": True})


# ── participants (first-class people) ────────────────────────────────────────────────
async def list_participants(request):
    rows = DB.execute("SELECT * FROM participants WHERE meeting_id=? ORDER BY created_at", (request.match_info["id"],))
    return web.json_response({"participants": [_participant(r) for r in rows]})


async def add_participant(request):
    mid = request.match_info["id"]
    b = await _json(request)
    if b is None or not (b.get("name") or "").strip():
        return _bad("name is required")
    pid = _mint("prt")
    name = b["name"].strip()
    DB.execute("INSERT INTO participants (id,meeting_id,name,speaker_label,role,entity_ref,created_at) "
               "VALUES (?,?,?,?,?,?,?)",
               (pid, mid, name, b.get("speaker_label", ""), b.get("role", ""), b.get("entity_ref", ""), _now()))
    # upsert the global roster (autocomplete)
    DB.execute("INSERT INTO roster (name,count,last_used) VALUES (?,1,?) "
               "ON CONFLICT(name) DO UPDATE SET count=count+1,last_used=excluded.last_used", (name, _now()))
    return web.json_response(_participant(DB.execute("SELECT * FROM participants WHERE id=?", (pid,)).fetchone()))


async def patch_participant(request):
    pid = request.match_info["pid"]
    b = await _json(request)
    if b is None:
        return _bad("invalid JSON")
    r = DB.execute("SELECT * FROM participants WHERE id=?", (pid,)).fetchone()
    if not r:
        return _bad("not found", 404)
    DB.execute("UPDATE participants SET name=?,speaker_label=?,role=?,entity_ref=? WHERE id=?",
               (b.get("name", r["name"]), b.get("speaker_label", r["speaker_label"]),
                b.get("role", r["role"]), b.get("entity_ref", r["entity_ref"]), pid))
    return web.json_response(_participant(DB.execute("SELECT * FROM participants WHERE id=?", (pid,)).fetchone()))


async def delete_participant(request):
    DB.execute("DELETE FROM participants WHERE id=?", (request.match_info["pid"],))
    return web.json_response({"ok": True})


async def roster(request):
    rows = DB.execute("SELECT name,count,last_used FROM roster ORDER BY count DESC,last_used DESC LIMIT 200")
    return web.json_response({"roster": [{"name": r["name"], "count": r["count"], "last_used": r["last_used"]} for r in rows]})


# ── templates ─────────────────────────────────────────────────────────────────────
async def list_templates(request):
    rows = DB.execute("SELECT * FROM templates ORDER BY builtin DESC, name")
    return web.json_response({"templates": [_template(r) for r in rows]})


async def create_template(request):
    b = await _json(request)
    if b is None or not (b.get("name") or "").strip() or not (b.get("prompt") or "").strip():
        return _bad("name and prompt are required")
    tid = _mint("tpl")
    DB.execute("INSERT INTO templates (id,name,description,prompt,output,meeting_type_hint,builtin,created_at) "
               "VALUES (?,?,?,?,?,?,0,?)",
               (tid, b["name"].strip(), b.get("description", ""), b["prompt"], b.get("output", "markdown"),
                b.get("meeting_type_hint", ""), _now()))
    return web.json_response(_template(DB.execute("SELECT * FROM templates WHERE id=?", (tid,)).fetchone()))


async def patch_template(request):
    tid = request.match_info["id"]
    b = await _json(request)
    if b is None:
        return _bad("invalid JSON")
    r = DB.execute("SELECT * FROM templates WHERE id=?", (tid,)).fetchone()
    if not r:
        return _bad("not found", 404)
    if r["builtin"]:  # copy-on-edit: built-ins fork to a user template
        tid = _mint("tpl")
        DB.execute("INSERT INTO templates (id,name,description,prompt,output,meeting_type_hint,builtin,created_at) "
                   "VALUES (?,?,?,?,?,?,0,?)",
                   (tid, b.get("name", r["name"]), b.get("description", r["description"]),
                    b.get("prompt", r["prompt"]), b.get("output", r["output"]),
                    b.get("meeting_type_hint", r["meeting_type_hint"]), _now()))
    else:
        DB.execute("UPDATE templates SET name=?,description=?,prompt=?,output=?,meeting_type_hint=? WHERE id=?",
                   (b.get("name", r["name"]), b.get("description", r["description"]), b.get("prompt", r["prompt"]),
                    b.get("output", r["output"]), b.get("meeting_type_hint", r["meeting_type_hint"]), tid))
    return web.json_response(_template(DB.execute("SELECT * FROM templates WHERE id=?", (tid,)).fetchone()))


async def delete_template(request):
    tid = request.match_info["id"]
    r = DB.execute("SELECT builtin FROM templates WHERE id=?", (tid,)).fetchone()
    if not r:
        return _bad("not found", 404)
    if r["builtin"]:
        return _bad("cannot delete a built-in template")
    DB.execute("DELETE FROM templates WHERE id=?", (tid,))
    return web.json_response({"ok": True})


# ── outputs (generated minutes/summaries — MANY per meeting) ──────────────────────────
async def list_outputs(request):
    rows = DB.execute("SELECT * FROM outputs WHERE meeting_id=? ORDER BY created_at DESC", (request.match_info["id"],))
    return web.json_response({"outputs": [_output(r) for r in rows]})


async def create_output(request):
    mid = request.match_info["id"]
    b = await _json(request)
    if b is None:
        return _bad("invalid JSON")
    content = (b.get("content_md") or "").strip()
    if not content:
        return _bad("content_md is required")
    oid = _mint("out")
    DB.execute("INSERT INTO outputs (id,meeting_id,template_id,template_name,title,content_md,"
               "action_items_json,model,edited,created_at) VALUES (?,?,?,?,?,?,?,?,0,?)",
               (oid, mid, b.get("template_id"), b.get("template_name"), b.get("title", ""), content,
                json.dumps(b.get("action_items") or []), b.get("model", ""), _now()))
    return web.json_response(_output(DB.execute("SELECT * FROM outputs WHERE id=?", (oid,)).fetchone()))


async def patch_output(request):
    oid = request.match_info["oid"]
    b = await _json(request)
    if b is None:
        return _bad("invalid JSON")
    r = DB.execute("SELECT * FROM outputs WHERE id=?", (oid,)).fetchone()
    if not r:
        return _bad("not found", 404)
    content = b.get("content_md", r["content_md"])
    title = b.get("title", r["title"])
    items = json.dumps(b["action_items"]) if "action_items" in b else r["action_items_json"]
    DB.execute("UPDATE outputs SET content_md=?,title=?,action_items_json=?,edited=1 WHERE id=?",
               (content, title, items, oid))
    return web.json_response(_output(DB.execute("SELECT * FROM outputs WHERE id=?", (oid,)).fetchone()))


async def delete_output(request):
    DB.execute("DELETE FROM outputs WHERE id=?", (request.match_info["oid"],))
    return web.json_response({"ok": True})


# ── extractions (consolidated dates / actions / follow-ups / decisions) ────────────────
_EXT_KINDS = {"date", "action", "followup", "decision"}


async def list_extractions(request):
    rows = DB.execute("SELECT * FROM extractions WHERE meeting_id=? ORDER BY kind, created_at", (request.match_info["id"],))
    return web.json_response({"extractions": [_extraction(r) for r in rows]})


async def add_extractions(request):
    """Bulk-add extractions (the generate step posts a batch: dates+actions+followups+decisions)."""
    mid = request.match_info["id"]
    b = await _json(request)
    if b is None or not isinstance(b.get("items"), list):
        return _bad("items[] required")
    added = []
    for it in b["items"]:
        if not isinstance(it, dict):
            continue
        kind = it.get("kind")
        text = (it.get("text") or "").strip()
        if kind not in _EXT_KINDS or not text:
            continue
        eid = _mint("ext")
        DB.execute("INSERT INTO extractions (id,meeting_id,kind,text,assignee,due,task_id,done,created_at) "
                   "VALUES (?,?,?,?,?,?,?,0,?)",
                   (eid, mid, kind, text, it.get("assignee", ""), it.get("due", ""), "", _now()))
        added.append(eid)
    return web.json_response({"ok": True, "added": len(added)})


async def patch_extraction(request):
    eid = request.match_info["eid"]
    b = await _json(request)
    if b is None:
        return _bad("invalid JSON")
    r = DB.execute("SELECT * FROM extractions WHERE id=?", (eid,)).fetchone()
    if not r:
        return _bad("not found", 404)
    DB.execute("UPDATE extractions SET text=?,assignee=?,due=?,task_id=?,done=? WHERE id=?",
               (b.get("text", r["text"]), b.get("assignee", r["assignee"]), b.get("due", r["due"]),
                b.get("task_id", r["task_id"]), 1 if b.get("done", bool(r["done"])) else 0, eid))
    return web.json_response(_extraction(DB.execute("SELECT * FROM extractions WHERE id=?", (eid,)).fetchone()))


async def delete_extraction(request):
    DB.execute("DELETE FROM extractions WHERE id=?", (request.match_info["eid"],))
    return web.json_response({"ok": True})


def make_app() -> web.Application:
    app = web.Application()
    r = app.router
    r.add_get("/health", health)
    # meetings
    r.add_get("/meetings", list_meetings)
    r.add_post("/meetings", create_meeting)
    r.add_get("/meetings/{id}", get_meeting)
    r.add_patch("/meetings/{id}", patch_meeting)
    r.add_delete("/meetings/{id}", delete_meeting)
    r.add_post("/meetings/{id}/members", add_member)
    r.add_delete("/meetings/{id}/members/{item_id}", remove_member)
    # participants
    r.add_get("/meetings/{id}/participants", list_participants)
    r.add_post("/meetings/{id}/participants", add_participant)
    r.add_patch("/meetings/{id}/participants/{pid}", patch_participant)
    r.add_delete("/meetings/{id}/participants/{pid}", delete_participant)
    r.add_get("/roster", roster)
    # templates
    r.add_get("/templates", list_templates)
    r.add_post("/templates", create_template)
    r.add_patch("/templates/{id}", patch_template)
    r.add_delete("/templates/{id}", delete_template)
    # outputs
    r.add_get("/meetings/{id}/outputs", list_outputs)
    r.add_post("/meetings/{id}/outputs", create_output)
    r.add_patch("/meetings/{id}/outputs/{oid}", patch_output)
    r.add_delete("/meetings/{id}/outputs/{oid}", delete_output)
    # extractions
    r.add_get("/meetings/{id}/extractions", list_extractions)
    r.add_post("/meetings/{id}/extractions", add_extractions)
    r.add_patch("/meetings/{id}/extractions/{eid}", patch_extraction)
    r.add_delete("/meetings/{id}/extractions/{eid}", delete_extraction)
    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8799"))
    web.run_app(make_app(), host="127.0.0.1", port=port, print=None)
