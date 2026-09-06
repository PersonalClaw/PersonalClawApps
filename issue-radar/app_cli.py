"""CLI seams for issue-radar: a setup step and a doctor probe.

``personalclaw setup`` calls :func:`setup` after the core steps; ``personalclaw doctor``
calls :func:`doctor` and renders the lines it returns as this app's section.

There is nothing to collect at setup — the app has no key of its own. What it DOES have is
two hard external dependencies, one per tracker (``gh`` and ``glab``, each signed in), and
that is exactly the class of thing doctor exists to say out loud before a sweep fails at
the wire. Neither is required: a GitHub-only maintainer needs only ``gh``, so a missing
``glab`` is reported as information, not as a failure.

This module reads the note directory itself instead of importing ``triage``: core loads a
CLI hook by file path, not as a package, so a sibling import is not guaranteed to resolve
when ``personalclaw doctor`` runs before anything has loaded the provider.
"""

from __future__ import annotations

import shutil
import subprocess

from personalclaw.sdk.cli import DoctorLine, SetupContext
from personalclaw.sdk.util import app_data_dir

APP_NAME = "issue-radar"

# Each tracker: the binary, the sign-in check, and where to get it.
TRACKERS = (
    ("GitHub", "gh", ("gh", "auth", "status"), "https://cli.github.com"),
    ("GitLab", "glab", ("glab", "auth", "status"), "https://gitlab.com/gitlab-org/cli"),
)


def setup(ctx: SetupContext) -> None:
    """No credential to collect — just tell the user what has to be true."""
    present = [name for name, binary, _, _ in TRACKERS if shutil.which(binary)]
    if present:
        ctx.print(
            "Issue Radar: "
            + ", ".join(f"`{b}` found" for n, b, _, _ in TRACKERS if n in present)
            + ". Run the matching `auth login` if you haven't."
        )
    if len(present) < len(TRACKERS):
        missing = [(n, b, url) for n, b, _, url in TRACKERS if n not in present]
        for name, binary, url in missing:
            ctx.print(f"Issue Radar: no `{binary}` — install it from {url} to triage {name}.")
    ctx.print(
        "The app reads issue trackers only through those CLIs — it opens no network "
        "connection of its own, and it never writes to a tracker."
    )


def doctor() -> list[DoctorLine]:
    """Report each tracker CLI's sign-in state, then what is kept locally."""
    lines = [DoctorLine("Issue Radar", "ok", "reads trackers through your own CLIs; never writes")]
    for name, binary, probe, url in TRACKERS:
        lines.append(_tracker_line(name, binary, probe, url))
    lines.append(_notes_line())
    return lines


def _tracker_line(name: str, binary: str, probe: tuple[str, ...], url: str) -> DoctorLine:
    # The probe is labeled by the binary it actually runs (`gh`, `glab`), the way
    # code-review labels its `gh` probe — the host name stays in the detail text.
    if not shutil.which(binary):
        return DoctorLine(
            binary, "info", f"no `{binary}` on PATH — install it from {url} to triage {name}"
        )
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell, no user input
            list(probe),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return DoctorLine(binary, "warn", f"`{' '.join(probe)}` could not be run: {exc}")
    if proc.returncode != 0:
        return DoctorLine(
            binary, "warn", f"`{binary}` is installed but not signed in — run `{binary} auth login`"
        )
    return DoctorLine(binary, "ok", f"`{binary}` installed and authenticated")


def _notes_line() -> DoctorLine:
    """What the app has kept. A store nobody can find is a store nobody trusts."""
    try:
        root = app_data_dir(APP_NAME)
        notes = sorted((root / "notes").glob("*.jsonl")) if (root / "notes").is_dir() else []
        sweeps = sorted((root / "sweeps").glob("*.json")) if (root / "sweeps").is_dir() else []
    except OSError as exc:
        return DoctorLine("kept locally", "fail", str(exc))
    if not notes and not sweeps:
        return DoctorLine("kept locally", "info", f"{root} — nothing swept yet")
    return DoctorLine(
        "kept locally",
        "ok",
        f"{root} — {len(sweeps)} sweep(s), notes on {len(notes)} issue(s)",
    )
