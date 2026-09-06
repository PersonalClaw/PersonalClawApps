"""CLI seams for spec-builder: a setup step and a doctor probe.

``personalclaw setup`` calls :func:`setup` after the core steps; ``personalclaw doctor``
calls :func:`doctor` and renders the lines it returns as this app's section.

There is no credential to collect. What there IS is a soft external dependency — `git`, which
only ``spec_seed`` needs — and a source repository the user has to point at before seeding
works at all. Both are exactly the class of thing doctor exists to say out loud, before a tool
call fails at the wire.

This module reads the spec directory itself instead of importing ``specs``: core loads a CLI
hook by file path, not as a package, so a sibling import is not guaranteed to resolve when
``personalclaw doctor`` runs before anything has loaded the provider.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from personalclaw.sdk.cli import DoctorLine, SetupContext
from personalclaw.sdk.util import app_data_dir

APP_NAME = "spec-builder"
SPEC_FILE = "spec.json"
REQUIRED_SECTIONS = ("problem", "outcome", "steps", "verification")


def _git_version() -> str | None:
    """The installed git's version string, or None if git cannot be run."""
    if not shutil.which("git"):
        return None
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell, no user input
            ["git", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return (proc.stdout or "").strip() or None if proc.returncode == 0 else None


def setup(ctx: SetupContext) -> None:
    """No credential to collect — just tell the user what has to be true, and where."""
    saved = ctx.settings.load(ctx.app_name) or {}
    repo = str(saved.get("source_repo") or "").strip()
    ctx.print(
        "Spec Builder: nothing to configure. A spec compiles into a workflow definition — "
        "this app writes the definition, the workflow engine runs it."
    )
    if repo:
        ctx.print(f"Specs can be seeded from {repo} (read-only, via `git show`).")
    else:
        ctx.print(
            "Set 'Source repository' in Settings -> Tools -> Spec Builder to seed a spec from "
            "the code it is about. Without it every other tool still works; only spec_seed "
            "is unavailable."
        )
    if not _git_version():
        ctx.print("Install git (https://git-scm.com) if you want spec_seed.")


def doctor() -> list[DoctorLine]:
    """Report the spec store, what is ready to compile, and whether seeding can work."""
    lines: list[DoctorLine] = []
    try:
        root = app_data_dir(APP_NAME) / "specs"
        files = sorted(root.glob(f"*/{SPEC_FILE}")) if root.is_dir() else []
    except OSError as exc:
        return [DoctorLine("Spec Builder", "fail", str(exc))]

    specs, unreadable = _read(files)
    lines.append(DoctorLine("Spec Builder", "ok", f"{root} — {len(specs)} spec(s)"))
    if unreadable:
        lines.append(
            DoctorLine(
                "unreadable",
                "warn",
                f"{unreadable} spec record(s) will not parse; the tools skip them",
            )
        )
    if not specs:
        lines.append(DoctorLine("ready", "info", "no specs yet — start one with spec_open"))
    else:
        ready = [s for s in specs if _has_required(s)]
        if ready:
            lines.append(
                DoctorLine(
                    "ready",
                    "ok",
                    ", ".join(str(s.get("id") or "?") for s in ready),
                )
            )
        else:
            lines.append(
                DoctorLine("ready", "info", "none have all four required sections filled in")
            )
    version = _git_version()
    if version is None:
        lines.append(
            DoctorLine("seeding", "warn", "`git` is not runnable — spec_seed is unavailable")
        )
    else:
        lines.append(DoctorLine("seeding", "ok", f"{version} — spec_seed can read a revision"))
    return lines


def _has_required(spec: dict[str, Any]) -> bool:
    """Whether all four required sections carry text.

    Deliberately weaker than the provider's readiness verdict, which also parses the bullet
    grammars. Doctor is a presence check, not a second implementation of the gate — one that
    could disagree with the real one would be worse than not reporting at all.
    """
    sections = spec.get("sections")
    if not isinstance(sections, dict):
        return False
    return all(str(sections.get(name) or "").strip() for name in REQUIRED_SECTIONS)


def _read(files: list[Path]) -> tuple[list[dict[str, Any]], int]:
    specs: list[dict[str, Any]] = []
    unreadable = 0
    for path in files:
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            unreadable += 1
            continue
        if isinstance(loaded, dict):
            specs.append(loaded)
        else:
            unreadable += 1
    return specs, unreadable
