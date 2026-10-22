"""The rendered state of the menu, and the only place it is computed.

Three rules hold this file together.

**Everything shown is DERIVED from the last HTTP read** of the two lists, everything waiting on
the owner (``GET /api/waiting``) and everything running (``GET /api/running``). ``badge`` is a
property over the waiting list, not an integer kept beside it. A count maintained
next to a list is two facts that can disagree, and the one the user sees is the wrong
one. :data:`INSTANCE_ATTRS` pins the whole attribute set so adding a cached counter
later fails a test instead of quietly drifting.

**Nothing in here ever sees a WebSocket payload.** ``refresh()`` takes no arguments
carrying server data; the socket's only power is to call it. See ``doorbell``.

**Approve is offered only under the whole brief.** Each pending approval carries the
dashboard's brief of the call (``brief``) and the answers it may be given from here:
Approve only when every part of the brief is shown whole, Deny only when what Deny does
is, and "Review in PersonalClaw" always. Every other kind is answered where the list says
it is, a click away: answering one needs its own card, which only the dashboard shows.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from menubar_companion.api import GatewayClient, GatewayError, ResolveOutcome
from menubar_companion.brief import brief_of, excerpt, one_line

#: What each kind of waiting item is called, as every PersonalClaw surface calls it on a card
#: (core's ``web/src/lib/waitingList.ts``, ``WAITING_KIND_LABEL``).
KIND_LABEL = {
    "approval": "Approval",
    "question": "Question",
    "loop_question": "Loop",
    "gate": "Workflow",
    "park": "Automation",
    "browse_grant": "Browser",
    "plan_review": "Plan",
    "bridge_confirmation": "Control bridge",
}

#: The row of an item whose kind this version has no name for: still listed, still counted.
UNKNOWN_KIND = "Waiting on you"

#: The exact instance attributes a ``CompanionModel`` may hold. The badge-drift rail
#: asserts against this set: a future ``self._badge`` / ``self.pending_count`` is a
#: second source of truth for a number already derivable, and it fails here first.
INSTANCE_ATTRS = frozenset(
    {"client", "clock", "waiting", "unreadable", "running", "last_error", "_seen"}
)

#: The answers an approval's menu offers, in its order.
APPROVE = "Approve"
DENY = "Deny"
REVIEW = "Review in PersonalClaw"

#: How many lines of an item's text the menu shows under its row when the row cuts it; the rest
#: is a click away.
QUESTION_LINES = 3

#: The one ``interrupt`` that lets an item ping you now. The gateway decides it once for every
#: surface, from mute, quiet hours and the kind's notification rule (``personalclaw.waiting``).
DELIVER = "deliver"


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _record(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def waiting_text(kind: str, brief: dict) -> str:
    """An item's text in its source's own words (core's ``waitingText``): never empty for a kind
    this version knows."""
    if kind == "approval":
        return _text(brief.get("tool_purpose")) or _text(brief.get("tool")) or "A tool call"
    if kind == "question":
        questions = brief.get("questions")
        first = _record(questions[0] if isinstance(questions, list) and questions else None)
        return _text(first.get("question")) or _text(first.get("header")) or "A question"
    if kind == "loop_question":
        return _text(brief.get("question")) or "The loop asks you"
    if kind == "gate":
        ask = _record(brief.get("ask"))
        return (
            _text(ask.get("prompt"))
            or _text(ask.get("title"))
            or _text(brief.get("node_id"))
            or "The run waits at a gate"
        )
    if kind == "park":
        return (
            _text(brief.get("question"))
            or _text(_record(brief.get("card")).get("blocker"))
            or "The action stopped for you"
        )
    if kind == "browse_grant":
        return _text(brief.get("task")) or "A task asks to use your browser"
    if kind == "plan_review":
        name = _text(brief.get("name"))
        return (
            _text(_record(brief.get("step")).get("title"))
            or (f"Launch “{name}”" if name else "")
            or "A plan waits for your review"
        )
    if kind == "bridge_confirmation":
        return (
            _text(brief.get("body"))
            or _text(brief.get("action"))
            or "A control-bridge action waits for you"
        )
    return ""


def waiting_source(kind: str, brief: dict) -> str:
    """Where an item came from, in words (core's ``waitingSource``); ``""`` when it says nothing."""
    if kind == "approval":
        source = _text(brief.get("source_label"))
    elif kind in ("question", "bridge_confirmation"):
        source = _text(brief.get("asked_by"))
    elif kind in ("loop_question", "plan_review"):
        source = f"loop “{_text(brief.get('name'))}”" if _text(brief.get("name")) else ""
    elif kind == "gate":
        source = f"workflow “{_text(brief.get('workflow'))}”" if _text(brief.get("workflow")) else ""
    elif kind == "park":
        trigger = _text(brief.get("trigger_name"))
        source = f"automation “{trigger}”" if trigger else ""
    else:
        source = ""
    return f"From {source}" if source else ""


def _epoch(stamp: object) -> float | None:
    """An ISO instant the list sends, as epoch seconds; ``None`` for none or one unreadable."""
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp).timestamp()
    except ValueError:
        return None


@dataclass(frozen=True)
class ApprovalRow:
    """One pending approval, as the menu shows it (``brief.brief_of``)."""

    id: str
    title: str
    brief: tuple[str, ...] = ()
    can_approve: bool = False
    can_deny: bool = False
    review_link: str = ""

    @property
    def answers(self) -> tuple[str, ...]:
        """What this approval's menu offers under its brief, in order."""
        offered = ((APPROVE, self.can_approve), (DENY, self.can_deny), (REVIEW, True))
        return tuple(answer for answer, on_offer in offered if on_offer)


@dataclass(frozen=True)
class WaitingRow:
    """One thing waiting on you, as the menu shows it: its kind and its text in one row, where it
    came from, and the page that answers it. ``approval`` is set for an approval alone, the one
    kind answered from the menu."""

    id: str
    kind: str
    title: str
    text: str = ""
    source: str = ""
    link: str = ""
    interrupt: str = ""
    approval: ApprovalRow | None = None

    @property
    def text_lines(self) -> tuple[str, ...]:
        """The item's text under its row, when the row cuts it: its first lines."""
        if self.title.endswith("…"):
            return excerpt(self.text, QUESTION_LINES)
        return ()


@dataclass(frozen=True)
class RunRow:
    """One piece of live work, as the menu shows it: its name, what it is doing, its page."""

    work: str
    name: str
    line: str
    link: str = ""


@dataclass(frozen=True)
class Notice:
    """A notification for an item new to the list, in the words its row shows."""

    heading: str
    title: str


@dataclass(frozen=True)
class RefreshResult:
    """What changed on this read — the input to "should we notify?".

    Deltas are computed from the fetched data, so a notification can never fire for
    something the menu is not also showing.
    """

    ok: bool
    notices: tuple[Notice, ...] = ()
    error: str = ""


class CompanionModel:
    """Live gateway state, re-read on demand."""

    def __init__(self, client: GatewayClient, clock: Callable[[], float] = time.time):
        self.client = client
        #: The clock an item's ``expires_at`` is read against.
        self.clock = clock
        self.waiting: list[dict] = []
        #: The kinds the gateway could not read, said in the menu, never shown as nothing.
        self.unreadable: list[str] = []
        self.running: list[dict] = []
        #: The last write or read failure, for display. Never silently discarded.
        self.last_error: str = ""
        #: Ids already reported to the user, so the same item does not notify twice.
        self._seen: set[str] = set()

    # ── reads ──

    def refresh(self) -> RefreshResult:
        """Re-read both lists over HTTP and replace local state.

        Takes NO server-supplied argument on purpose. This is the signature the
        doorbell calls, and it is why a socket frame cannot inject state: there is no
        parameter for a payload to arrive through.
        """
        try:
            waiting, unreadable = self.client.get_waiting()
            running = self.client.get_running()
        except GatewayError as exc:
            self.last_error = str(exc)
            return RefreshResult(ok=False, error=str(exc))
        self.waiting = waiting
        self.unreadable = unreadable
        self.running = running
        self.last_error = ""
        rows = self.waiting_rows
        new = [row for row in rows if row.id not in self._seen]
        self._seen = {row.id for row in rows}
        return RefreshResult(
            ok=True,
            notices=tuple(
                Notice(
                    "Approval needed" if row.kind == "approval" else "PersonalClaw needs you",
                    row.title,
                )
                for row in new
                if row.interrupt == DELIVER
            ),
        )

    # ── derived views ──

    @property
    def waiting_rows(self) -> list[WaitingRow]:
        """Everything waiting on you, in the list's order: the gateway's, never sorted here."""
        out: list[WaitingRow] = []
        for item in self.waiting:
            item_id = _text(item.get("id"))
            if not item_id:
                continue
            kind = _text(item.get("kind"))
            brief = _record(item.get("brief"))
            text = waiting_text(kind, brief)
            label = KIND_LABEL.get(kind)
            approval = self._approval(item, brief) if kind == "approval" else None
            out.append(
                WaitingRow(
                    id=item_id,
                    kind=kind,
                    title=one_line(f"{label}: {text}") if label else UNKNOWN_KIND,
                    text=text,
                    source=waiting_source(kind, brief),
                    link=approval.review_link if approval else self.client.link(item.get("route")),
                    interrupt=_text(item.get("interrupt")),
                    approval=approval,
                )
            )
        return out

    def _approval(self, item: dict, brief: dict) -> ApprovalRow:
        """An approval item as its brief and answers: the registry row its brief carries, under
        the registry id its own route answers it by."""
        approval_id = _text(item.get("id")).removeprefix("approval:")
        shown = brief_of({**brief, "id": approval_id, "risk": item.get("risk")})
        expires = _epoch(item.get("expires_at"))
        # Past answering, a reader disables the action itself: only the review is offered.
        answerable = item.get("current") is not False and (
            expires is None or self.clock() < expires
        )
        return ApprovalRow(
            id=approval_id,
            title=one_line(f"{KIND_LABEL['approval']}: {waiting_text('approval', brief)}"),
            brief=shown.lines,
            can_approve=answerable and shown.approvable,
            can_deny=answerable and shown.deniable,
            review_link=self.client.review_link(approval_id),
        )

    @property
    def pending_approvals(self) -> list[ApprovalRow]:
        return [row.approval for row in self.waiting_rows if row.approval is not None]

    @property
    def runs(self) -> list[RunRow]:
        """Everything running, in the list's order, each saying what it is doing."""
        return [
            RunRow(
                work=_text(row.get("work")),
                name=one_line(_text(row.get("name")) or "Work"),
                line=one_line(_text(row.get("line"))),
                link=self.client.link(row.get("route")),
            )
            for row in self.running
            if _text(row.get("work"))
        ]

    @property
    def badge(self) -> int:
        """What is waiting on the owner: the waiting list's length, every kind.

        Derived on read from the same list the menu renders, so the number and the
        rows it summarises cannot disagree.
        """
        return len(self.waiting_rows)

    @property
    def badge_text(self) -> str:
        return "" if self.badge == 0 else str(self.badge)

    @property
    def unreadable_line(self) -> str:
        """The kinds the gateway could not read, by name, or ``""``."""
        if not self.unreadable:
            return ""
        return "Could not read: " + ", ".join(KIND_LABEL.get(k, k) for k in self.unreadable)

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
    rows = model.waiting_rows
    lines.append(f"Waiting on you: {len(rows)}")
    for row in rows:
        if row.approval is not None:
            approval = row.approval
            lines.append(f"  • {row.title}  ({approval.id})")
            lines.extend(f"      {line}" for line in approval.brief)
            answers = [
                f"[{a} → {approval.review_link}]" if a == REVIEW else f"[{a}]"
                for a in approval.answers
            ]
            lines.append(f"      {' '.join(answers)}")
            continue
        lines.append(f"  • {row.title} → {row.link}")
        lines.extend(f"      {line}" for line in row.text_lines)
        if row.source:
            lines.append(f"      {row.source}")
    runs = model.runs
    lines.append(f"Running: {len(runs)}")
    for run in runs:
        lines.append(f"  • {run.name} — {run.line}" if run.line else f"  • {run.name}")
    lines.append(f"Notifications: {'muted' if muted else 'on'}")
    if model.unreadable_line:
        # A source the gateway could not read is said, never shown as nothing waiting.
        lines.append(f"⚠ {model.unreadable_line}")
    if model.last_error:
        # The failure is shown on the surface that caused it, not only in a log.
        lines.append(f"⚠ {model.last_error}")
    return MenuText(badge=model.badge_text, lines=lines)
