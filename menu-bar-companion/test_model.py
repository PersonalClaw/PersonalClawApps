"""What the menu reads, derived from the two lists: everything waiting on you and everything running.

The companion reads ``GET /api/waiting`` and ``GET /api/running`` and nothing else for what it
shows. Every kind of thing waiting on you is a row, in the list's own order, with its kind, one line
in its source's words and a link to where it is answered; only an approval is answered from here.

Two properties get explicit vacuity floors, because both are the kind that pass for the
wrong reason: the badge (a stored counter agrees with the list right up until it doesn't)
and the failed write (a swallowed error looks exactly like no error).

And what every request carries: the owner token in its ``Authorization`` header, never in
its URL.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse

import pytest
from _rows import approval, item, lists, loop_question, radius, running, waiting_approval
from _ws_fakes import FakeOpener
from menubar_companion.api import WIRE_ACTION, GatewayClient
from menubar_companion.model import INSTANCE_ATTRS, KIND_LABEL, CompanionModel, render

BASE = "http://127.0.0.1:10000"
TOKEN = "fake-owner-token"
A1 = waiting_approval(approval(id="a1"))
A2 = waiting_approval(
    approval(
        id="a2",
        tool="write_file",
        tool_input='{"path": "notes/todo.md"}',
        risk="caution",
        blast_radius=radius(writes=True),
    )
)
L2 = loop_question(
    "L2", "which database?", "prod or dev?", why="The deploy target decides the database."
)
WAITING = [A1, A2, L2]
RUNNING = [
    running("loop:L1", "ship the thing", "Running"),
    running("loop:L2", "which database?", "Needs your answer", phase="input"),
]

#: One item of every kind the list carries (``personalclaw.waiting``), each with the brief its
#: source writes and the route its source answers it at.
EVERY_KIND = [
    waiting_approval(approval(id="ap-1", tool_purpose="Clear the stale build cache")),
    item(
        "question",
        "q-1",
        {"questions": [{"question": "Which branch?", "header": "Branch"}], "asked_by": "agent"},
        route="#/chat/dashboard%3Achat-2",
    ),
    loop_question("L7", "Fix the README", "Keep the old badge?"),
    item(
        "gate",
        "run-1:review",
        {"ask": {"prompt": "Ship the release notes?"}, "workflow": "release"},
        consequence="changes",
        route="#/workflows/runs/run-1",
    ),
    item(
        "park",
        "park-1",
        {"question": "Send the invoice to the client?", "trigger_name": "invoices"},
        consequence="changes",
        route="#/triggers?open=trig-1",
    ),
    item("browse_grant", "g-1", {"task": "Book the table"}, route="#/dashboard"),
    item(
        "plan_review",
        "loop:L8:s1",
        {"name": "Migrate", "step": {"title": "Move the tables"}},
        route="#/loops/L8",
    ),
    item(
        "bridge_confirmation",
        "c-1",
        {"action": "Restart the gateway", "asked_by": "Phone"},
        consequence="changes",
        route="#/inbox",
    ),
]

#: Each kind's row: its kind as every PersonalClaw surface names it, its line in its source's words
#: (``web/src/lib/waitingList.ts``), and the page it opens.
EVERY_ROW = [
    ("approval", "Approval: Clear the stale build cache", f"{BASE}/#/companion?approval=ap-1"),
    ("question", "Question: Which branch?", f"{BASE}/#/chat/dashboard%3Achat-2"),
    ("loop_question", "Loop: Keep the old badge?", f"{BASE}/#/loops/L7"),
    ("gate", "Workflow: Ship the release notes?", f"{BASE}/#/workflows/runs/run-1"),
    ("park", "Automation: Send the invoice to the client?", f"{BASE}/#/triggers?open=trig-1"),
    ("browse_grant", "Browser: Book the table", f"{BASE}/#/dashboard"),
    ("plan_review", "Plan: Move the tables", f"{BASE}/#/loops/L8"),
    ("bridge_confirmation", "Control bridge: Restart the gateway", f"{BASE}/#/inbox"),
]


def _build(
    waiting: list | None = None,
    running_rows: list | None = None,
    routes: dict | None = None,
    token: str = TOKEN,
    unreadable: list | None = None,
) -> tuple[CompanionModel, FakeOpener]:
    opener = FakeOpener(
        {
            **lists(
                WAITING if waiting is None else waiting,
                RUNNING if running_rows is None else running_rows,
                unreadable,
            ),
            **(routes or {}),
        }
    )
    client = GatewayClient(BASE, token, opener=opener)
    return CompanionModel(client), opener


# ── every kind is a row, with its link ──


def test_every_waiting_kind_is_a_row_with_its_kind_its_line_and_its_link():
    model, _ = _build(EVERY_KIND)
    assert model.refresh().ok
    assert [(r.kind, r.title, r.link) for r in model.waiting_rows] == EVERY_ROW
    assert {kind for kind, _title, _link in EVERY_ROW} == set(KIND_LABEL), "every kind covered"


def test_a_row_says_where_it_came_from_in_its_sources_words():
    model, _ = _build(EVERY_KIND)
    model.refresh()
    sources = {r.kind: r.source for r in model.waiting_rows}
    assert sources["loop_question"] == "From loop “Fix the README”"
    assert sources["gate"] == "From workflow “release”"
    assert sources["park"] == "From automation “invoices”"
    assert sources["question"] == "From agent"
    assert sources["browse_grant"] == ""


def test_a_kind_this_version_does_not_know_is_still_listed_and_counted():
    unknown = item("handoff", "h-1", {"title": "Take over the call"}, route="#/inbox")
    model, _ = _build([unknown])
    model.refresh()
    (row,) = model.waiting_rows
    assert (row.kind, row.title, row.link) == ("handoff", "Waiting on you", f"{BASE}/#/inbox")
    assert model.badge == 1


def test_the_menu_keeps_the_lists_order():
    """The gateway orders by consequence, then age; the menu never sorts again."""
    order = [L2, A2, A1]  # an ask ahead of two approvals: not an order this app would pick
    model, _ = _build(order)
    model.refresh()
    assert [r.id for r in model.waiting_rows] == ["loop_question:L2", "approval:a2", "approval:a1"]
    lines = render(model).lines
    at = [next(i for i, line in enumerate(lines) if needle in line) for needle in (
        "Loop: prod or dev?", "(a2)", "(a1)"
    )]
    assert at == sorted(at), lines


def test_only_an_approval_offers_approve_and_deny():
    model, _ = _build(EVERY_KIND)
    model.refresh()
    (approval_row,) = model.pending_approvals
    assert approval_row.id == "ap-1", "the registry's id, which its own route answers"
    assert approval_row.answers == ("Approve", "Deny", "Review in PersonalClaw")
    for row in model.waiting_rows:
        assert (row.approval is not None) == (row.kind == "approval"), row.kind


def test_an_approval_past_answering_offers_only_the_review():
    """A reader holding an item past its ``expires_at`` disables its action itself."""
    late = waiting_approval(approval(id="late"), expires_at="2020-01-01T00:00:00+00:00")
    gone = waiting_approval(approval(id="gone"), current=False)
    model, _ = _build([late, gone, A1])
    model.refresh()
    answers = {r.id: r.answers for r in model.pending_approvals}
    assert answers["late"] == ("Review in PersonalClaw",)
    assert answers["gone"] == ("Review in PersonalClaw",)
    assert answers["a1"] == ("Approve", "Deny", "Review in PersonalClaw"), "the floor"


def test_a_route_that_is_not_a_dashboard_route_opens_home():
    odd = [
        item("gate", "g-1", {"ask": {"prompt": "a"}}, route=""),
        item("gate", "g-2", {"ask": {"prompt": "b"}}, route="https://elsewhere.example/#/x"),
        item("gate", "g-3", {"ask": {"prompt": "c"}}, route="#/runs/1\nSet-Cookie: x"),
        item("gate", "g-4", {"ask": {"prompt": "d"}}, route=["#/runs/1"]),
    ]
    model, _ = _build(odd)
    model.refresh()
    assert [r.link for r in model.waiting_rows] == [f"{BASE}/#/"] * 4


def test_no_link_carries_the_token():
    model, _ = _build(EVERY_KIND)
    model.refresh()
    for row in model.waiting_rows:
        assert TOKEN not in row.link and "token" not in row.link, row.link


# ── what is running ──


def test_the_running_rows_say_what_each_is_doing():
    model, _ = _build()
    model.refresh()
    assert [(r.name, r.line, r.link) for r in model.runs] == [
        ("ship the thing", "Running", f"{BASE}/#/loops/L1"),
        ("which database?", "Needs your answer", f"{BASE}/#/loops/L2"),
    ]


# ── a source that cannot be read is said ──


def test_a_source_the_gateway_could_not_read_is_said_not_shown_as_nothing():
    model, _ = _build([], unreadable=["gate", "park"])
    model.refresh()
    assert model.badge == 0
    assert "⚠ Could not read: Workflow, Automation" in render(model).lines


def test_a_list_of_the_wrong_shape_is_a_failed_read():
    model, _ = _build(routes={"/api/waiting": json.dumps([A1]).encode()})
    result = model.refresh()
    assert result.ok is False
    assert "GET /api/waiting did not return the waiting list" in model.last_error


# ── what every request carries ──


def test_the_app_reads_only_the_two_lists_and_answers_through_the_approvals_route():
    model, opener = _build(routes={"/api/approvals/": b'{"ok": true}'})
    model.refresh()
    assert model.resolve("a1", "deny").ok

    sent = [(req.get_method(), urllib.parse.urlsplit(req.full_url).path) for req in opener.requests]
    assert sent == [
        ("GET", "/api/waiting"),
        ("GET", "/api/running"),
        ("POST", "/api/approvals/a1/reject"),
        ("GET", "/api/waiting"),
        ("GET", "/api/running"),
    ], sent
    for req in opener.requests:
        assert urllib.parse.urlsplit(req.full_url).query == "", req.full_url
        assert TOKEN not in req.full_url
        assert req.get_header("Authorization") == f"Bearer {TOKEN}"
        assert req.get_header("Origin") == BASE


@pytest.mark.parametrize("token", ["two words", "tokén", ""], ids=["a space", "not ascii", "empty"])
def test_a_token_that_cannot_be_a_header_is_refused_before_anything_is_sent(token):
    model, opener = _build(routes={"/api/approvals/": b'{"ok": true}'}, token=token)

    assert model.refresh().ok is False
    assert "is not a PersonalClaw token" in model.last_error, model.last_error
    outcome = model.resolve("a1", "approve")
    assert outcome.ok is False and "is not a PersonalClaw token" in outcome.error
    assert opener.requests == [], "nothing was sent"


def test_vacuity_floor_the_same_requests_go_out_with_a_usable_token():
    model, opener = _build(routes={"/api/approvals/": b'{"ok": true}'}, token="a-usable-fake-token")
    assert model.refresh().ok
    assert model.resolve("a1", "approve").ok
    assert len(opener.requests) == 5


# ── the badge is derived ──


def test_the_badge_is_the_waiting_lists_length():
    model, _ = _build(EVERY_KIND)
    model.refresh()
    assert model.badge == len(EVERY_KIND) == 8
    assert model.badge_text == "8"


def test_badge_cannot_drift_from_the_rows_it_summarises():
    """It is computed from the same list the menu renders, on every read."""
    model, _ = _build()
    model.refresh()
    assert model.badge == 3

    # Mutate the fetched data directly — the number follows immediately.
    model.waiting.pop()
    assert model.badge == 2, "the badge re-derives; it is not a cached count"
    model.waiting.clear()
    assert model.badge == 0
    assert model.badge_text == "", "zero shows no badge at all"

    # Structural half: no instance attribute holds a count that could go stale.
    assert set(vars(model)) == set(INSTANCE_ATTRS)


def test_vacuity_floor_the_drift_rails_can_fail():
    """Prove both halves of the rail above are discriminating.

    A stored counter passes the first read and then lies; and the attribute rail
    actually notices a cache being added.
    """

    class StoredBadge:
        """The design this model refuses: a count maintained beside the list."""

        def __init__(self, rows):
            self.rows = list(rows)
            self.badge = len(rows)  # ← the second source of truth

        def pop(self):
            self.rows.pop()

    stale = StoredBadge(WAITING)
    assert stale.badge == 3  # agrees at first…
    stale.pop()
    assert len(stale.rows) == 2
    assert stale.badge == 3, "…and now the number and the rows disagree"

    model, _ = _build()
    model.refresh()
    model._badge_cache = 99  # the thing the attribute rail exists to catch
    assert set(vars(model)) - set(INSTANCE_ATTRS) == {"_badge_cache"}


# ── the write ──


def test_deny_is_posted_as_the_gateways_reject_action():
    model, opener = _build(routes={"/api/approvals/": b'{"ok": true}'})
    model.refresh()
    outcome = model.resolve("a1", "deny")
    assert outcome.ok
    posts = [c for c in opener.calls if c[0] == "POST"]
    assert posts == [("POST", f"{BASE}/api/approvals/a1/reject")], posts
    assert WIRE_ACTION == {"approve": "approve", "deny": "reject"}


def test_a_failed_write_is_reported_and_the_row_stays_pending():
    """A swallowed write is the defect class this asserts against."""
    failure = urllib.error.HTTPError(
        f"{BASE}/api/approvals/a1/approve",
        404,
        "Not Found",
        {},  # type: ignore[arg-type]
        io.BytesIO(b'{"error": "not found or expired"}'),
    )
    model, opener = _build(routes={"/api/approvals/": failure})
    model.refresh()
    before = model.badge

    outcome = model.resolve("a1", "approve")

    assert outcome.ok is False
    assert outcome.error, "a failed outcome must carry something to show"
    assert "a1" in outcome.error and "404" in outcome.error
    assert "not found or expired" in outcome.error, "the gateway's own reason survives"
    # Reported on the surface that caused it: last_error is what the menu renders.
    assert model.last_error == outcome.error
    assert f"⚠ {outcome.error}" in render(model).lines
    # And nothing optimistically moved: the row still reads as pending.
    assert "a1" in [r.id for r in model.pending_approvals]
    assert model.badge == before


def test_vacuity_floor_a_successful_write_clears_the_error_and_the_row():
    """The floor for the failure test: success looks different in every assertion."""
    state = {"waiting": WAITING}
    opener = FakeOpener(
        {
            "/api/waiting": lambda: json.dumps({"waiting": state["waiting"], "unreadable": []}).encode(),
            "/api/running": json.dumps({"running": RUNNING, "finished": []}).encode(),
            "/api/approvals/": b'{"ok": true}',
        }
    )
    model = CompanionModel(GatewayClient(BASE, "tok", opener=opener))
    model.refresh()
    model.last_error = "a stale message from an earlier failure"

    # The gateway would drop the item; make the next GET reflect that.
    state["waiting"] = [A2, L2]
    outcome = model.resolve("a1", "approve")

    assert outcome.ok and outcome.error == ""
    assert model.last_error == "", "a success clears the reported error"
    assert "a1" not in [r.id for r in model.pending_approvals]
    assert model.badge == 2, "one approval left + the loop's question"
    assert not [line for line in render(model).lines if line.startswith("⚠")]


def test_an_unknown_action_never_reaches_the_wire():
    model, opener = _build(routes={"/api/approvals/": b'{"ok": true}'})
    model.refresh()
    outcome = model.resolve("a1", "maybe")
    assert outcome.ok is False
    assert "unknown approval action" in outcome.error
    assert [c for c in opener.calls if c[0] == "POST"] == []


def test_a_read_failure_is_reported_rather_than_rendering_an_empty_menu():
    """An empty list and a broken gateway must not look the same."""
    broken, _ = _build(routes={"/api/running": urllib.error.URLError("connection refused")})
    result = broken.refresh()
    assert result.ok is False
    assert "connection refused" in broken.last_error
    assert any(line.startswith("⚠") for line in render(broken).lines)


# ── what notifies ──


def test_a_new_item_of_any_kind_notifies_once():
    state = {"waiting": []}
    opener = FakeOpener(
        {
            "/api/waiting": lambda: json.dumps({"waiting": state["waiting"], "unreadable": []}).encode(),
            "/api/running": json.dumps({"running": [], "finished": []}).encode(),
        }
    )
    model = CompanionModel(GatewayClient(BASE, "tok", opener=opener))

    assert model.refresh().notices == ()

    state["waiting"] = EVERY_KIND
    second = model.refresh()
    assert [n.title for n in second.notices] == [title for _kind, title, _link in EVERY_ROW]
    assert [n.heading for n in second.notices][:2] == ["Approval needed", "PersonalClaw needs you"]

    third = model.refresh()
    assert third.notices == (), "already-reported items do not notify again"


@pytest.mark.parametrize("interrupt", ["quiet", "hush", "drop"])
def test_an_item_that_may_not_interrupt_now_is_listed_and_counted_but_not_notified(interrupt):
    """The gateway decides once whether an item may interrupt (mute, quiet hours, its kind's rule),
    so every surface that would ping applies one policy."""
    hushed = loop_question("L9", "night run", "Keep going?", interrupt=interrupt)
    model, _ = _build([hushed])
    result = model.refresh()
    assert result.notices == ()
    assert model.badge == 1
    assert [r.id for r in model.waiting_rows] == ["loop_question:L9"]
