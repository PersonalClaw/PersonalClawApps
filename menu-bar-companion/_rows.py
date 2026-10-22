"""Rows shaped as the gateway sends them, for the menu's tests.

:func:`approval` is one approval registry row carrying every field core's pending-approval
entry has (``DashboardApprovalState._approval_entry``), so a test reads the brief from the fields
the gateway really sends rather than from fields a fixture made up.

:func:`item`, :func:`waiting_approval` and :func:`loop_question` are ``GET /api/waiting`` items,
and :func:`running` a ``GET /api/running`` row, each with every field core's list gives one.
"""

from __future__ import annotations

#: A reach line in the words core's run bounds compose it (``run_bounds.ask_note``).
REACH = (
    "It reaches packages.example.com, which is not on your allowed hosts. A command that "
    "reaches a host off that list, or one it does not name, is always asked about, whatever "
    "this chat or its agent allows."
)

#: Who asked for a call's turn when it was not the owner, in the words core says it
#: (``approval_grants.asked_for_line``).
ASKED_FOR = (
    "Jonas (U0JONASCOL) on teamchat asked for this, not you. Your Trust, Trust reads, YOLO and "
    "an agent's Always allow answer only what you ask for, so this call waits for your answer."
)

#: A Deny that ends an agent's turn, in the words core says it (``turn_endings.deny_effect``).
DENY_ENDS_THE_TURN = (
    "The agent CLI offers no way to skip only this step: Deny ends its turn, and PersonalClaw "
    "then asks it to carry on without it."
)


def radius(**established: bool) -> dict[str, bool]:
    """A blast radius as core composes it: every facet named, the given ones established."""
    facets = ("writes", "shell", "network", "saysReadOnly", "readOnly")
    unknown = set(established) - set(facets)
    assert not unknown, f"not a facet: {sorted(unknown)}"
    return {facet: bool(established.get(facet, False)) for facet in facets}


def approval(**fields: object) -> dict[str, object]:
    """A chat's pending ``bash`` call, with *fields* in place of the defaults."""
    row: dict[str, object] = {
        "id": "dashboard:chat-1:call-1",
        "request_id": "call-1",
        "source": "",
        "tool": "bash",
        "tool_input": '{"command": "rm -rf build/cache"}',
        "tool_purpose": "Clear the stale build cache",
        "session": "dashboard:chat-1",
        "session_title": "Release prep",
        "agent": "agent-cli",
        "risk": "destructive",
        "is_read_only": False,
        "blast_radius": radius(writes=True, shell=True),
        "grant_agent": "agent-cli",
        "reach": "",
        "deny_effect": "",
        "asked_for": "",
        "trigger": "",
        "trigger_name": "",
        "source_label": "chat “Release prep”",
        "asked_by": "agent:dashboard:chat-1",
        "ts": 1_700_000_000.0,
    }
    row.update(fields)
    return row


#: The fields of an approval's registry row its waiting item carries as its brief, as core's
#: waiting list copies them (``personalclaw.waiting.APPROVAL_BRIEF``).
APPROVAL_BRIEF = (
    "tool",
    "tool_input",
    "tool_purpose",
    "source_label",
    "session_title",
    "whose_work",
    "asked_for",
    "agent",
    "reach",
    "blast_radius",
    "deny_effect",
    "is_read_only",
    "confirms",
    "asked_by",
    "trigger_name",
)


def item(kind: str, ident: str, brief: dict, **fields: object) -> dict[str, object]:
    """One ``GET /api/waiting`` item with every field core's list gives one
    (``personalclaw.waiting.ITEM_FIELDS``), *fields* in place of the defaults."""
    row: dict[str, object] = {
        "id": f"{kind}:{ident}",
        "kind": kind,
        "work": "",
        "session": "",
        "brief": brief,
        "consequence": "asks",
        "risk": "",
        "asked_at": "2026-10-22T09:00:00+00:00",
        "age_secs": 60,
        "expires_at": None,
        "current": True,
        "interrupt": "deliver",
        "answer": {"method": "POST", "path": "", "takes": [], "body": {}},
        "route": "",
    }
    row.update(fields)
    return row


def waiting_approval(row: dict[str, object] | None = None, **fields: object) -> dict[str, object]:
    """An approval's waiting item, built from its registry *row* (:func:`approval`) as core builds
    it: the registry's id under ``approval:``, its brief fields, its risk beside them."""
    row = row if row is not None else approval()
    approval_id = str(row["id"])
    risk = str(row.get("risk") or "")
    return item(
        "approval",
        approval_id,
        {key: row[key] for key in APPROVAL_BRIEF if key in row},
        **{
            "work": "chat:chat-1",
            "session": "chat-1",
            "consequence": "destructive" if risk == "destructive" else "changes",
            "risk": risk,
            "answer": {
                "method": "POST",
                "path": f"/api/approvals/{approval_id}/{{answer}}",
                "takes": ["approve", "reject"],
                "body": {},
            },
            "route": "#/chat/dashboard%3Achat-1",
            **fields,
        },
    )


def loop_question(
    loop_id: str, name: str, question: str, *, why: str = "", **fields: object
) -> dict[str, object]:
    """A loop's question, as the waiting list names it: ``{name, question, why?}``."""
    brief = {"name": name, "question": question, **({"why": why} if why else {})}
    return item(
        "loop_question",
        loop_id,
        brief,
        **{"work": f"loop:{loop_id}", "route": f"#/loops/{loop_id}", **fields},
    )


def running(work: str, name: str, line: str, **fields: object) -> dict[str, object]:
    """One ``GET /api/running`` row with every field core's list gives one
    (``personalclaw.running_now.ROW_FIELDS``)."""
    kind, _, ident = work.partition(":")
    row: dict[str, object] = {
        "work": work,
        "kind": kind,
        "name": name,
        "agent": "",
        "phase": "thinking",
        "line": line,
        "since": "2026-10-22T09:00:00+00:00",
        "started_at": "2026-10-22T08:55:00+00:00",
        "route": f"#/loops/{ident}" if kind == "loop" else f"#/chat/{ident}",
        "session": "",
        "blocked": False,
        "waiting": [],
    }
    row.update(fields)
    return row


def lists(waiting: list, running_rows: list | None = None, unreadable: list | None = None):
    """The two bodies a gateway answers, for a ``FakeOpener``."""
    import json

    return {
        "/api/waiting": json.dumps({"waiting": waiting, "unreadable": unreadable or []}).encode(),
        "/api/running": json.dumps({"running": running_rows or [], "finished": []}).encode(),
    }
