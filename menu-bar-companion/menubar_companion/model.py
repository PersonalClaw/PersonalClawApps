"""The rendered state of the menu, and the only place it is computed.

Two rules hold this file together.

**Everything shown is DERIVED from the last HTTP read.** ``badge`` is a property over
``approvals`` and ``needs_input``, not an integer kept beside them. A count maintained
next to a list is two facts that can disagree, and the one the user sees is the wrong
one. :data:`INSTANCE_ATTRS` pins the whole attribute set so adding a cached counter
later fails a test instead of quietly drifting.

**Nothing in here ever sees a WebSocket payload.** ``refresh()`` takes no arguments
carrying server data; the socket's only power is to call it. See ``doorbell``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from menubar_companion.api import GatewayClient, ResolveOutcome

#: Loop statuses that mean the run is over (``personalclaw.loop.loop.LOOP_PHASES``
#: calls these ENDED). Everything else is live enough to belong in the menu.
ENDED_STATUSES = frozenset({"complete", "failed", "stopped"})

#: The one status that means the loop is blocked on the owner personally.
NEEDS_INPUT = "needs_input"

#: The exact instance attributes a ``CompanionModel`` may hold. The badge-drift rail
#: asserts against this set: a future ``self._badge`` / ``self.pending_count`` is a
#: second source of truth for a number already derivable, and it fails here first.
INSTANCE_ATTRS = frozenset({"client", "loops", "approvals", "last_error", "_seen"})


def _row_id(row: dict) -> str:
    return str(row.get("id", "")).strip()


@dataclass(frozen=True)
class RunRow:
    """One live run, as the menu shows it."""

    id: str
    label: str
    status: str
    needs_input: bool
    deep_link: str = ""
    question: str = ""


@dataclass(frozen=True)
class ApprovalRow:
    """One pending approval, as the menu shows it."""

    id: str
    label: str
    detail: str = ""


@dataclass(frozen=True)
class RefreshResult:
    """What changed on this read — the input to "should we notify?".

    Deltas are computed from the fetched data, so a notification can never fire for
    something the menu is not also showing.
    """

    ok: bool
    new_approvals: tuple[str, ...] = ()
    new_needs_input: tuple[str, ...] = ()
    error: str = ""


class CompanionModel:
    """Live gateway state, re-read on demand."""

    def __init__(self, client: GatewayClient):
        self.client = client
        self.loops: list[dict] = []
        self.approvals: list[dict] = []
        #: The last write or read failure, for display. Never silently discarded.
        self.last_error: str = ""
        #: Ids already reported to the user, so the same approval does not notify twice.
        self._seen: set[str] = set()

    # ── reads ──

    def refresh(self) -> RefreshResult:
        """Re-read both lists over HTTP and replace local state.

        Takes NO server-supplied argument on purpose. This is the signature the
        doorbell calls, and it is why a socket frame cannot inject state: there is no
        parameter for a payload to arrive through.
        """
        from menubar_companion.api import GatewayError

        try:
            loops = self.client.get_loops()
            approvals = self.client.get_approvals()
        except GatewayError as exc:
            self.last_error = str(exc)
            return RefreshResult(ok=False, error=str(exc))
        self.loops = loops
        self.approvals = approvals
        self.last_error = ""
        ids = {_row_id(r) for r in self.approvals} | {r.id for r in self.needs_input}
        ids.discard("")
        new = tuple(sorted(ids - self._seen))
        self._seen = ids
        approval_ids = {_row_id(r) for r in self.approvals}
        return RefreshResult(
            ok=True,
            new_approvals=tuple(i for i in new if i in approval_ids),
            new_needs_input=tuple(i for i in new if i not in approval_ids),
        )

    # ── derived views ──

    @property
    def runs(self) -> list[RunRow]:
        """Live run rows, newest first (the order ``GET /api/loops`` returns)."""
        out: list[RunRow] = []
        for row in self.loops:
            status = str(row.get("status", "")).strip()
            if status in ENDED_STATUSES:
                continue
            loop_id = _row_id(row)
            blocked = status == NEEDS_INPUT
            out.append(
                RunRow(
                    id=loop_id,
                    label=str(row.get("name") or row.get("task") or loop_id or "(untitled)"),
                    status=status,
                    needs_input=blocked,
                    # Only a blocked run gets a deep link: the link exists to take the
                    # owner to the thing waiting on them, not to decorate every row.
                    deep_link=self.client.deep_link(loop_id) if blocked and loop_id else "",
                    question=str(row.get("pending_question") or "") if blocked else "",
                )
            )
        return out

    @property
    def needs_input(self) -> list[RunRow]:
        return [r for r in self.runs if r.needs_input]

    @property
    def pending_approvals(self) -> list[ApprovalRow]:
        out: list[ApprovalRow] = []
        for row in self.approvals:
            label = str(row.get("tool") or row.get("title") or row.get("name") or "approval")
            detail = str(row.get("summary") or row.get("description") or "")
            out.append(ApprovalRow(id=_row_id(row), label=label, detail=detail))
        return out

    @property
    def badge(self) -> int:
        """What is blocked on the owner: pending approvals + runs needing input.

        Derived on read from the same two lists the menu renders, so the number and the
        rows it summarises cannot disagree.
        """
        return len(self.pending_approvals) + len(self.needs_input)

    @property
    def badge_text(self) -> str:
        return "" if self.badge == 0 else str(self.badge)

    # ── the one write ──

    def resolve(self, approval_id: str, action: str) -> ResolveOutcome:
        """Approve or deny, and REPORT a failure instead of swallowing it.

        On failure: ``last_error`` is set (the menu renders it) and local state is left
        exactly as it was, so the row still reads as pending and the badge still counts
        it. On success: nothing is removed locally either — the next ``refresh()``
        (which the resolve triggers) is what makes the row disappear. Optimistically
        dropping the row is how a failed write comes to look like a successful one.
        """
        outcome = self.client.resolve_approval(approval_id, action)
        if not outcome.ok:
            self.last_error = outcome.error
            return outcome
        self.last_error = ""
        self.refresh()
        return outcome


@dataclass
class MenuText:
    """The menu as flat lines — what the status-item host renders, and what a headless
    ``--check`` prints. Keeping it a value makes the surface assertable without a GUI.
    """

    badge: str
    lines: list[str] = field(default_factory=list)


def render(model: CompanionModel, muted: bool = False) -> MenuText:
    """Compose the menu. Pure over the model; no I/O."""
    lines: list[str] = []
    approvals = model.pending_approvals
    lines.append(f"Approvals waiting: {len(approvals)}")
    for row in approvals:
        lines.append(f"  • {row.label} [Approve] [Deny]  ({row.id})")
    blocked = model.needs_input
    lines.append(f"Needs your input: {len(blocked)}")
    for row in blocked:
        lines.append(f"  • {row.label} → {row.deep_link}")
    runs = [r for r in model.runs if not r.needs_input]
    lines.append(f"Running: {len(runs)}")
    for row in runs:
        lines.append(f"  • {row.label} ({row.status})")
    lines.append(f"Notifications: {'muted' if muted else 'on'}")
    if model.last_error:
        # The failure is shown on the surface that caused it, not only in a log.
        lines.append(f"⚠ {model.last_error}")
    return MenuText(badge=model.badge_text, lines=lines)
