"""CLI seams for code-review: a setup step and a doctor probe.

``personalclaw setup`` calls :func:`setup` after the core steps; ``personalclaw doctor``
calls :func:`doctor` and renders the lines it returns as this app's section.

There is nothing to collect at setup — the app has no key of its own. What it DOES have
is a hard external dependency (``gh``, signed in), and that is exactly the class of thing
doctor exists to say out loud before a review fails at the wire.
"""

from __future__ import annotations

import shutil
import subprocess

from personalclaw.sdk.cli import DoctorLine, SetupContext


def setup(ctx: SetupContext) -> None:
    """No credential to collect — just tell the user what has to be true."""
    if shutil.which("gh"):
        ctx.print("Code Review: `gh` found. Run `gh auth login` if you haven't.")
    else:
        ctx.print(
            "Code Review: install the GitHub CLI (https://cli.github.com) and run "
            "`gh auth login`. The app reads GitHub only through `gh` — it opens no "
            "network connection of its own."
        )


def doctor() -> list[DoctorLine]:
    """Report whether `gh` is present and authenticated."""
    if not shutil.which("gh"):
        return [DoctorLine(
            label="Code Review",
            status="fail",
            detail="`gh` is not on PATH — install the GitHub CLI, then `gh auth login`",
        )]
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell, no user input
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [DoctorLine(
            label="Code Review",
            status="warn",
            detail=f"`gh auth status` could not be run: {exc}",
        )]
    if proc.returncode != 0:
        return [DoctorLine(
            label="Code Review",
            status="warn",
            detail="`gh` is installed but not signed in — run `gh auth login`",
        )]
    return [DoctorLine(
        label="Code Review",
        status="ok",
        detail="`gh` installed and authenticated",
    )]
