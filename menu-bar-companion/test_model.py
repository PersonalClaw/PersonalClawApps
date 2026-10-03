"""Badge derivation, needs-input deep links, and the Approve/Deny write.

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
from _rows import approval, radius
from _ws_fakes import FakeOpener
from menubar_companion.api import WIRE_ACTION, GatewayClient
from menubar_companion.model import INSTANCE_ATTRS, CompanionModel, render

TOKEN = "fake-owner-token"
APPROVALS = [
    approval(id="a1"),
    approval(
        id="a2",
        tool="write_file",
        tool_input='{"path": "notes/todo.md"}',
        risk="caution",
        blast_radius=radius(writes=True),
    ),
]
#: What a loop waiting on you carries, as core sends it: ``{question, ts, why?}``.
ASKED = {
    "question": "prod or dev?",
    "ts": 1_700_000_000.0,
    "why": "The deploy target decides the database.",
}
LOOPS = {
    "loops": [
        {"id": "L1", "name": "ship the thing", "status": "running"},
        {
            "id": "L2",
            "name": "which database?",
            "status": "needs_input",
            "pending_question": ASKED,
        },
        {"id": "L3", "name": "finished", "status": "complete"},
        {"id": "L4", "name": "stopped one", "status": "stopped"},
    ]
}


def _build(routes: dict | None = None, token: str = TOKEN) -> tuple[CompanionModel, FakeOpener]:
    opener = FakeOpener(
        {
            "/api/approvals": json.dumps(APPROVALS).encode(),
            "/api/loops": json.dumps(LOOPS).encode(),
            **(routes or {}),
        }
    )
    client = GatewayClient("http://127.0.0.1:10000", token, opener=opener)
    return CompanionModel(client), opener


# ── live run rows ──


def test_live_rows_exclude_ended_loops():
    model, _ = _build()
    model.refresh()
    assert [r.id for r in model.runs] == ["L1", "L2"], "complete/stopped are not live"
    assert [r.status for r in model.runs] == ["running", "needs_input"]


def test_needs_input_rows_carry_a_dashboard_deep_link():
    model, _ = _build()
    model.refresh()
    (blocked,) = model.needs_input
    assert blocked.id == "L2"
    # The loop's page, with no credential in it: a browser without a session signs in on
    # the gateway's own sign-in page, which lands it on this route afterwards.
    assert blocked.deep_link == "http://127.0.0.1:10000/#/loops/L2"
    assert TOKEN not in blocked.deep_link
    assert blocked.question == "prod or dev?"
    # A running loop is not decorated with a link it does not need.
    running = [r for r in model.runs if not r.needs_input]
    assert [r.deep_link for r in running] == [""]


@pytest.mark.parametrize(
    ("asked", "shown"),
    [
        (ASKED, "prod or dev?"),
        (
            {"question": "  Which branch should it merge into?  ", "ts": 1.0},
            "Which branch should it merge into?",
        ),
        ({"ts": 1.0, "why": "a question with no text"}, ""),
        ({"question": ["not", "text"], "ts": 1.0}, ""),
        (None, ""),
    ],
    ids=["a question", "padded", "no question text", "a question that is not text", "none"],
)
def test_the_loop_question_reads_as_its_text(asked, shown):
    """Core sends ``{question, ts, why?}``: the row shows the question, never the dict."""
    row = {"id": "L2", "name": "which?", "status": "needs_input", "pending_question": asked}
    loops = {"loops": [row]}
    model, _ = _build({"/api/loops": json.dumps(loops).encode()})
    model.refresh()
    (blocked,) = model.needs_input
    assert blocked.question == shown
    lines = render(model).lines
    assert not [line for line in lines if "'question'" in line or "'ts'" in line], lines
    if shown:
        assert f"      {shown}" in lines, lines


# ── what every request carries ──


def test_every_request_carries_the_token_in_its_header_and_no_url_carries_it():
    """``Authorization: Bearer`` is the gateway's carrier for a client that is not a browser.

    It is judged against the session's whole lifetime and sets no cookie, and it keeps the
    token out of the request line every log records.
    """
    model, opener = _build({"/api/approvals/": b'{"ok": true}'})
    model.refresh()
    assert model.resolve("a1", "deny").ok

    sent = [(req.get_method(), urllib.parse.urlsplit(req.full_url).path) for req in opener.requests]
    assert sent == [
        ("GET", "/api/loops"),
        ("GET", "/api/approvals"),
        ("POST", "/api/approvals/a1/reject"),
        ("GET", "/api/loops"),
        ("GET", "/api/approvals"),
    ], sent
    for req in opener.requests:
        assert urllib.parse.urlsplit(req.full_url).query == "", req.full_url
        assert TOKEN not in req.full_url
        assert req.get_header("Authorization") == f"Bearer {TOKEN}"
        assert req.get_header("Origin") == "http://127.0.0.1:10000"


@pytest.mark.parametrize("token", ["two words", "tokén", ""], ids=["a space", "not ascii", "empty"])
def test_a_token_that_cannot_be_a_header_is_refused_before_anything_is_sent(token):
    model, opener = _build({"/api/approvals/": b'{"ok": true}'}, token=token)

    assert model.refresh().ok is False
    assert "is not a PersonalClaw token" in model.last_error, model.last_error
    outcome = model.resolve("a1", "approve")
    assert outcome.ok is False and "is not a PersonalClaw token" in outcome.error
    assert opener.requests == [], "nothing was sent"


def test_vacuity_floor_the_same_requests_go_out_with_a_usable_token():
    model, opener = _build({"/api/approvals/": b'{"ok": true}'}, token="a-usable-fake-token")
    assert model.refresh().ok
    assert model.resolve("a1", "approve").ok
    assert len(opener.requests) == 5


# ── the badge is derived ──


def test_badge_is_pending_approvals_plus_needs_input():
    model, _ = _build()
    model.refresh()
    assert len(model.pending_approvals) == 2
    assert len(model.needs_input) == 1
    assert model.badge == 3
    assert model.badge_text == "3"


def test_badge_cannot_drift_from_the_rows_it_summarises():
    """It is computed from the same lists the menu renders, on every read."""
    model, _ = _build()
    model.refresh()
    assert model.badge == 3

    # Mutate the fetched data directly — the number follows immediately.
    model.approvals.pop()
    assert model.badge == 2, "the badge re-derives; it is not a cached count"
    model.loops = [row for row in model.loops if row["status"] != "needs_input"]
    assert model.badge == 1
    model.approvals.clear()
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

    stale = StoredBadge(APPROVALS)
    assert stale.badge == 2  # agrees at first…
    stale.pop()
    assert len(stale.rows) == 1
    assert stale.badge == 2, "…and now the number and the rows disagree"

    model, _ = _build()
    model.refresh()
    model._badge_cache = 99  # the thing the attribute rail exists to catch
    assert set(vars(model)) - set(INSTANCE_ATTRS) == {"_badge_cache"}


# ── the write ──


def test_deny_is_posted_as_the_gateways_reject_action():
    model, opener = _build({"/api/approvals/": b'{"ok": true}'})
    model.refresh()
    outcome = model.resolve("a1", "deny")
    assert outcome.ok
    posts = [c for c in opener.calls if c[0] == "POST"]
    assert posts == [("POST", "http://127.0.0.1:10000/api/approvals/a1/reject")], posts
    assert WIRE_ACTION == {"approve": "approve", "deny": "reject"}


def test_a_failed_write_is_reported_and_the_row_stays_pending():
    """A swallowed write is the defect class this asserts against."""
    failure = urllib.error.HTTPError(
        "http://127.0.0.1:10000/api/approvals/a1/approve",
        404,
        "Not Found",
        {},  # type: ignore[arg-type]
        io.BytesIO(b'{"error": "not found or expired"}'),
    )
    model, opener = _build({"/api/approvals/": failure})
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
    state = {"approvals": APPROVALS}

    opener = FakeOpener(
        {
            "/api/approvals": lambda: json.dumps(state["approvals"]).encode(),
            "/api/loops": json.dumps(LOOPS).encode(),
            "/api/approvals/": b'{"ok": true}',
        }
    )
    client = GatewayClient("http://127.0.0.1:10000", "tok", opener=opener)
    model = CompanionModel(client)
    model.refresh()
    model.last_error = "a stale message from an earlier failure"

    # The gateway would drop the row; make the next GET reflect that.
    state["approvals"] = [APPROVALS[1]]
    outcome = model.resolve("a1", "approve")

    assert outcome.ok and outcome.error == ""
    assert model.last_error == "", "a success clears the reported error"
    assert "a1" not in [r.id for r in model.pending_approvals]
    assert model.badge == 2, "one approval left + one needs-input loop"
    assert not [line for line in render(model).lines if line.startswith("⚠")]


def test_an_unknown_action_never_reaches_the_wire():
    model, opener = _build({"/api/approvals/": b'{"ok": true}'})
    model.refresh()
    outcome = model.resolve("a1", "maybe")
    assert outcome.ok is False
    assert "unknown approval action" in outcome.error
    assert [c for c in opener.calls if c[0] == "POST"] == []


def test_a_read_failure_is_reported_rather_than_rendering_an_empty_menu():
    """An empty list and a broken gateway must not look the same."""
    model, _ = _build()
    model.refresh()
    broken, _ = _build({"/api/loops": urllib.error.URLError("connection refused")})
    result = broken.refresh()
    assert result.ok is False
    assert "connection refused" in broken.last_error
    assert any(line.startswith("⚠") for line in render(broken).lines)


# ── notification deltas come from the fetched data ──


def test_new_item_deltas_are_computed_from_the_fetched_rows():
    state = {"approvals": []}
    opener = FakeOpener(
        {
            "/api/approvals": lambda: json.dumps(state["approvals"]).encode(),
            "/api/loops": json.dumps(LOOPS).encode(),
        }
    )
    model = CompanionModel(GatewayClient("http://127.0.0.1:10000", "tok", opener=opener))

    first = model.refresh()
    assert first.new_needs_input == ("L2",), "the blocked loop is new on the first read"
    assert first.new_approvals == ()

    state["approvals"] = APPROVALS
    second = model.refresh()
    assert sorted(second.new_approvals) == ["a1", "a2"]
    assert second.new_needs_input == (), "already-reported items do not re-notify"

    third = model.refresh()
    assert third.new_approvals == () and third.new_needs_input == ()
