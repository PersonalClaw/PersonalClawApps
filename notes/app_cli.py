"""CLI seams for notes: a setup step and a doctor probe.

``personalclaw setup`` calls :func:`setup` after the core steps; ``personalclaw doctor``
calls :func:`doctor` and renders the lines it returns as this app's section.

There is no credential to collect. What there IS is a hard external dependency — `git`,
which is what makes note history real — and a notebook location the user should be told
about out loud, because it is where their writing will live.
"""

from __future__ import annotations

import shutil
import subprocess

from personalclaw.sdk.cli import DoctorLine, SetupContext


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
    if _git_version():
        ctx.print(
            "Notes: `git` found. Your notebook lives in this app's data dir by default "
            "(Settings → Tools → Notes to point it somewhere else, e.g. a folder in a "
            "repo you already sync)."
        )
    else:
        ctx.print(
            "Notes: install git (https://git-scm.com) — the notebook IS a git repository, "
            "which is how note history survives a reinstall and stays readable without "
            "PersonalClaw."
        )


def doctor() -> list[DoctorLine]:
    """Report whether git is present, since without it there is no note history."""
    version = _git_version()
    if version is None:
        return [DoctorLine(
            label="git",
            status="fail",
            detail="`git` is not runnable — install git; the notebook is a git repository",
        )]
    return [DoctorLine(
        label="git",
        status="ok",
        detail=f"{version} — notes are versioned in git",
    )]
