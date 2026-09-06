"""The incident ledger: alarm intake, identifier grammars, the timeline, the priority
score, and the fix proposals with their content-bound confirm tokens.

Everything here is synchronous and takes a root path, so the whole ledger is testable
against a temp dir with no provider, no gateway and no settings. The provider wraps each
call in ``asyncio.to_thread`` — a spool sweep reads files and hashes them, and the
gateway's event loop must not wait on it.

Three invariants this module exists to hold:

1. **An alarm payload is untrusted from the moment it is read.** It arrives from a
   monitor whose fields carry log lines, hostnames and URLs that nobody in this process
   authored. Nothing out of a payload is ever interpolated into a path or an argv
   element: the incident id is derived (a hash of the alarm's identity, matched by a
   fixed grammar), and the one payload field that *names* something on disk — the
   ``runbook`` hint — is checked against a strict per-segment grammar and must resolve
   to a runbook the operator already wrote, so it can only ever SELECT among their own
   files.
2. **This module cannot change anything outside the ledger.** It imports no
   ``subprocess`` and spawns nothing. Applying a remediation lives in ``runbooks.py``
   behind the provider's one gate, which is what makes "no ungated mutation path"
   checkable rather than asserted.
3. **A confirm token is a digest of the proposal it confirms.** Rewriting a proposal
   changes its token, so a human who read one plan cannot have a different one applied
   under the approval they gave.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from personalclaw.sdk.util import app_data_dir, atomic_write

APP_NAME = "ops"

# An incident id is DERIVED, never taken from a payload: `inc-` plus 12 hex characters of
# the alarm's identity digest. The grammar is what makes `..`, `.git`, dotfiles and
# `-oProxyCommand=…` unrepresentable rather than filtered, and it is re-checked on the way
# back in so a caller-supplied id is held to the same shape as a generated one.
INCIDENT_ID = re.compile(r"^inc-[0-9a-f]{12}$")
PROPOSAL_ID = re.compile(r"^prop-[0-9a-f]{8}$")

# A runbook is addressed by its filename stem: one segment, starting alphanumeric. Same
# reason as the incident id — this string becomes a path.
RUNBOOK_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9])?$")

#: Severity vocabulary, worst first. An unrecognised severity becomes ``unknown`` and is
#: weighted mid-scale rather than dropped: a monitor emitting a word we do not know is
#: not evidence that the alarm is unimportant.
SEVERITIES = ("critical", "high", "medium", "low", "info", "unknown")

_SEVERITY_ALIASES = {
    "critical": "critical", "crit": "critical", "fatal": "critical", "sev1": "critical",
    "p1": "critical", "emergency": "critical", "page": "critical",
    "high": "high", "error": "high", "major": "high", "sev2": "high", "p2": "high",
    "medium": "medium", "moderate": "medium", "warn": "medium", "warning": "medium",
    "sev3": "medium", "p3": "medium",
    "low": "low", "minor": "low", "sev4": "low", "p4": "low",
    "info": "info", "informational": "info", "ok": "info", "resolved": "info",
    "none": "info", "debug": "info",
}

# Incident lifecycle. `new → claimed → investigating → proposed` is the on-call chain the
# tools enforce; the three terminal states are what takes an incident out of the queue.
OPEN_STATES = ("new", "claimed", "investigating", "proposed", "applied")
TERMINAL_STATES = ("resolved", "dismissed")
STATES = OPEN_STATES + TERMINAL_STATES

# Intake caps. A spool is written by a monitor, not by a person, so every dimension of it
# is bounded: a runaway exporter degrades the sweep instead of stalling a tool call.
MAX_SPOOL_FILES = 400
MAX_SPOOL_FILE_BYTES = 262_144
MAX_ALERTS_PER_FILE = 50
MAX_TEXT_CHARS = 8_000
MAX_FIELD_CHARS = 400
MAX_TIMELINE_ENTRIES = 200
MAX_PROPOSALS = 20
MAX_SEEN_FILES = 5_000

# Priority weights. Every term is reachable and every term can be zero — the score is
# returned WITH this breakdown so a term that stopped firing is visible in the output
# rather than silently absent. See `test_provider.py`, which pins each term in both
# directions.
SEVERITY_WEIGHT = {
    "critical": 40.0, "high": 25.0, "medium": 12.0, "low": 5.0, "info": 0.0,
    "unknown": 8.0,
}
PAGE_BONUS = 20.0
UNCLAIMED_BONUS = 10.0
AGE_POINTS_PER_MINUTE = 0.2
AGE_CAP = 20.0
REPEAT_POINTS = 2.0
REPEAT_CAP = 10.0
NO_RUNBOOK_BONUS = 6.0


class IncidentRefError(ValueError):
    """An identifier that must never become a path element."""


class IncidentMissing(LookupError):
    """No such incident (or proposal) in the ledger."""


class LedgerError(RuntimeError):
    """The ledger refused the transition."""


def utc_now() -> str:
    """Second-resolution UTC. Used for every recorded time so the ledger diffs cleanly."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_incident_id(raw: str) -> str:
    """Validate an incident id, or refuse it. See ``INCIDENT_ID`` for why."""
    ref = (raw or "").strip()
    if not INCIDENT_ID.match(ref):
        raise IncidentRefError(
            f"incident id {raw!r} is not allowed — an id looks like "
            "'inc-0a1b2c3d4e5f'. Call ops_queue to see the open ones."
        )
    return ref


def parse_proposal_id(raw: str) -> str:
    """Validate a proposal id, or refuse it."""
    ref = (raw or "").strip()
    if not PROPOSAL_ID.match(ref):
        raise IncidentRefError(
            f"proposal id {raw!r} is not allowed — an id looks like 'prop-0a1b2c3d'. "
            "Call ops_incident to see this incident's proposals."
        )
    return ref


def parse_runbook_name(raw: str) -> str:
    """Validate a runbook name into one path segment, or refuse it."""
    name = (raw or "").strip()
    if not name:
        raise IncidentRefError("a runbook name is required")
    if len(name) > 64:
        raise IncidentRefError("a runbook name is at most 64 characters")
    if not RUNBOOK_NAME.match(name):
        raise IncidentRefError(
            f"runbook name {name!r} is not allowed — one segment, starting with a letter "
            "or digit, using only letters, digits, '.', '_' and '-'"
        )
    return name


def normalise_severity(raw: Any) -> str:
    """Map a monitor's severity word onto this app's vocabulary."""
    word = str(raw or "").strip().lower()
    if not word:
        return "unknown"
    return _SEVERITY_ALIASES.get(word, "unknown")


def _clip(value: Any, limit: int = MAX_FIELD_CHARS) -> str:
    """One printable line, capped. Control characters would forge structure in a log."""
    text = str(value or "")
    text = "".join(ch if ch.isprintable() else " " for ch in text).strip()
    return text[:limit]


def _clip_text(value: Any) -> str:
    """Multi-line untrusted prose, capped. Newlines survive; other controls do not."""
    text = str(value or "")
    text = "".join(ch if (ch.isprintable() or ch == "\n") else " " for ch in text)
    text = text.replace("\r", "\n").strip()
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + "\n… (truncated at the intake cap)"
    return text


def _first(payload: dict[str, Any], *keys: str) -> Any:
    """First present, non-empty value among *keys*, including one level of nesting.

    Monitors disagree about where the same fact lives — Alertmanager puts the alarm name
    in ``labels.alertname`` and the prose in ``annotations.summary``, CloudWatch-shaped
    payloads put them at the top level. Reading both shapes here is what keeps the rest
    of the ledger free of per-vendor branches.
    """
    nests = [payload]
    for nested_key in ("labels", "annotations", "detail", "alarm"):
        nested = payload.get(nested_key)
        if isinstance(nested, dict):
            nests.append(nested)
    for key in keys:
        for source in nests:
            value = source.get(key)
            if isinstance(value, (str, int, float, bool)) and str(value).strip():
                return value
    return None


def alarm_fingerprint(source: str, name: str, resource: str, severity: str) -> str:
    """The identity of an alarm across firings — deliberately NOT its message or time.

    Two firings of the same alarm on the same resource are one incident that fired twice,
    which is what makes the repeat term of the priority score mean something. Folding the
    message in would mint a new incident every time a value in it changed.
    """
    material = "\x00".join((source, name.lower(), resource.lower(), severity))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass
class Alarm:
    """One normalised alarm firing. Every text field is untrusted."""

    name: str
    severity: str
    resource: str = ""
    summary: str = ""
    page: bool = False
    source: str = "spool"
    runbook_hint: str = ""
    at: str = ""

    @property
    def fingerprint(self) -> str:
        return alarm_fingerprint(self.source, self.name, self.resource, self.severity)

    @property
    def incident_id(self) -> str:
        return f"inc-{self.fingerprint[:12]}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "severity": self.severity, "resource": self.resource,
            "summary": self.summary, "page": self.page, "source": self.source,
            "runbook_hint": self.runbook_hint, "at": self.at,
        }


def parse_alarm(payload: Any, *, source: str = "spool", fallback_at: str = "") -> Alarm | None:
    """Normalise one monitor payload into an :class:`Alarm`, or None if it is not one.

    A payload with no recognisable alarm name is refused rather than filed under a
    placeholder: an incident nobody can name is noise in the queue, and counting it out
    loud (the sweep's ``unreadable``/``skipped`` totals) is more useful than filing it.
    """
    if not isinstance(payload, dict):
        return None
    name = _clip(_first(payload, "alertname", "alarm_name", "alarmName", "alarm", "name",
                        "monitor", "check"))
    if not name:
        return None
    severity = normalise_severity(
        _first(payload, "severity", "level", "priority", "urgency", "state")
    )
    resource = _clip(_first(payload, "resource", "instance", "host", "hostname", "service",
                            "target", "dimension"))
    summary = _clip_text(
        _first(payload, "summary", "message", "description", "reason", "text", "body")
    )
    page_raw = _first(payload, "page", "paging", "pages", "wake")
    page = str(page_raw).strip().lower() in {"1", "true", "yes", "on"} if page_raw is not None \
        else severity == "critical"
    hint_raw = _clip(_first(payload, "runbook", "runbook_name", "playbook"), 64)
    try:
        hint = parse_runbook_name(hint_raw) if hint_raw else ""
    except IncidentRefError:
        # A payload naming something that cannot be a filename is not an error worth
        # failing the sweep over — the alarm is still real. The hint is dropped and the
        # runbook matcher picks by rule instead.
        hint = ""
    at = _clip(_first(payload, "at", "startsAt", "timestamp", "time", "fired_at"), 64) \
        or fallback_at or utc_now()
    return Alarm(name=name, severity=severity, resource=resource, summary=summary,
                 page=page, source=source, runbook_hint=hint, at=at)


def alarms_in_document(document: Any, *, source: str, fallback_at: str) -> list[Alarm]:
    """Every alarm in one spool document — a bare object, a list, or ``{"alerts": [...]}``."""
    if isinstance(document, dict):
        for key in ("alerts", "alarms", "records", "events"):
            nested = document.get(key)
            if isinstance(nested, list):
                items: list[Any] = nested[:MAX_ALERTS_PER_FILE]
                break
        else:
            items = [document]
    elif isinstance(document, list):
        items = document[:MAX_ALERTS_PER_FILE]
    else:
        return []
    out: list[Alarm] = []
    for item in items:
        alarm = parse_alarm(item, source=source, fallback_at=fallback_at)
        if alarm is not None:
            out.append(alarm)
    return out


@dataclass
class Incident:
    """One incident record — the on-disk unit of the ledger."""

    id: str
    alarm: Alarm
    state: str = "new"
    first_seen: str = ""
    last_seen: str = ""
    occurrences: int = 1
    owner: str = ""
    claimed_at: str = ""
    runbook: str = ""
    timeline: list[dict[str, Any]] = field(default_factory=list)
    proposals: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "alarm": self.alarm.to_dict(), "state": self.state,
            "first_seen": self.first_seen, "last_seen": self.last_seen,
            "occurrences": self.occurrences, "owner": self.owner,
            "claimed_at": self.claimed_at, "runbook": self.runbook,
            "timeline": self.timeline, "proposals": self.proposals,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Incident":
        alarm_data = data.get("alarm") or {}
        alarm = Alarm(
            name=str(alarm_data.get("name") or ""),
            severity=normalise_severity(alarm_data.get("severity")),
            resource=str(alarm_data.get("resource") or ""),
            summary=str(alarm_data.get("summary") or ""),
            page=bool(alarm_data.get("page")),
            source=str(alarm_data.get("source") or "spool"),
            runbook_hint=str(alarm_data.get("runbook_hint") or ""),
            at=str(alarm_data.get("at") or ""),
        )
        state = str(data.get("state") or "new")
        return cls(
            id=parse_incident_id(str(data.get("id") or "")),
            alarm=alarm,
            state=state if state in STATES else "new",
            first_seen=str(data.get("first_seen") or ""),
            last_seen=str(data.get("last_seen") or ""),
            occurrences=max(1, int(data.get("occurrences") or 1)),
            owner=str(data.get("owner") or ""),
            claimed_at=str(data.get("claimed_at") or ""),
            runbook=str(data.get("runbook") or ""),
            timeline=[e for e in (data.get("timeline") or []) if isinstance(e, dict)],
            proposals=[p for p in (data.get("proposals") or []) if isinstance(p, dict)],
        )

    @property
    def open(self) -> bool:
        return self.state in OPEN_STATES

    def note(self, kind: str, detail: str, **extra: Any) -> dict[str, Any]:
        """Append one timeline entry and return it."""
        entry = {"at": utc_now(), "kind": kind, "detail": _clip_text(detail), **extra}
        self.timeline.append(entry)
        if len(self.timeline) > MAX_TIMELINE_ENTRIES:
            del self.timeline[: len(self.timeline) - MAX_TIMELINE_ENTRIES]
        return entry

    def proposal(self, proposal_id: str) -> dict[str, Any]:
        for candidate in self.proposals:
            if candidate.get("id") == proposal_id:
                return candidate
        raise IncidentMissing(
            f"{self.id} has no proposal {proposal_id!r}"
        )


def age_minutes(first_seen: str, *, now: datetime | None = None) -> float:
    """Minutes since *first_seen*. An unparseable stamp ages nothing rather than guessing."""
    try:
        seen = datetime.fromisoformat(first_seen)
    except (TypeError, ValueError):
        return 0.0
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    return max(0.0, (reference - seen).total_seconds() / 60.0)


def priority(incident: Incident, *, now: datetime | None = None) -> dict[str, Any]:
    """Score an open incident and return the score WITH its per-term breakdown.

    Five independent terms, each of which can fire and each of which can be zero:
    severity, paging, unclaimed, age, and repetition — plus a bonus for an incident no
    runbook matched, because that is the one a human has to think about. Returning the
    breakdown is deliberate: a weight that stops firing shows up as a zero in the output
    instead of quietly leaving the ranking alone.
    """
    terms = {
        "severity": SEVERITY_WEIGHT.get(incident.alarm.severity, SEVERITY_WEIGHT["unknown"]),
        "page": PAGE_BONUS if incident.alarm.page else 0.0,
        "unclaimed": UNCLAIMED_BONUS if not incident.owner else 0.0,
        "age": min(AGE_CAP, AGE_POINTS_PER_MINUTE * age_minutes(incident.first_seen, now=now)),
        "repeats": min(REPEAT_CAP, REPEAT_POINTS * (incident.occurrences - 1)),
        "no_runbook": 0.0 if incident.runbook else NO_RUNBOOK_BONUS,
    }
    terms = {key: round(value, 1) for key, value in terms.items()}
    return {"score": round(sum(terms.values()), 1), "terms": terms}


def proposal_digest(body: dict[str, Any]) -> str:
    """The confirm token: a digest over the proposal's decision-bearing content.

    ``id`` and ``created_at`` are excluded so the token depends on WHAT would be done,
    not on when it was written down. Anything a human would re-read before approving —
    the summary, the action, the exact argv, the blast radius, the rollback — is inside.
    """
    material = json.dumps(
        {
            "incident": body.get("incident", ""),
            "summary": body.get("summary", ""),
            "action": body.get("action") or "",
            "argv": body.get("argv") or [],
            "blast_radius": body.get("blast_radius", ""),
            "rollback": body.get("rollback", ""),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


class Ledger:
    """The incident store: one JSON file per incident under *root*."""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root).expanduser() if root else app_data_dir(APP_NAME)
        self.incidents_dir = self.root / "incidents"
        self.seen_path = self.root / "seen-spool.json"
        self.incidents_dir.mkdir(parents=True, exist_ok=True)

    # ── Records ─────────────────────────────────────────────────────────────────

    def _path(self, incident_id: str) -> Path:
        return self.incidents_dir / f"{parse_incident_id(incident_id)}.json"

    def load(self, incident_id: str) -> Incident:
        path = self._path(incident_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise IncidentMissing(f"no incident {incident_id!r} in the ledger") from exc
        except ValueError as exc:
            raise LedgerError(
                f"the record for {incident_id} will not parse ({exc}); it has to be "
                "repaired or removed by hand"
            ) from exc
        return Incident.from_dict(data)

    def save(self, incident: Incident) -> None:
        if len(incident.proposals) > MAX_PROPOSALS:
            del incident.proposals[: len(incident.proposals) - MAX_PROPOSALS]
        atomic_write(
            self._path(incident.id),
            json.dumps(incident.to_dict(), indent=2, sort_keys=True) + "\n",
        )

    def all_incidents(self) -> tuple[list[Incident], int]:
        """Every parseable incident, plus a count of the records that would not parse.

        An unreadable record is SKIPPED so the rest of the queue still works, and counted
        so ``doctor`` and the queue tool can say so out loud — a silently shorter queue
        during an incident is the worst possible failure for this app.
        """
        incidents: list[Incident] = []
        unreadable = 0
        try:
            files = sorted(self.incidents_dir.glob("inc-*.json"))
        except OSError:
            return [], 0
        for path in files:
            try:
                incidents.append(Incident.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError, IncidentRefError):
                unreadable += 1
        return incidents, unreadable

    def queue(
        self, *, now: datetime | None = None
    ) -> tuple[list[tuple[Incident, dict[str, Any]]], int]:
        """Open incidents highest-priority-first with their score breakdowns, plus the
        count of records that would not parse — so a caller can say so out loud."""
        incidents, unreadable = self.all_incidents()
        scored = [(inc, priority(inc, now=now)) for inc in incidents if inc.open]
        scored.sort(key=lambda pair: (-pair[1]["score"], pair[0].first_seen, pair[0].id))
        return scored, unreadable

    # ── Intake ──────────────────────────────────────────────────────────────────

    def _seen(self) -> dict[str, str]:
        try:
            data = json.loads(self.seen_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def _write_seen(self, seen: dict[str, str]) -> None:
        if len(seen) > MAX_SEEN_FILES:
            seen = dict(list(seen.items())[-MAX_SEEN_FILES:])
        atomic_write(self.seen_path, json.dumps(seen, indent=2, sort_keys=True) + "\n")

    def sweep(
        self,
        spool: Path,
        *,
        match_runbook=None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Read every unseen spool file and register or update its alarms.

        A spool file is identified by name PLUS the digest of its bytes, so re-sweeping an
        unchanged spool registers nothing while a monitor that rewrites a file in place is
        still noticed. That distinction is what keeps the repeat term of the priority
        score honest: without it every sweep would look like another firing.

        Spool files are never deleted or moved. The monitor owns them; this app only reads.
        """
        report: dict[str, Any] = {
            "spool": str(spool), "files_read": 0, "files_skipped": 0, "unreadable": 0,
            "alarms": 0, "opened": [], "refired": [], "updated": [], "truncated": False,
        }
        try:
            files = sorted(p for p in spool.glob("*.json") if p.is_file())
        except OSError as exc:
            report["error"] = str(exc)
            return report
        if len(files) > MAX_SPOOL_FILES:
            files = files[:MAX_SPOOL_FILES]
            report["truncated"] = True

        seen = self._seen()
        for path in files:
            try:
                raw = path.read_bytes()
            except OSError:
                report["unreadable"] += 1
                continue
            if len(raw) > MAX_SPOOL_FILE_BYTES:
                report["unreadable"] += 1
                continue
            digest = hashlib.sha256(raw).hexdigest()
            if seen.get(path.name) == digest:
                report["files_skipped"] += 1
                continue
            try:
                document = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                report["unreadable"] += 1
                # Recorded as seen anyway: a file that is not JSON will not become JSON,
                # and re-reporting it on every sweep would bury the real intake.
                seen[path.name] = digest
                continue
            fallback = _iso_mtime(path)
            alarms = alarms_in_document(document, source="spool", fallback_at=fallback)
            report["files_read"] += 1
            report["alarms"] += len(alarms)
            for alarm in alarms:
                outcome = self._ingest(alarm, match_runbook=match_runbook, now=now)
                report[outcome[0]].append(outcome[1])
            seen[path.name] = digest
        self._write_seen(seen)
        return report

    def _ingest(self, alarm: Alarm, *, match_runbook=None, now=None) -> tuple[str, str]:
        """File one alarm firing. Returns which bucket of the sweep report it lands in."""
        stamp = utc_now()
        try:
            incident = self.load(alarm.incident_id)
        except IncidentMissing:
            runbook = match_runbook(alarm) if match_runbook else ""
            incident = Incident(
                id=alarm.incident_id, alarm=alarm, first_seen=alarm.at or stamp,
                last_seen=stamp, runbook=runbook or "",
            )
            incident.note(
                "opened",
                f"{alarm.severity} alarm {alarm.name!r}"
                + (f" on {alarm.resource}" if alarm.resource else "")
                + (f"; runbook {runbook}" if runbook else "; no runbook matched"),
            )
            self.save(incident)
            return "opened", incident.id

        incident.occurrences += 1
        incident.last_seen = stamp
        # A newer firing can carry a message the first one did not have; the alarm's
        # IDENTITY cannot change, because the identity is what addressed this record.
        incident.alarm.summary = alarm.summary or incident.alarm.summary
        incident.alarm.page = incident.alarm.page or alarm.page
        if not incident.runbook and match_runbook:
            incident.runbook = match_runbook(alarm) or ""
        if incident.state in TERMINAL_STATES:
            was = incident.state
            incident.state = "new"
            incident.owner = ""
            incident.claimed_at = ""
            incident.note(
                "refired",
                f"fired again after being {was} — firing #{incident.occurrences}. "
                "Reopened unclaimed.",
            )
            self.save(incident)
            return "refired", incident.id
        incident.note("refired", f"fired again — firing #{incident.occurrences}")
        self.save(incident)
        return "updated", incident.id

    # ── Transitions ─────────────────────────────────────────────────────────────

    def claim(self, incident_id: str, owner: str) -> Incident:
        incident = self.load(incident_id)
        if incident.state in TERMINAL_STATES:
            raise LedgerError(
                f"{incident.id} is {incident.state} — nothing to claim. It will reopen by "
                "itself if the alarm fires again."
            )
        who = _clip(owner, 80) or "on-call"
        if incident.owner and incident.owner != who:
            raise LedgerError(
                f"{incident.id} is already claimed by {incident.owner!r}. Release it there "
                "first — two responders on one alarm is how a fix gets applied twice."
            )
        incident.owner = who
        incident.claimed_at = incident.claimed_at or utc_now()
        if incident.state == "new":
            incident.state = "claimed"
        incident.note("claimed", f"claimed by {who}")
        self.save(incident)
        return incident

    def release(self, incident_id: str) -> Incident:
        incident = self.load(incident_id)
        was = incident.owner
        incident.owner = ""
        incident.claimed_at = ""
        if incident.state in ("claimed", "investigating"):
            incident.state = "new"
        incident.note("released", f"released by {was or 'nobody'}")
        self.save(incident)
        return incident

    def require_claimed(self, incident_id: str) -> Incident:
        """Load an incident that must already be claimed.

        The on-call chain is claim → investigate → propose, and it is enforced here rather
        than trusted: an unclaimed incident being worked is how two responders end up
        proposing two different fixes for the same alarm.
        """
        incident = self.load(incident_id)
        if incident.state in TERMINAL_STATES:
            raise LedgerError(f"{incident.id} is {incident.state} — reopen it by claiming "
                              "the next firing.")
        if not incident.owner:
            raise LedgerError(
                f"{incident.id} is not claimed yet — call ops_claim first so the ledger "
                "records who is working it."
            )
        return incident

    def record(self, incident_id: str, finding: str, *, verdict: str = "") -> Incident:
        incident = self.require_claimed(incident_id)
        if not (finding or "").strip():
            raise LedgerError("a finding is required — record what you actually observed")
        incident.note("finding", finding, verdict=_clip(verdict, 120))
        if incident.state == "claimed":
            incident.state = "investigating"
        self.save(incident)
        return incident

    def add_proposal(
        self,
        incident_id: str,
        *,
        summary: str,
        blast_radius: str,
        rollback: str,
        action: str = "",
        argv: list[str] | None = None,
    ) -> tuple[Incident, dict[str, Any]]:
        """Record a PROPOSED fix and mint its content-bound confirm token.

        This writes to the ledger and nothing else. Whether the proposal names an
        executable runbook action or is a plan for a human to carry out, nothing here
        touches the system the alarm is about.
        """
        incident = self.require_claimed(incident_id)
        for label, value in (("summary", summary), ("blast_radius", blast_radius),
                             ("rollback", rollback)):
            if not (value or "").strip():
                raise LedgerError(
                    f"{label} is required — a proposal a human cannot weigh is not a "
                    "proposal. Say what changes, what it touches, and how to undo it."
                )
        body = {
            "incident": incident.id,
            "summary": _clip_text(summary),
            "action": action or None,
            "argv": list(argv or []) or None,
            "blast_radius": _clip_text(blast_radius),
            "rollback": _clip_text(rollback),
        }
        token = proposal_digest(body)
        proposal = {
            "id": f"prop-{token[:8]}",
            "created_at": utc_now(),
            "confirm_token": token,
            "applied": False,
            **body,
        }
        incident.proposals = [p for p in incident.proposals if p.get("id") != proposal["id"]]
        incident.proposals.append(proposal)
        incident.state = "proposed"
        incident.note(
            "proposed",
            f"{proposal['id']}: {body['summary']}",
            action=action or "", gated=True,
        )
        self.save(incident)
        return incident, proposal

    def mark_applied(
        self, incident_id: str, proposal_id: str, outcome: dict[str, Any]
    ) -> Incident:
        """Record that the gate let a proposal through, and what came back."""
        incident = self.load(incident_id)
        proposal = incident.proposal(parse_proposal_id(proposal_id))
        proposal["applied"] = True
        proposal["applied_at"] = utc_now()
        proposal["exit_code"] = outcome.get("exit_code")
        incident.state = "applied"
        incident.note(
            "applied",
            f"{proposal['id']} applied by {incident.owner or 'on-call'} — exit "
            f"{outcome.get('exit_code')}",
            action=proposal.get("action") or "",
        )
        self.save(incident)
        return incident

    def close(self, incident_id: str, *, state: str, note: str) -> Incident:
        if state not in TERMINAL_STATES:
            raise LedgerError(f"state must be one of {', '.join(TERMINAL_STATES)}")
        incident = self.load(incident_id)
        incident.state = state
        incident.note(state, note or f"marked {state}")
        self.save(incident)
        return incident


def _iso_mtime(path: Path) -> str:
    """A spool file's mtime as a UTC stamp — the fallback when a payload has no time."""
    try:
        return datetime.fromtimestamp(
            os.stat(path).st_mtime, tz=timezone.utc
        ).isoformat(timespec="seconds")
    except OSError:
        return utc_now()


#: Severity ordering for the queue's floor filter. ``unknown`` deliberately sorts ABOVE
#: ``low``: a severity this app could not read is not evidence the alarm is unimportant,
#: and a floor of "medium and worse" that dropped unreadable ones would hide exactly the
#: alarms a new monitor emits before its vocabulary is mapped.
SEVERITY_RANK = {"critical": 5, "high": 4, "unknown": 3, "medium": 3, "low": 2, "info": 1}


def severity_at_least(severity: str, floor: str) -> bool:
    """Whether *severity* is at or above *floor*. An unknown floor filters nothing."""
    if floor not in SEVERITY_RANK:
        return True
    return SEVERITY_RANK.get(severity, SEVERITY_RANK["unknown"]) >= SEVERITY_RANK[floor]


def glob_match(value: str, patterns: list[str]) -> bool:
    """Case-insensitive fnmatch over a list of patterns, with substring as the fallback.

    Runbook authors write either `worker-*` or a bare word they expect to appear in the
    alarm name; supporting both is why this is not a plain `fnmatch`.
    """
    haystack = (value or "").lower()
    for pattern in patterns:
        needle = str(pattern or "").lower().strip()
        if not needle:
            continue
        if any(ch in needle for ch in "*?["):
            if fnmatch.fnmatch(haystack, needle):
                return True
        elif needle in haystack:
            return True
    return False
