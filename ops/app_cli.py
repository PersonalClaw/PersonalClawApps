"""CLI seams for ops: a setup step and a doctor probe.

``personalclaw setup`` calls :func:`setup` after the core steps; ``personalclaw doctor``
calls :func:`doctor` and renders the lines it returns as this app's section.

There is no credential to collect. What doctor exists to say out loud is the state of the
three things that decide whether this app is any use during an incident: does the spool
folder the monitor writes into exist, do the runbooks load, and is the remediation gate
open or shut. The third one is reported on every run, whichever way it is set: an operator
should never have to guess whether their on-call assistant can restart a service.

This module reads the folders itself rather than importing ``provider``: core loads a CLI
hook by file path, not as a package, so a sibling import is not guaranteed to resolve when
``personalclaw doctor`` runs before anything has loaded the provider.
"""

from __future__ import annotations

import json
from pathlib import Path

from personalclaw.sdk.cli import DoctorLine, SetupContext
from personalclaw.sdk.settings import ProviderSettings
from personalclaw.sdk.util import app_data_dir

APP_NAME = "ops"
LABEL = "Ops"


def _dirs(settings: dict) -> tuple[Path, Path, Path]:
    """The ledger root, the spool folder and the runbook folder, per saved settings."""
    root = app_data_dir(APP_NAME)
    spool = str(settings.get("spool_dir") or "").strip()
    books = str(settings.get("runbooks_dir") or "").strip()
    return (
        root,
        Path(spool).expanduser() if spool else root / "spool",
        Path(books).expanduser() if books else root / "runbooks",
    )


def setup(ctx: SetupContext) -> None:
    """No credential to collect — just say where alarms come in and what can run."""
    saved = ctx.settings.load(ctx.app_name) or {}
    _, spool, books = _dirs(saved)
    ctx.print(f"Ops: alarms are read from {spool} — point your monitor's webhook, SNS "
              "bridge or exporter at a small script that writes one JSON file per firing "
              "into it.")
    ctx.print(f"Ops: runbooks are read from {books} — one JSON file per runbook, its "
              "filename being its name.")
    if saved.get("allow_apply"):
        ctx.print("Ops: 'Allow gated remediation' is ON. A proposed runbook action can be "
                  "run after an explicit confirm. Turn it off in Settings to make this app "
                  "propose-only.")
    else:
        ctx.print("Ops: 'Allow gated remediation' is off, so this app can only ever "
                  "propose. Nothing it suggests can run until you switch that on.")


def doctor() -> list[DoctorLine]:
    """Report the spool, the runbooks, the open queue and the remediation gate."""
    settings = _load_settings()
    root, spool, books = _dirs(settings)
    lines: list[DoctorLine] = []

    lines.append(
        DoctorLine(
            label=f"{LABEL} · alarm spool",
            status="ok" if spool.is_dir() else "warn",
            detail=(
                f"{spool} — {_count(spool, '*.json')} file(s) waiting"
                if spool.is_dir() else
                f"{spool} does not exist yet, so ops_watch finds nothing. Create it, or "
                "point `spool_dir` at where your monitor writes."
            ),
        )
    )

    loaded, broken = _scan_runbooks(books)
    lines.append(
        DoctorLine(
            label=f"{LABEL} · runbooks",
            status="warn" if broken else ("ok" if loaded else "info"),
            detail=(
                f"{broken} runbook file(s) will not parse and are skipped; {loaded} load"
                if broken else
                f"{books} — {loaded} runbook(s)" if loaded else
                f"{books} — none yet. Incidents still open and rank; they just get the "
                "generic checklist."
            ),
        )
    )

    open_count, unreadable = _scan_incidents(root / "incidents")
    lines.append(
        DoctorLine(
            label=f"{LABEL} · queue",
            status="warn" if unreadable else "ok",
            detail=(
                f"{unreadable} incident record(s) will not parse and are not shown in the "
                f"queue; {open_count} open"
                if unreadable else f"{open_count} open incident(s)"
            ),
        )
    )

    allowed = bool(settings.get("allow_apply"))
    lines.append(
        DoctorLine(
            label=f"{LABEL} · remediation gate",
            status="warn" if allowed else "ok",
            detail=(
                "'Allow gated remediation' is ON — a proposed runbook action can run after "
                "an explicit confirm"
                if allowed else
                "propose-only: nothing this app suggests can run until 'Allow gated "
                "remediation' is switched on"
            ),
        )
    )
    return lines


def _load_settings() -> dict:
    """This app's saved settings.

    ``doctor()`` takes no arguments, so there is no ``SetupContext`` to read them through —
    and the gate's state is exactly the fact this report must not get wrong. Read through
    the SDK accessor rather than a guessed path, so it stays the same file the Settings UI
    writes.
    """
    try:
        data = ProviderSettings.load(APP_NAME)
    except OSError:
        return {}
    return data if isinstance(data, dict) else {}


def _count(folder: Path, pattern: str) -> int:
    try:
        return sum(1 for p in folder.glob(pattern) if p.is_file())
    except OSError:
        return 0


def _scan_runbooks(folder: Path) -> tuple[int, int]:
    """(loadable, unparseable) — a shallow JSON check, not the provider's full validation."""
    loaded = broken = 0
    try:
        files = sorted(p for p in folder.glob("*.json") if p.is_file())
    except OSError:
        return 0, 0
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            broken += 1
            continue
        if isinstance(data, dict):
            loaded += 1
        else:
            broken += 1
    return loaded, broken


def _scan_incidents(folder: Path) -> tuple[int, int]:
    """(open, unreadable) over the incident records."""
    open_count = unreadable = 0
    terminal = {"resolved", "dismissed"}
    try:
        files = sorted(p for p in folder.glob("inc-*.json") if p.is_file())
    except OSError:
        return 0, 0
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            unreadable += 1
            continue
        if not isinstance(data, dict):
            unreadable += 1
        elif str(data.get("state") or "new") not in terminal:
            open_count += 1
    return open_count, unreadable
