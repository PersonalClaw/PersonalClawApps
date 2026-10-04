"""Rows shaped as the gateway sends them, for the menu's tests.

:func:`approval` is one ``GET /api/approvals`` row carrying every field core's pending-approval
entry has (``DashboardApprovalState._approval_entry``), so a test reads the brief from the fields
the gateway really sends rather than from fields a fixture made up.
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
