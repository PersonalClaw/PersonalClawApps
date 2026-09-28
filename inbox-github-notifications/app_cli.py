"""CLI seams for inbox-github-notifications: a setup step and a doctor probe.

``personalclaw setup`` calls :func:`setup` after the core steps; ``personalclaw doctor``
calls :func:`doctor` and renders the lines it returns as this app's section.
"""

from __future__ import annotations

from personalclaw.sdk.cli import DoctorLine, SetupContext
from personalclaw.sdk.settings import ProviderSettings

APP_NAME = "inbox-github-notifications"


def _token_line(saved: dict) -> DoctorLine:
    if str(saved.get("token", "") or "").strip():
        return DoctorLine("token", "ok", "set — your notifications are read with it")
    return DoctorLine(
        "token",
        "warn",
        "not set, so no notification is read — add a GitHub token in the app's settings",
    )


def setup(ctx: SetupContext) -> None:
    """Say whether a token is set and where to set it. It is stored as a secret and never shown
    again, so it is entered in the app's settings rather than echoed here."""
    line = _token_line(ctx.settings.load(ctx.app_name) or {})
    ctx.print(f"GitHub Notifications Inbox: {line.label}: {line.detail}")
    ctx.print(
        "GitHub Notifications Inbox: it only reads. Replies and reactions happen on GitHub, "
        "and a watched-channels list limits the inbox to the repositories you name."
    )


def doctor() -> list[DoctorLine]:
    """Report whether the token is set, the one thing that makes this source read anything."""
    return [_token_line(ProviderSettings.load(APP_NAME) or {})]
