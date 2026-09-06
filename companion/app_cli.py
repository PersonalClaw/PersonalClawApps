"""CLI seams for companion: a setup step and a doctor probe.

``personalclaw setup`` calls :func:`setup` after the core steps; ``personalclaw doctor``
calls :func:`doctor` and renders the lines it returns as this app's section.

There is no credential to collect and no external binary to find. What there IS is a set of
switches that are all off — so a user who installed this app and heard nothing from it needs to
be told that is correct and where the switches are — and one fact that is easy to get wrong and
expensive to discover late: **an unset timezone means UTC**, because that is what core's arm
path falls back to. A reminder set for 08:30 on a machine in Berlin fires at 10:30 local unless
the zone is known. This is exactly the class of thing doctor exists to say out loud.

This module reads the store file itself instead of importing ``companion``: core loads a CLI
hook by file path, not as a package, so a sibling import is not guaranteed to resolve when
``personalclaw doctor`` runs before anything has loaded the provider.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from personalclaw.sdk.cli import DoctorLine, SetupContext
from personalclaw.sdk.settings import ProviderSettings
from personalclaw.sdk.util import app_data_dir

APP_NAME = "companion"
STORE_FILE = "companion.json"
SURFACES: tuple[tuple[str, str], ...] = (
    ("reminders", "Reminders"),
    ("watchlist", "Watchlist"),
    ("day_brief", "Day brief"),
)


def _local_zone_name() -> str:
    """The machine's IANA zone from ``/etc/localtime``, or ``""``.

    Duplicated from ``companion.local_zone_name`` for the reason in the module docstring: a
    doctor hook cannot rely on a sibling import. Twelve lines of readlink is a cheaper price
    than a doctor that raises ImportError on the one run where it matters.
    """
    try:
        target = os.readlink("/etc/localtime")
    except OSError:
        return ""
    _, marker, tail = target.partition("zoneinfo/")
    if not marker:
        return ""
    name = tail.strip("/")
    if not name or ".." in name.split("/"):
        return ""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return ""
    return name


def _on_surfaces(saved: dict[str, Any]) -> list[str]:
    """The labels of the surfaces currently switched on."""
    out = []
    for key, label in SURFACES:
        value = saved.get(key)
        if key == "day_brief":
            if str(value or "").strip():
                out.append(label)
        elif bool(value):
            out.append(label)
    return out


def setup(ctx: SetupContext) -> None:
    """No credential to collect — say what is off, and what UTC would cost."""
    saved = ctx.settings.load(ctx.app_name) or {}
    on = _on_surfaces(saved)
    if on:
        ctx.print(f"Companion: {', '.join(on)} on. Everything else stays quiet.")
    else:
        ctx.print(
            "Companion: all three surfaces are off, which is how it ships — it is contributing "
            "no automations at all. Turn on Reminders, Watchlist or set a Day brief time in "
            "Settings → Tools → Companion."
        )
    zone = str(saved.get("timezone") or "").strip() or _local_zone_name()
    if zone:
        ctx.print(f"Schedules will use {zone}.")
    else:
        ctx.print(
            "No timezone is set and this machine's could not be read, so schedules will run in "
            "UTC. Set 'Timezone' (e.g. Europe/Berlin) or an 08:30 reminder will fire at 08:30 "
            "UTC."
        )


def doctor() -> list[DoctorLine]:
    """Report the store, each surface, and the timezone actually in use.

    Deliberately app-name-then-per-check: one ``Companion`` line locating the store, then one
    line per thing that can independently be wrong. A single rolled-up verdict would hide which
    surface is the quiet one.
    """
    lines: list[DoctorLine] = []
    try:
        root = app_data_dir(APP_NAME)
    except OSError as exc:
        return [DoctorLine("Companion", "fail", str(exc))]
    store = root / STORE_FILE
    reminders, watches, unreadable = _read(store)
    if unreadable:
        lines.append(
            DoctorLine(
                "Companion",
                "warn",
                f"{store} will not parse — the app serves no automations until it is fixed "
                "or removed",
            )
        )
    else:
        lines.append(
            DoctorLine(
                "Companion",
                "ok",
                f"{root} — {reminders} reminder(s), {watches} watch(es)",
            )
        )
    saved = _settings()
    if saved is None:
        lines.append(
            DoctorLine(
                "surfaces",
                "info",
                "settings are not readable from here — check them in Settings → Tools",
            )
        )
    else:
        on = _on_surfaces(saved)
        if on:
            lines.append(DoctorLine("surfaces", "ok", f"on: {', '.join(on)}"))
        else:
            lines.append(
                DoctorLine(
                    "surfaces",
                    "info",
                    "all off (the shipped default) — this app is contributing no automations",
                )
            )
    configured = str((saved or {}).get("timezone") or "").strip()
    machine = _local_zone_name()
    if configured:
        try:
            ZoneInfo(configured)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            lines.append(
                DoctorLine(
                    "timezone",
                    "fail",
                    f"{configured!r} is not an IANA zone name — schedules are running in UTC",
                )
            )
        else:
            lines.append(DoctorLine("timezone", "ok", f"{configured} (configured)"))
    elif machine:
        lines.append(DoctorLine("timezone", "ok", f"{machine} (read from this machine)"))
    else:
        lines.append(
            DoctorLine(
                "timezone",
                "warn",
                "not set and not readable from /etc/localtime — schedules run in UTC, so a "
                "morning reminder will not fire in the morning",
            )
        )
    return lines


def _read(store: Path) -> tuple[int, int, bool]:
    """(reminders, watches, unreadable) from the store file. Never raises."""
    if not store.exists():
        return (0, 0, False)
    try:
        data = json.loads(store.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return (0, 0, True)
    if not isinstance(data, dict):
        return (0, 0, True)
    reminders = data.get("reminders")
    watches = data.get("watches")
    return (
        len(reminders) if isinstance(reminders, list) else 0,
        len(watches) if isinstance(watches, list) else 0,
        False,
    )


def _settings() -> dict[str, Any] | None:
    """This app's saved settings, or None when they cannot be read.

    ``doctor()`` takes no context — there is no ``SetupContext`` to hand it one — so it asks
    the SDK's own settings accessor directly rather than guessing at a file path. A failure
    reads as None, "cannot tell", rather than as "all off": reporting a surface as off when it
    is on is the one wrong answer this check can give.
    """
    try:
        loaded = ProviderSettings.load(APP_NAME)
    except Exception:  # noqa: BLE001 - a doctor probe must never be the thing that raises
        return None
    return loaded if isinstance(loaded, dict) else None
