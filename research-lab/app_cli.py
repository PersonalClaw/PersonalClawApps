"""CLI seams for research-lab: a setup step and a doctor probe.

``personalclaw setup`` calls :func:`setup` after the core steps; ``personalclaw doctor``
calls :func:`doctor` and renders the lines it returns as this app's section.

There is nothing to collect at setup — the app has no key of its own and opens no network
connection. What doctor exists to say out loud is whether the unattended loop has anything
to work on, and whether any campaign file has stopped parsing: the tools skip an unreadable
campaign so the rest keep working, which means doctor is the only place a skipped one
becomes visible.

This module reads the campaign directory itself instead of importing ``provider``: core
loads a CLI hook by file path, not as a package, so a sibling import is not guaranteed to
resolve when ``personalclaw doctor`` runs before anything has loaded the provider.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from personalclaw.sdk.cli import DoctorLine, SetupContext
from personalclaw.sdk.util import app_data_dir

APP_NAME = "research-lab"
DEFAULT_CYCLE_BUDGET = 5
DEFAULT_BREADTH = 3
STATUS_OPEN = "open"


def setup(ctx: SetupContext) -> None:
    """No credential to collect — just tell the user what the campaigns will inherit."""
    saved = ctx.settings.load(ctx.app_name) or {}
    budget = saved.get("default_cycle_budget", DEFAULT_CYCLE_BUDGET)
    breadth = saved.get("cycle_breadth", DEFAULT_BREADTH)
    ctx.print(
        f"Research Lab: nothing to configure. New campaigns get {budget} unattended "
        f"cycle(s), {breadth} sub-question(s) per cycle."
    )
    ctx.print(
        "The hourly 'advance-campaigns' cron runs the cycles; change either number in "
        "Settings → Tools → Research Lab."
    )


def doctor() -> list[DoctorLine]:
    """Report the campaign store, what the cron has to advance, and anything unreadable."""
    try:
        root = app_data_dir(APP_NAME) / "campaigns"
        files = sorted(root.glob("*/campaign.json")) if root.is_dir() else []
    except OSError as exc:
        return [DoctorLine("campaigns", "fail", str(exc))]

    campaigns, unreadable = _read(files)
    lines = [DoctorLine("campaigns", "ok", f"{root} — {len(campaigns)} campaign(s)")]
    if unreadable:
        lines.append(
            DoctorLine(
                "unreadable",
                "warn",
                f"{unreadable} campaign file(s) will not parse; the tools skip them",
            )
        )
    open_ones = [c for c in campaigns if c.get("status") == STATUS_OPEN]
    if not campaigns:
        lines.append(DoctorLine("open", "info", "none yet — ask for one with research_open"))
    elif not open_ones:
        lines.append(
            DoctorLine("open", "info", "none open; the hourly cron has nothing to advance")
        )
    else:
        lines.append(
            DoctorLine(
                "open",
                "ok",
                ", ".join(
                    f"{c.get('id')} (cycle {len(c.get('cycles') or [])}/{c.get('cycle_budget')})"
                    for c in open_ones
                ),
            )
        )
    return lines


def _read(files: list[Path]) -> tuple[list[dict[str, Any]], int]:
    campaigns: list[dict[str, Any]] = []
    unreadable = 0
    for path in files:
        try:
            campaigns.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            unreadable += 1
    return campaigns, unreadable
