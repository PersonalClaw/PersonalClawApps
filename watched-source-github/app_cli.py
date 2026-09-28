"""CLI seams for watched-source-github: a setup step and a doctor probe.

``personalclaw setup`` calls :func:`setup` after the core steps; ``personalclaw doctor``
calls :func:`doctor` and renders the lines it returns as this app's section.
"""

from __future__ import annotations

from personalclaw.sdk.cli import DoctorLine, SetupContext
from personalclaw.sdk.settings import ProviderSettings
from watched_source_github_repos import parse_repos

APP_NAME = "watched-source-github"


def _watch_lines(saved: dict) -> list[DoctorLine]:
    watched, skipped = parse_repos(saved.get("repos", ""))
    lines: list[DoctorLine] = []
    if watched:
        lines.append(DoctorLine("repositories", "ok", f"watching {', '.join(watched)}"))
    else:
        lines.append(
            DoctorLine(
                "repositories",
                "warn",
                "none set, so nothing is watched — add owner/name entries in the app's settings",
            )
        )
    if skipped:
        lines.append(
            DoctorLine(
                "skipped entries",
                "warn",
                f"not owner/name, so not watched: {', '.join(skipped)}",
            )
        )
    token = str(saved.get("token", "") or "").strip()
    lines.append(
        DoctorLine(
            "token",
            "ok",
            (
                "set — private repositories are readable and the rate limit is the account's"
                if token
                else "not set — public repositories only, at GitHub's anonymous rate limit"
            ),
        )
    )
    return lines


def setup(ctx: SetupContext) -> None:
    """Say what is watched and where to change it. The token is set in the app's settings,
    where it is stored as a secret and never shown again."""
    saved = ctx.settings.load(ctx.app_name) or {}
    for line in _watch_lines(saved):
        ctx.print(f"GitHub Repo Watcher: {line.label}: {line.detail}")
    ctx.print(
        "GitHub Repo Watcher: repositories, the token and the poll interval are set in the "
        "app's settings. Each new release or issue fires the automations bound to its event."
    )


def doctor() -> list[DoctorLine]:
    """Report what is watched, which entries were skipped, and whether a token is set."""
    return _watch_lines(ProviderSettings.load(APP_NAME) or {})
