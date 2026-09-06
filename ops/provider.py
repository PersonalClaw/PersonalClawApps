"""The `ops` tool provider — an on-call first responder on the agent tool layer.

Nine tools over one incident ledger: ``ops_watch``, ``ops_queue``, ``ops_incident``,
``ops_claim``, ``ops_investigate``, ``ops_record``, ``ops_propose_fix``,
``ops_apply_fix``, ``ops_resolve``. They walk the shift the way a responder does — watch,
triage, claim, investigate, write down what you found, propose a fix — and then stop.

**Eight of the nine cannot change anything outside the ledger.** ``ops_apply_fix`` is the
only path to a side effect, and it is gated four ways: the ``allow_apply`` setting is off
by default, the call must pass ``confirm: true``, it must echo the confirm token that
digests the proposal it is applying, and the proposal must name an action still declared
in the matched runbook with the same argv. On top of that the tool carries
``requires_approval=True`` and ``RiskLevel.DESTRUCTIVE``, so the host's own approval
prompt stands in front of all four. ``ops_propose_fix`` writes a plan and nothing else.

Everything that came out of an alarm payload, a runbook or a command's output is fenced
with ``personalclaw.sdk.security.fence_untrusted`` before a model sees it. An alarm is the
canonical untrusted input for this app: it is machine-generated text that quotes log
lines, hostnames and URLs from wherever the failure happened.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from personalclaw.sdk.security import fence_untrusted
from personalclaw.sdk.tool import RiskLevel, ToolDefinition, ToolProvider, ToolResult

from incidents import (
    APP_NAME,
    SEVERITIES,
    TERMINAL_STATES,
    IncidentMissing,
    IncidentRefError,
    Ledger,
    LedgerError,
    parse_proposal_id,
    priority,
    severity_at_least,
)
from runbooks import GENERIC_CHECKS, RunbookError, RunbookLibrary, run_action

logger = logging.getLogger("ops")

DEFAULT_TIMEOUT = 60
_QUEUE_HINT = "Call ops_queue to see the open incidents and their ids."


class OpsProvider(ToolProvider):
    """On-call intake, triage and confirm-gated remediation over a local ledger."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = dict(config or {})
        self._spool_setting = str(self._config.get("spool_dir") or "").strip()
        self._runbooks_setting = str(self._config.get("runbooks_dir") or "").strip()
        self._on_call = str(self._config.get("on_call") or "").strip() or "on-call"
        self._allow_apply = bool(self._config.get("allow_apply", False))
        self._timeout = max(5, min(900, int(self._config.get("timeout_secs", DEFAULT_TIMEOUT)
                                            or DEFAULT_TIMEOUT)))
        self._ledger_impl: Ledger | None = None

    # ── Lazily bound state ──────────────────────────────────────────────────────

    @property
    def _ledger(self) -> Ledger:
        """The ledger, bound on first use.

        Lazy on purpose: core constructs a provider just to READ its tool list (Settings →
        Tools, the manifest round-trip), and that must not create directories under the
        user's home. The ledger appears the first time an incident is actually touched.
        """
        if self._ledger_impl is None:
            self._ledger_impl = Ledger()
        return self._ledger_impl

    @property
    def _spool(self) -> Path:
        if self._spool_setting:
            return Path(self._spool_setting).expanduser()
        return self._ledger.root / "spool"

    @property
    def _runbooks(self) -> RunbookLibrary:
        if self._runbooks_setting:
            return RunbookLibrary(Path(self._runbooks_setting).expanduser())
        return RunbookLibrary(self._ledger.root / "runbooks")

    # ── Identity ────────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return APP_NAME

    @property
    def display_name(self) -> str:
        return "Ops"

    def info(self) -> dict[str, Any]:
        # Reports the CONFIGURED paths, not the resolved ones: resolving falls back to the
        # ledger, and binding the ledger creates directories.
        return {
            "spool_dir": self._spool_setting or "(this app's data dir)/spool",
            "runbooks_dir": self._runbooks_setting or "(this app's data dir)/runbooks",
            "on_call": self._on_call,
            "allow_apply": self._allow_apply,
            "timeout_secs": self._timeout,
        }

    # ── Tool surface ────────────────────────────────────────────────────────────

    async def list_tools(self) -> list[ToolDefinition]:
        incident_param = {
            "type": "string",
            "description": "The incident id, e.g. 'inc-0a1b2c3d4e5f'. From ops_queue.",
        }
        return [
            ToolDefinition(
                name="ops_watch",
                description=(
                    "Sweep the alarm spool and file anything new as an incident, then "
                    "return the ranked open queue. Run this first on every shift and at "
                    "the top of every unattended cycle. Files nothing twice: a spool file "
                    "already read with the same contents is skipped, and a repeat firing "
                    "of an alarm bumps that incident's count instead of opening a second."
                ),
                provider=self.name,
                parameters={"type": "object", "properties": {}},
                requires_approval=False,
                risk_level=RiskLevel.CAUTION,
                max_output=40_000,
            ),
            ToolDefinition(
                name="ops_queue",
                description=(
                    "The open incidents, worst first, each with the score that put it "
                    "there broken down by term (severity, paging, unclaimed, age, "
                    "repeats, no-runbook). Read-only."
                ),
                provider=self.name,
                parameters={
                    "type": "object",
                    "properties": {
                        "min_severity": {
                            "type": "string",
                            "enum": list(SEVERITIES),
                            "description": (
                                "Only incidents at this severity or worse. Unreadable "
                                "severities rank above 'low', so a floor of 'medium' keeps "
                                "them."
                            ),
                        },
                        "unclaimed_only": {
                            "type": "boolean",
                            "description": "Only incidents nobody has claimed yet.",
                        },
                        "limit": {"type": "integer", "description": "How many rows (default 20)."},
                    },
                },
                requires_approval=False,
                risk_level=RiskLevel.SAFE,
                max_output=40_000,
            ),
            ToolDefinition(
                name="ops_incident",
                description=(
                    "Everything on one incident: the alarm as it arrived, the matched "
                    "runbook, the timeline of findings, and every fix proposed so far with "
                    "its confirm token. Read-only."
                ),
                provider=self.name,
                parameters={
                    "type": "object",
                    "properties": {"incident": incident_param},
                    "required": ["incident"],
                },
                requires_approval=False,
                risk_level=RiskLevel.SAFE,
                max_output=60_000,
            ),
            ToolDefinition(
                name="ops_claim",
                description=(
                    "Take ownership of an incident before working it — the ledger has to "
                    "know who is on it, and investigate/propose refuse an unclaimed one. "
                    "Pass release=true to hand it back."
                ),
                provider=self.name,
                parameters={
                    "type": "object",
                    "properties": {
                        "incident": incident_param,
                        "owner": {
                            "type": "string",
                            "description": (
                                "Who is taking it. Defaults to the on-call name in "
                                "Settings."
                            ),
                        },
                        "release": {
                            "type": "boolean",
                            "description": "Release the claim instead of taking it.",
                        },
                    },
                    "required": ["incident"],
                },
                requires_approval=False,
                risk_level=RiskLevel.CAUTION,
            ),
            ToolDefinition(
                name="ops_investigate",
                description=(
                    "The investigation plan for a claimed incident: the matched runbook's "
                    "own checks plus the generic first-responder checks. Walk them with "
                    "your read-only tools and write each answer back with ops_record. This "
                    "tool runs no checks itself."
                ),
                provider=self.name,
                parameters={
                    "type": "object",
                    "properties": {"incident": incident_param},
                    "required": ["incident"],
                },
                requires_approval=False,
                risk_level=RiskLevel.SAFE,
                max_output=40_000,
            ),
            ToolDefinition(
                name="ops_record",
                description=(
                    "Append one finding to a claimed incident's timeline — what you looked "
                    "at and what it said, including 'checked and it was fine'. A negative "
                    "result is worth recording; the next responder needs to know it was "
                    "ruled out."
                ),
                provider=self.name,
                parameters={
                    "type": "object",
                    "properties": {
                        "incident": incident_param,
                        "finding": {
                            "type": "string",
                            "description": "What you observed, in your own words.",
                        },
                        "verdict": {
                            "type": "string",
                            "description": (
                                "Optional short conclusion, e.g. 'ruled out' or "
                                "'root cause'."
                            ),
                        },
                    },
                    "required": ["incident", "finding"],
                },
                requires_approval=False,
                risk_level=RiskLevel.CAUTION,
            ),
            ToolDefinition(
                name="ops_propose_fix",
                description=(
                    "Write down a PROPOSED fix and get back its confirm token. This changes "
                    "nothing outside the ledger — it is the artefact a human reads before "
                    "deciding. Say what changes, what it touches and how to undo it; all "
                    "three are required. Name a runbook `action` only if the operator's "
                    "runbook already declares it."
                ),
                provider=self.name,
                parameters={
                    "type": "object",
                    "properties": {
                        "incident": incident_param,
                        "summary": {
                            "type": "string",
                            "description": "The fix, stated so a human can weigh it.",
                        },
                        "blast_radius": {
                            "type": "string",
                            "description": "What this touches, and what else could be affected.",
                        },
                        "rollback": {
                            "type": "string",
                            "description": "How to undo it if it makes things worse.",
                        },
                        "action": {
                            "type": "string",
                            "description": (
                                "An action name from the incident's runbook, if this fix is "
                                "one the operator already declared. Omit for a fix a human "
                                "carries out by hand."
                            ),
                        },
                    },
                    "required": ["incident", "summary", "blast_radius", "rollback"],
                },
                requires_approval=False,
                risk_level=RiskLevel.CAUTION,
            ),
            ToolDefinition(
                name="ops_apply_fix",
                description=(
                    "Run a proposed runbook action. THE ONLY TOOL HERE THAT CHANGES "
                    "ANYTHING OUTSIDE THE LEDGER, and it needs all of: 'Allow gated "
                    "remediation' switched on in Settings (off by default), confirm=true, "
                    "the proposal's exact confirm_token, and an action still declared in "
                    "the runbook with the same argv. Never call this in an unattended run — "
                    "propose instead and let a human confirm."
                ),
                provider=self.name,
                parameters={
                    "type": "object",
                    "properties": {
                        "incident": incident_param,
                        "proposal": {
                            "type": "string",
                            "description": "The proposal id, e.g. 'prop-0a1b2c3d'.",
                        },
                        "confirm_token": {
                            "type": "string",
                            "description": (
                                "The token ops_propose_fix returned for THIS proposal. It "
                                "digests the plan, so an edited plan needs a new confirm."
                            ),
                        },
                        "confirm": {
                            "type": "boolean",
                            "description": "Must be true. There is no default.",
                        },
                    },
                    "required": ["incident", "proposal", "confirm_token", "confirm"],
                },
                requires_approval=True,
                risk_level=RiskLevel.DESTRUCTIVE,
                max_output=20_000,
            ),
            ToolDefinition(
                name="ops_resolve",
                description=(
                    "Close an incident as 'resolved' or 'dismissed' with a note. It leaves "
                    "the queue but keeps its whole timeline, and it reopens by itself if "
                    "the alarm fires again."
                ),
                provider=self.name,
                parameters={
                    "type": "object",
                    "properties": {
                        "incident": incident_param,
                        "state": {
                            "type": "string",
                            "enum": list(TERMINAL_STATES),
                            "description": (
                                "'resolved' (it is fixed) or 'dismissed' (it was "
                                "noise)."
                            ),
                        },
                        "note": {"type": "string", "description": "Why. One or two sentences."},
                    },
                    "required": ["incident", "state", "note"],
                },
                requires_approval=False,
                risk_level=RiskLevel.CAUTION,
            ),
        ]

    async def invoke(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        handlers = {
            "ops_watch": self._watch,
            "ops_queue": self._queue,
            "ops_incident": self._detail,
            "ops_claim": self._claim,
            "ops_investigate": self._investigate,
            "ops_record": self._record,
            "ops_propose_fix": self._propose_fix,
            "ops_apply_fix": self._apply_fix,
            "ops_resolve": self._resolve,
        }
        handler = handlers.get(tool_name)
        if handler is None:
            return ToolResult(
                success=False,
                error=f"Unknown tool: {tool_name!r}",
                recovery_hints=[f"This provider exposes: {', '.join(sorted(handlers))}."],
            )
        try:
            return await handler(arguments)
        except IncidentRefError as exc:
            return ToolResult(success=False, error=str(exc), recovery_hints=[_QUEUE_HINT])
        except IncidentMissing as exc:
            return ToolResult(success=False, error=str(exc), recovery_hints=[_QUEUE_HINT])
        except (LedgerError, RunbookError) as exc:
            return ToolResult(success=False, error=str(exc))
        except OSError as exc:
            # A ledger on a full disk, a read-only mount, a spool the gateway user cannot
            # read: a legible failure, not a traceback out of the tool layer.
            logger.warning("ops storage failed for %s: %s", tool_name, exc)
            return ToolResult(
                success=False,
                error=f"The incident ledger could not be read or written: {exc}",
                recovery_hints=[
                    "Check that the spool and runbook folders in Settings exist and are "
                    "readable by PersonalClaw.",
                ],
            )

    # ── Handlers ────────────────────────────────────────────────────────────────

    async def _watch(self, _args: dict[str, Any]) -> ToolResult:
        library = self._runbooks
        spool = self._spool
        report = await asyncio.to_thread(
            self._ledger.sweep, spool, match_runbook=library.match
        )
        _, problems = await asyncio.to_thread(library.load_all)
        logger.info(
            "sweep %s: %d file(s) read, %d skipped, %d unreadable, %d opened, %d refired",
            spool, report["files_read"], report["files_skipped"], report["unreadable"],
            len(report["opened"]), len(report["refired"]),
        )
        lines = [
            f"Swept `{spool}`: {report['files_read']} new file(s), "
            f"{report['files_skipped']} unchanged, {report['unreadable']} unreadable.",
            f"{len(report['opened'])} incident(s) opened, {len(report['refired'])} reopened, "
            f"{len(report['updated'])} refired.",
        ]
        if report.get("error"):
            lines.append(
                f"The spool folder could not be listed: {report['error']}. Create it, or "
                "point `spool_dir` at where your monitor writes."
            )
        if report.get("truncated"):
            lines.append(
                "The spool held more files than one sweep reads; the rest come in on the "
                "next sweep."
            )
        if problems:
            lines.append(
                f"{len(problems)} runbook file(s) would not load and matched nothing: "
                + "; ".join(problems[:5])
            )
        queue_body, queue_meta = await asyncio.to_thread(self._queue_table, "", False, 20)
        return ToolResult(
            success=True,
            output="\n".join(lines) + "\n\n" + queue_body,
            metadata={**report, "runbook_problems": problems, **queue_meta},
        )

    async def _queue(self, args: dict[str, Any]) -> ToolResult:
        limit = args.get("limit")
        try:
            cap = max(1, min(200, int(limit))) if limit is not None else 20
        except (TypeError, ValueError):
            cap = 20
        floor = str(args.get("min_severity") or "").strip().lower()
        body, meta = await asyncio.to_thread(
            self._queue_table, floor, bool(args.get("unclaimed_only")), cap
        )
        return ToolResult(success=True, output=body, metadata=meta)

    def _queue_table(
        self, floor: str, unclaimed_only: bool, cap: int
    ) -> tuple[str, dict[str, Any]]:
        """Render the ranked queue. Synchronous — called from a thread."""
        scored, unreadable = self._ledger.queue()
        rows = [
            (inc, score) for inc, score in scored
            if severity_at_least(inc.alarm.severity, floor)
            and not (unclaimed_only and inc.owner)
        ]
        shown = rows[:cap]
        if not shown:
            note = "No open incident matches." if scored else "The queue is empty."
            tail = (f" {unreadable} incident record(s) will not parse and are not shown."
                    if unreadable else "")
            return note + tail, {"open": len(scored), "shown": 0, "unreadable": unreadable}
        table = ["| incident | score | severity | alarm | resource | state | owner | runbook |",
                 "|---|---|---|---|---|---|---|---|"]
        for inc, score in shown:
            table.append(
                f"| `{inc.id}` | {score['score']} | {inc.alarm.severity} | {inc.alarm.name} | "
                f"{inc.alarm.resource or '—'} | {inc.state} | {inc.owner or '—'} | "
                f"{inc.runbook or '—'} |"
            )
        # The alarm names and resources in this table came out of a monitor payload, so the
        # table is untrusted content and is fenced like any other.
        fenced = fence_untrusted(
            "\n".join(table), source="on-call incident queue",
            source_type="ops_queue", source_id=str(self._ledger.root),
        )
        tail = ""
        if len(rows) > cap:
            tail = f"\n\nShowing {cap} of {len(rows)} matching incident(s)."
        if unreadable:
            tail += (f"\n\n{unreadable} incident record(s) will not parse and are not shown — "
                     "they have to be repaired or removed by hand.")
        return (
            f"{len(rows)} open incident(s), worst first:\n\n{fenced}{tail}",
            {
                "open": len(scored), "shown": len(shown), "unreadable": unreadable,
                "queue": [
                    {**inc.to_dict(), "priority": score} for inc, score in shown
                ],
            },
        )

    async def _detail(self, args: dict[str, Any]) -> ToolResult:
        incident = await asyncio.to_thread(self._ledger.load, str(args.get("incident") or ""))
        score = priority(incident)
        alarm = incident.alarm
        # The header carries only facts this app or the operator authored — the ledger's own
        # state, the validated runbook name, the derived score. The alarm's NAME and
        # RESOURCE came out of a monitor payload, so they live inside the fence with its
        # message rather than being quoted as if this app had said them.
        header = (
            f"# `{incident.id}` — {incident.state}, {alarm.severity}"
            f"{', paging' if alarm.page else ''}\n\n"
            f"- **first seen**: {incident.first_seen} · **last**: {incident.last_seen} · "
            f"**firings**: {incident.occurrences}\n"
            f"- **owner**: {incident.owner or 'unclaimed'}\n"
            f"- **runbook**: {incident.runbook or 'none matched'}\n"
            f"- **priority**: {score['score']} {score['terms']}\n"
        )
        body = [
            f"## Alarm\n\n- name: {alarm.name}\n- resource: {alarm.resource or '—'}\n\n"
            f"### Alarm text\n\n{alarm.summary or '(the payload carried no message)'}"
        ]
        if incident.timeline:
            body.append("## Timeline\n\n" + "\n".join(
                f"- `{e.get('at')}` **{e.get('kind')}** — {e.get('detail')}"
                + (f" _[{e['verdict']}]_" if e.get("verdict") else "")
                for e in incident.timeline
            ))
        if incident.proposals:
            body.append("## Proposals\n\n" + "\n\n".join(
                f"- **`{p.get('id')}`** ({'APPLIED' if p.get('applied') else 'proposed'})\n"
                f"  - fix: {p.get('summary')}\n"
                f"  - action: {p.get('action') or '(by hand — no runbook action)'}"
                + (f"\n  - argv: `{' '.join(p.get('argv') or [])}`" if p.get("argv") else "")
                + f"\n  - blast radius: {p.get('blast_radius')}"
                f"\n  - rollback: {p.get('rollback')}"
                f"\n  - confirm_token: `{p.get('confirm_token')}`"
                for p in incident.proposals
            ))
        fenced = fence_untrusted(
            "\n\n".join(body), source=f"incident {incident.id} ({alarm.name})",
            source_type="ops_incident", source_id=incident.id,
        )
        return ToolResult(
            success=True,
            output=header + "\n" + fenced,
            metadata={**incident.to_dict(), "priority": score},
        )

    async def _claim(self, args: dict[str, Any]) -> ToolResult:
        incident_id = str(args.get("incident") or "")
        if bool(args.get("release")):
            incident = await asyncio.to_thread(self._ledger.release, incident_id)
            logger.info("incident %s released", incident.id)
            return ToolResult(
                success=True,
                output=f"`{incident.id}` released — it is back in the queue as "
                       f"{incident.state}.",
                metadata={"id": incident.id, "state": incident.state, "owner": ""},
            )
        owner = str(args.get("owner") or "").strip() or self._on_call
        incident = await asyncio.to_thread(self._ledger.claim, incident_id, owner)
        logger.info("incident %s claimed by %s", incident.id, incident.owner)
        return ToolResult(
            success=True,
            output=(
                f"`{incident.id}` claimed by **{incident.owner}** at {incident.claimed_at}. "
                f"Next: ops_investigate to get the plan"
                + (f" (runbook `{incident.runbook}`)." if incident.runbook
                   else " — no runbook matched, so it is the generic checklist.")
            ),
            metadata={"id": incident.id, "state": incident.state, "owner": incident.owner,
                      "runbook": incident.runbook},
        )

    async def _investigate(self, args: dict[str, Any]) -> ToolResult:
        incident = await asyncio.to_thread(
            self._ledger.require_claimed, str(args.get("incident") or "")
        )
        book = None
        if incident.runbook:
            try:
                book = await asyncio.to_thread(self._runbooks.get, incident.runbook)
            except RunbookError as exc:
                # The runbook matched at intake and has since been renamed, broken or
                # removed. That is worth saying out loud rather than silently falling back.
                incident.note("runbook-missing", str(exc))
                await asyncio.to_thread(self._ledger.save, incident)
        sections = [
            f"## Incident\n\n- alarm: {incident.alarm.name}\n"
            f"- resource: {incident.alarm.resource or '—'}\n"
            f"- what it said: {incident.alarm.summary or '(no message)'}"
        ]
        if book is not None:
            checks = "\n".join(f"{i}. {c}" for i, c in enumerate(book.checks, 1)) \
                or "(the runbook lists no checks)"
            sections.append(f"## Runbook `{book.name}` — {book.title}\n\n{checks}")
            if book.actions:
                sections.append(
                    "### Declared remediations (propose one by name; none of them run here)\n\n"
                    + "\n".join(
                        f"- `{a.name}` — {a.description or '(no description)'}\n"
                        f"  - argv: `{' '.join(a.argv)}`\n"
                        f"  - blast radius: {a.blast_radius or '(not stated in the runbook)'}\n"
                        f"  - rollback: {a.rollback or '(not stated in the runbook)'}"
                        for a in book.actions
                    )
                )
            else:
                sections.append(
                    "### Declared remediations\n\nNone. Any fix here is a plan for a human "
                    "to carry out — propose it without an `action`."
                )
        else:
            sections.append(
                "## No runbook\n\nNothing in the runbook folder matched this alarm, so this "
                "is the generic checklist. Consider writing a runbook for it afterwards."
            )
        sections.append(
            "## First-responder checks\n\n"
            + "\n".join(f"{i}. {c}" for i, c in enumerate(GENERIC_CHECKS, 1))
        )
        # A runbook is the operator's own file, but it is still read off disk and quoted
        # into a model's context, so it is fenced with the rest.
        fenced = fence_untrusted(
            "\n\n".join(sections), source=f"investigation plan for {incident.id}",
            source_type="ops_plan", source_id=incident.id,
        )
        if incident.state == "claimed":
            incident.state = "investigating"
            incident.note("investigating", "investigation plan issued")
            await asyncio.to_thread(self._ledger.save, incident)
        return ToolResult(
            success=True,
            output=(
                # The alarm's name is deliberately absent from this line: it is payload text,
                # and it is already inside the fenced plan below.
                f"Plan for `{incident.id}` ({incident.alarm.severity}). Walk it with your "
                f"own read-only tools and write each answer back with ops_record.\n\n{fenced}"
            ),
            metadata={
                "id": incident.id, "state": incident.state, "runbook": incident.runbook,
                "runbook_checks": list(book.checks) if book else [],
                "generic_checks": list(GENERIC_CHECKS),
                "declared_actions": [a.name for a in (book.actions if book else ())],
            },
        )

    async def _record(self, args: dict[str, Any]) -> ToolResult:
        incident = await asyncio.to_thread(
            self._ledger.record, str(args.get("incident") or ""),
            str(args.get("finding") or ""), verdict=str(args.get("verdict") or ""),
        )
        # The finding itself is not logged: it can quote a log line or a customer record.
        logger.info("incident %s recorded a finding (%d on the timeline)", incident.id,
                    len(incident.timeline))
        return ToolResult(
            success=True,
            output=f"Recorded on `{incident.id}` — {len(incident.timeline)} timeline "
                   f"entr{'y' if len(incident.timeline) == 1 else 'ies'} now.",
            metadata={"id": incident.id, "state": incident.state,
                      "timeline_entries": len(incident.timeline)},
        )

    async def _propose_fix(self, args: dict[str, Any]) -> ToolResult:
        incident_id = str(args.get("incident") or "")
        action_name = str(args.get("action") or "").strip().lower()
        argv: list[str] = []
        declared = None
        if action_name:
            incident = await asyncio.to_thread(self._ledger.require_claimed, incident_id)
            if not incident.runbook:
                raise LedgerError(
                    f"{incident.id} matched no runbook, so there is no declared action to "
                    "name. Propose the fix without `action` and a human carries it out."
                )
            book = await asyncio.to_thread(self._runbooks.get, incident.runbook)
            declared = book.action(action_name)
            argv = list(declared.argv)
        incident, proposal = await asyncio.to_thread(
            self._ledger.add_proposal, incident_id,
            summary=str(args.get("summary") or ""),
            blast_radius=str(args.get("blast_radius") or ""),
            rollback=str(args.get("rollback") or ""),
            action=action_name, argv=argv,
        )
        logger.info("incident %s proposed %s (action=%s)", incident.id, proposal["id"],
                    action_name or "-")
        how = (
            f"It names runbook action `{action_name}` (`{' '.join(argv)}`). Applying it "
            "needs a human: ops_apply_fix with this token, confirm=true, and 'Allow gated "
            "remediation' switched on in Settings."
            if action_name else
            "It names no runbook action, so nothing here can run it — it is a plan for a "
            "human to carry out."
        )
        return ToolResult(
            success=True,
            output=(
                f"Proposed `{proposal['id']}` on `{incident.id}`. **Nothing has been "
                f"changed.**\n\n"
                f"- fix: {proposal['summary']}\n"
                f"- blast radius: {proposal['blast_radius']}\n"
                f"- rollback: {proposal['rollback']}\n"
                f"- confirm_token: `{proposal['confirm_token']}`\n\n{how}"
            ),
            metadata={"id": incident.id, "state": incident.state, "proposal": proposal,
                      "applied": False},
        )

    async def _apply_fix(self, args: dict[str, Any]) -> ToolResult:
        """The one gate. Four independent refusals stand in front of the subprocess."""
        incident_id = str(args.get("incident") or "")
        proposal_id = parse_proposal_id(str(args.get("proposal") or ""))
        incident = await asyncio.to_thread(self._ledger.load, incident_id)
        proposal = incident.proposal(proposal_id)

        if not self._allow_apply:
            logger.warning("apply refused for %s/%s: allow_apply is off", incident.id,
                           proposal_id)
            return ToolResult(
                success=False,
                error=(
                    "'Allow gated remediation' is off, so this app cannot run anything. "
                    "The proposal stands; carry it out by hand, or switch the setting on "
                    "in Settings → Tools → Ops if you want this app to be able to."
                ),
                metadata={"id": incident.id, "proposal": proposal_id, "gate": "allow_apply",
                          "applied": False},
            )
        if args.get("confirm") is not True:
            return ToolResult(
                success=False,
                error="confirm must be true. Nothing runs on an implied yes.",
                metadata={"id": incident.id, "proposal": proposal_id, "gate": "confirm",
                          "applied": False},
            )
        if str(args.get("confirm_token") or "") != str(proposal.get("confirm_token") or ""):
            logger.warning("apply refused for %s/%s: confirm token mismatch", incident.id,
                           proposal_id)
            return ToolResult(
                success=False,
                error=(
                    "The confirm token does not match this proposal. The token digests the "
                    "plan, so a plan that changed needs a fresh confirm — re-read it with "
                    "ops_incident and confirm the token it shows."
                ),
                metadata={"id": incident.id, "proposal": proposal_id, "gate": "token",
                          "applied": False},
            )
        if proposal.get("applied"):
            return ToolResult(
                success=False,
                error=f"{proposal_id} was already applied at {proposal.get('applied_at')}. "
                      "Propose a new fix rather than re-running this one.",
                metadata={"id": incident.id, "proposal": proposal_id, "gate": "already-applied",
                          "applied": True},
            )
        action_name = str(proposal.get("action") or "")
        if not action_name:
            return ToolResult(
                success=False,
                error=(
                    f"{proposal_id} names no runbook action, so there is nothing for this "
                    "app to run — it is a plan for a human. Carry it out, then close the "
                    "incident with ops_resolve."
                ),
                metadata={"id": incident.id, "proposal": proposal_id, "gate": "no-action",
                          "applied": False},
            )
        book = await asyncio.to_thread(self._runbooks.get, incident.runbook)
        declared = book.action(action_name)
        if list(declared.argv) != list(proposal.get("argv") or []):
            logger.warning("apply refused for %s/%s: runbook argv changed since the proposal",
                           incident.id, proposal_id)
            return ToolResult(
                success=False,
                error=(
                    f"Runbook `{book.name}` no longer declares `{action_name}` with the argv "
                    "this proposal recorded. Re-propose against the runbook as it is now, so "
                    "the confirm covers what would actually run."
                ),
                metadata={"id": incident.id, "proposal": proposal_id, "gate": "argv-drift",
                          "declared_argv": list(declared.argv),
                          "proposed_argv": list(proposal.get("argv") or []),
                          "applied": False},
            )

        outcome = await asyncio.to_thread(run_action, declared, timeout=self._timeout)
        incident = await asyncio.to_thread(
            self._ledger.mark_applied, incident.id, proposal_id, outcome
        )
        # Refs and the verdict are logged; the command's output is not — it can quote
        # anything the failing system had in it.
        logger.info("incident %s applied %s (action=%s, exit=%s, timed_out=%s)", incident.id,
                    proposal_id, action_name, outcome["exit_code"], outcome["timed_out"])
        stream = fence_untrusted(
            f"$ {' '.join(outcome['argv'])}\n\n"
            f"--- stdout ---\n{outcome['stdout'] or '(empty)'}\n"
            f"--- stderr ---\n{outcome['stderr'] or '(empty)'}",
            source=f"output of runbook action {action_name}",
            source_type="ops_action_output", source_id=f"{incident.id}/{proposal_id}",
        )
        verdict = (
            "timed out" if outcome["timed_out"]
            else ("exited 0" if outcome["exit_code"] == 0
                  else f"exited {outcome['exit_code']}")
        )
        return ToolResult(
            success=True,
            output=(
                f"Applied `{proposal_id}` on `{incident.id}` — action `{action_name}` "
                f"{verdict}.\n\n{stream}\n\nConfirm the symptom is gone, then close it with "
                "ops_resolve. If it is worse, the rollback you proposed is: "
                f"{proposal.get('rollback')}"
            ),
            metadata={"id": incident.id, "state": incident.state, "proposal": proposal_id,
                      "action": action_name, "exit_code": outcome["exit_code"],
                      "timed_out": outcome["timed_out"], "applied": True},
        )

    async def _resolve(self, args: dict[str, Any]) -> ToolResult:
        state = str(args.get("state") or "").strip().lower()
        incident = await asyncio.to_thread(
            self._ledger.close, str(args.get("incident") or ""),
            state=state, note=str(args.get("note") or ""),
        )
        logger.info("incident %s closed as %s", incident.id, incident.state)
        return ToolResult(
            success=True,
            output=f"`{incident.id}` is {incident.state}. It keeps its timeline and will "
                   "reopen by itself if the alarm fires again.",
            metadata={"id": incident.id, "state": incident.state},
        )


def create_provider(config: dict[str, Any] | None = None) -> OpsProvider:
    """Manifest factory — core calls this with this app's saved settings."""
    return OpsProvider(config)
