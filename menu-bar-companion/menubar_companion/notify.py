"""Native macOS notifications, and the mute switch that suppresses them.

Muting is checked HERE, at the single place a notification is posted, rather than at
each caller. A preference enforced at the call sites is a preference that stops working
the first time someone adds a fourth call site.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Sequence


def _default_runner(argv: Sequence[str]) -> None:
    subprocess.run(list(argv), check=False, timeout=10)  # noqa: S603 - fixed argv below


class Notifier:
    """Posts a macOS notification unless the user muted them.

    ``is_muted`` is a callable, not a captured bool: the Settings menu flips the
    preference on a live process, and a snapshot taken at construction would keep
    notifying after the user asked it to stop.
    """

    def __init__(
        self,
        is_muted: Callable[[], bool],
        runner: Callable[[Sequence[str]], None] = _default_runner,
        osascript: str | None = None,
    ):
        self._is_muted = is_muted
        self._runner = runner
        self._osascript = osascript if osascript is not None else (shutil.which("osascript") or "")
        self.posted = 0
        self.suppressed = 0

    def post(self, title: str, body: str) -> bool:
        """Return True when a notification was actually posted."""
        if self._is_muted():
            self.suppressed += 1
            return False
        if not self._osascript:
            # No osascript (not macOS, or a stripped environment): staying silent is
            # correct, and it is counted as suppressed rather than reported as posted.
            self.suppressed += 1
            return False
        script = (
            "display notification "
            f"{_as_applescript_string(body)} with title {_as_applescript_string(title)}"
        )
        self._runner([self._osascript, "-e", script])
        self.posted += 1
        return True


def _as_applescript_string(text: str) -> str:
    """Quote *text* as an AppleScript string literal.

    Loop names and tool names are attacker-influenced text in the general case, and it
    reaches ``osascript``. Escaping backslashes and quotes keeps it one argument to
    ``display notification`` instead of the tail of a script.
    """
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
