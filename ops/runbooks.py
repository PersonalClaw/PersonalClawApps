"""Runbooks: the operator's own investigation checklists and declared remediations —
plus the ONE place in this bundle that can spawn a process.

A runbook is a JSON file the operator writes, addressed by its filename stem. It carries
the checks a first responder should walk and, optionally, named actions with a **literal**
argv. Two properties make that safe to hand a language model:

1. **An action's argv is authored, never assembled.** There is no substitution, no format
   string and no interpolation anywhere in this module: the argv that reaches
   ``subprocess.run`` is the exact list the operator committed to the runbook file. An
   alarm payload full of shell metacharacters therefore cannot change one byte of it —
   the alarm's only influence is *which runbook matches*, and a match can only ever select
   among files the operator already wrote.
2. **Running one is gated four ways.** ``run_action`` is unreachable unless the operator
   turned the ``allow_apply`` setting on, the caller passed ``confirm=true``, the caller
   echoed the confirm token that digests the proposal, and the proposal names an action
   that is still declared in the matched runbook with the same argv. The provider owns
   those gates; this module owns the fixed argv, the timeout and the closed stdin.

``incidents.py`` imports no ``subprocess`` at all, which is what makes "no ungated
mutation path exists" a property of the file layout rather than a promise in a docstring.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from incidents import (
    Alarm,
    IncidentRefError,
    glob_match,
    normalise_severity,
    parse_runbook_name,
)

logger = logging.getLogger("ops.runbooks")

# An action is addressed by name inside its runbook, and the name is echoed into logs and
# proposals — so it gets a grammar too, narrower than a runbook's.
ACTION_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,46}[a-z0-9])?$")

MAX_ARGV = 24
MAX_ARGV_ELEMENT = 400
MAX_ACTIONS = 12
MAX_CHECKS = 30
MAX_CHECK_CHARS = 400
MAX_RUNBOOK_BYTES = 131_072
MAX_RUNBOOKS = 200
MAX_OUTPUT_CHARS = 4_000

# A bare program name must look like a program name. An absolute path is allowed (the
# operator may point at /usr/local/bin/deploy); a RELATIVE path is not, because what it
# resolves to depends on a working directory nobody in this flow chose.
PROGRAM_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._+-]{0,62}[A-Za-z0-9])?$")


class RunbookError(ValueError):
    """A runbook file this app refuses to load, or an action it refuses to run."""


@dataclass(frozen=True)
class Action:
    """One declared remediation: a name, a literal argv, and the human's three answers."""

    name: str
    argv: tuple[str, ...]
    description: str = ""
    blast_radius: str = ""
    rollback: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "argv": list(self.argv), "description": self.description,
            "blast_radius": self.blast_radius, "rollback": self.rollback,
        }


@dataclass
class Runbook:
    """One loaded runbook. ``name`` is the filename stem — the addressable identity."""

    name: str
    title: str = ""
    match: dict[str, list[str]] = field(default_factory=dict)
    checks: tuple[str, ...] = ()
    actions: tuple[Action, ...] = ()

    def action(self, name: str) -> Action:
        for candidate in self.actions:
            if candidate.name == name:
                return candidate
        raise RunbookError(
            f"runbook {self.name!r} declares no action {name!r}. It declares: "
            f"{', '.join(a.name for a in self.actions) or '(none)'}."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "title": self.title, "match": self.match,
            "checks": list(self.checks), "actions": [a.to_dict() for a in self.actions],
        }


def parse_argv(raw: Any) -> tuple[str, ...]:
    """Validate a declared argv, or refuse the action.

    Refusals are all structural: a shell string instead of a list, an empty list, a
    relative program path, a control character in an element. There is deliberately no
    denylist of dangerous-looking flags — the operator authored this command on purpose,
    and a denylist would only imply a safety this app does not provide. What the app DOES
    guarantee is that the list is used verbatim and that nothing untrusted can join it.
    """
    if isinstance(raw, str):
        raise RunbookError(
            "an action's 'argv' must be a LIST of arguments, not a command string — this "
            "app never hands a string to a shell"
        )
    if not isinstance(raw, list) or not raw:
        raise RunbookError("an action's 'argv' must be a non-empty list of strings")
    if len(raw) > MAX_ARGV:
        raise RunbookError(f"an action's 'argv' is at most {MAX_ARGV} elements")
    argv: list[str] = []
    for element in raw:
        if not isinstance(element, str):
            raise RunbookError(f"argv element {element!r} is not a string")
        if len(element) > MAX_ARGV_ELEMENT:
            raise RunbookError(f"argv element is longer than {MAX_ARGV_ELEMENT} characters")
        if any(not ch.isprintable() for ch in element):
            raise RunbookError(
                f"argv element {element!r} contains a control character — a newline or NUL "
                "in an argument forges structure in every log this command is read through"
            )
        argv.append(element)
    program = argv[0]
    if program.startswith("/"):
        parts = Path(program).parts[1:]
        if any(seg in ("..", ".") or seg.startswith(".") for seg in parts):
            raise RunbookError(
                f"program path {program!r} is not allowed — no '..', '.' or dot-segment may "
                "appear in it"
            )
    elif not PROGRAM_NAME.match(program):
        raise RunbookError(
            f"program {program!r} is not allowed — use a bare program name found on PATH "
            "(e.g. 'systemctl') or an absolute path. A relative path resolves against a "
            "working directory nobody in this flow chose."
        )
    return tuple(argv)


def parse_action(raw: Any) -> Action:
    """Validate one declared action."""
    if not isinstance(raw, dict):
        raise RunbookError("each entry of 'actions' must be an object")
    name = str(raw.get("name") or "").strip().lower()
    if not ACTION_NAME.match(name):
        raise RunbookError(
            f"action name {raw.get('name')!r} is not allowed — lowercase letters, digits, "
            "'.', '_' and '-', starting and ending alphanumeric"
        )
    return Action(
        name=name,
        argv=parse_argv(raw.get("argv")),
        description=str(raw.get("description") or "")[:MAX_CHECK_CHARS],
        blast_radius=str(raw.get("blast_radius") or "")[:MAX_CHECK_CHARS],
        rollback=str(raw.get("rollback") or "")[:MAX_CHECK_CHARS],
    )


def parse_runbook(name: str, raw: Any) -> Runbook:
    """Validate one runbook document. *name* is the filename stem, already validated."""
    if not isinstance(raw, dict):
        raise RunbookError("a runbook file must contain a JSON object")
    match_raw = raw.get("match") if isinstance(raw.get("match"), dict) else {}
    match: dict[str, list[str]] = {}
    for key in ("alarm", "resource", "severity"):
        value = match_raw.get(key)
        if isinstance(value, str):
            value = [value]
        if isinstance(value, list):
            patterns = [str(v)[:MAX_CHECK_CHARS] for v in value if str(v or "").strip()]
            if key == "severity":
                # Normalised through the same alias table the alarms are, so a runbook
                # written against its monitor's vocabulary ("warning", "sev2") still
                # matches the severity this app filed the alarm under.
                patterns = sorted({normalise_severity(p) for p in patterns})
            if patterns:
                match[key] = patterns[:20]
    checks = [
        str(c)[:MAX_CHECK_CHARS]
        for c in (raw.get("checks") or [])
        if isinstance(c, (str, int, float)) and str(c).strip()
    ][:MAX_CHECKS]
    actions_raw = raw.get("actions") or []
    if not isinstance(actions_raw, list):
        raise RunbookError("'actions' must be a list")
    if len(actions_raw) > MAX_ACTIONS:
        raise RunbookError(f"a runbook declares at most {MAX_ACTIONS} actions")
    actions = [parse_action(a) for a in actions_raw]
    names = [a.name for a in actions]
    if len(set(names)) != len(names):
        raise RunbookError("two actions in one runbook share a name")
    return Runbook(
        name=name,
        title=str(raw.get("title") or name)[:MAX_CHECK_CHARS],
        match=match,
        checks=tuple(checks),
        actions=tuple(actions),
    )


class RunbookLibrary:
    """The runbooks on disk, loaded on demand from one directory."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser()

    def load_all(self) -> tuple[list[Runbook], list[str]]:
        """Every loadable runbook, plus a legible reason for each one refused.

        A refused runbook does not fail the sweep — the other runbooks still match and the
        incident is still filed. The reasons are surfaced by ``ops_runbooks`` and by
        ``doctor``, because a runbook silently missing during an incident is worse than a
        runbook that never existed.
        """
        books: list[Runbook] = []
        problems: list[str] = []
        try:
            files = sorted(p for p in self.root.glob("*.json") if p.is_file())
        except OSError as exc:
            return [], [f"{self.root}: {exc}"]
        if len(files) > MAX_RUNBOOKS:
            problems.append(
                f"{len(files)} runbook files present; only the first {MAX_RUNBOOKS} are read"
            )
            files = files[:MAX_RUNBOOKS]
        for path in files:
            try:
                name = parse_runbook_name(path.stem)
            except IncidentRefError as exc:
                problems.append(f"{path.name}: {exc}")
                continue
            try:
                raw = path.read_bytes()
            except OSError as exc:
                problems.append(f"{path.name}: {exc}")
                continue
            if len(raw) > MAX_RUNBOOK_BYTES:
                problems.append(f"{path.name}: larger than {MAX_RUNBOOK_BYTES} bytes")
                continue
            try:
                books.append(parse_runbook(name, json.loads(raw.decode("utf-8", "replace"))))
            except (ValueError, RunbookError) as exc:
                problems.append(f"{path.name}: {exc}")
        return books, problems

    def get(self, name: str) -> Runbook:
        """One runbook by name. The name is validated before it becomes a path."""
        wanted = parse_runbook_name(name)
        for book in self.load_all()[0]:
            if book.name == wanted:
                return book
        raise RunbookError(f"no runbook named {wanted!r} in {self.root}")

    def match(self, alarm: Alarm) -> str:
        """The name of the runbook that best fits *alarm*, or "" if none does.

        Specificity wins: the runbook matching the most of the three criteria is chosen,
        ties broken by name so the choice is deterministic. A runbook with an EMPTY match
        block is a catch-all and scores zero, so it is only ever chosen when nothing more
        specific fits — which is what makes a default runbook useful instead of dominant.
        """
        books, _ = self.load_all()
        hint = alarm.runbook_hint
        if hint:
            # A payload may NAME a runbook. It is honoured only if the operator already
            # wrote one by that name — the hint selects, it never creates or escapes.
            for book in books:
                if book.name == hint:
                    return book.name
        # `load_all` returns books in filename order, so a strict `>` already breaks ties
        # by name: the alphabetically first runbook at the winning specificity keeps the
        # slot. An explicit tie-break branch here could never fire, so there isn't one.
        best: tuple[int, str] = (-1, "")
        for book in books:
            score = match_score(book, alarm)
            if score is not None and score > best[0]:
                best = (score, book.name)
        return best[1]


def match_score(book: Runbook, alarm: Alarm) -> int | None:
    """How specifically *book* matches *alarm*, or None if a stated criterion fails.

    Every criterion the runbook STATES must hold; each one that holds is a point. A
    runbook stating nothing matches everything at zero points.
    """
    score = 0
    patterns = book.match.get("alarm")
    if patterns:
        if not glob_match(alarm.name, patterns):
            return None
        score += 1
    patterns = book.match.get("resource")
    if patterns:
        if not glob_match(alarm.resource, patterns):
            return None
        score += 1
    wanted = book.match.get("severity")
    if wanted:
        if alarm.severity not in {str(w).strip().lower() for w in wanted}:
            return None
        score += 1
    return score


def run_action(action: Action, *, timeout: int) -> dict[str, Any]:
    """Run one declared remediation. The only process this bundle ever spawns.

    Reached only through the provider's gate. ``shell=False`` is structural (an argv list
    is passed, never a string), stdin is closed so a remediation can never block waiting
    for a confirmation nobody is there to type, and the output is capped because it comes
    back as untrusted text.
    """
    if not action.argv:
        raise RunbookError("the action declares no argv")
    program = action.argv[0]
    if not program.startswith("/") and shutil.which(program) is None:
        raise RunbookError(
            f"{program!r} is not on PATH — the action cannot run. Fix the runbook or install it."
        )
    logger.info("applying action %s: argv[0]=%s (%d args)", action.name, program,
                len(action.argv) - 1)
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv from the operator's runbook, no shell
            list(action.argv),
            capture_output=True,
            text=True,
            timeout=max(5, int(timeout)),
            check=False,
            stdin=subprocess.DEVNULL,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("action %s timed out after %ss", action.name, timeout)
        return {
            "action": action.name, "argv": list(action.argv), "exit_code": None,
            "timed_out": True, "stdout": "", "stderr": "",
        }
    except OSError as exc:
        raise RunbookError(f"the action could not be started: {exc}") from exc
    logger.info("action %s finished with exit %s", action.name, proc.returncode)
    return {
        "action": action.name,
        "argv": list(action.argv),
        "exit_code": proc.returncode,
        "timed_out": False,
        "stdout": (proc.stdout or "")[:MAX_OUTPUT_CHARS],
        "stderr": (proc.stderr or "")[:MAX_OUTPUT_CHARS],
    }


#: The first-responder checks every incident gets, runbook or not. Deliberately generic:
#: this app does not know the operator's stack, and a checklist that pretended to would
#: send a responder looking for services that do not exist.
GENERIC_CHECKS = (
    "Confirm the alarm is real: is the symptom visible outside the monitor that fired?",
    "Establish the blast radius — one resource, one tier, or everything?",
    "Find the change: what deployed, rotated, expired or was reconfigured just before "
    "first_seen?",
    "Check the obvious exhaustions: disk, memory, file handles, connection pool, quota.",
    "Check the dependencies this resource calls, and the ones that call it.",
    "Decide whether the next step is mitigation (restore service) or diagnosis (find the "
    "cause) — and say which you chose.",
)
