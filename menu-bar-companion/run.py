#!/usr/bin/env python3
"""Launcher for the PersonalClaw menu-bar companion.

    python3 run.py --configure http://localhost:10000 <token>   # store URL + token (0600)
    python3 run.py --check                                      # one read, print the menu
    python3 run.py                                              # live in the menu bar

``--check`` is the honest smoke test: it proves the token works, prints exactly what the
menu would show, and says whether a status-item backend is present on this machine.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from menubar_companion.app import build_companion, start_background  # noqa: E402
from menubar_companion.settings import Settings  # noqa: E402
from menubar_companion.tray import build_menu, headless_render, resolve_host  # noqa: E402


def _configure(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: run.py --configure <gateway-url> <token>", file=sys.stderr)
        return 2
    settings = Settings.load()
    settings.url, settings.token = argv[0].rstrip("/"), argv[1]
    path = settings.save()
    print(f"saved {path} (mode 0600)")
    return 0


def _check() -> int:
    settings = Settings.load()
    if not settings.url or not settings.token:
        print(
            "not configured — run: run.py --configure <gateway-url> <token>\n"
            "(get both from `personalclaw token`; or export "
            "PERSONALCLAW_COMPANION_URL / PERSONALCLAW_COMPANION_TOKEN to avoid "
            "persisting the token)",
            file=sys.stderr,
        )
        return 2
    companion = build_companion(settings)
    result = companion.model.refresh()
    print(headless_render(companion.model, settings))
    host, reason = resolve_host()
    print(f"\nstatus-item host: {'ready' if host else reason}")
    return 0 if result.ok else 1


def _run_headless(companion, settings: Settings) -> int:
    """No status-item backend: print the menu on every change instead of dying."""
    import time

    start_background(companion)
    last = -1
    try:
        while True:
            if companion.revision != last:
                last = companion.revision
                print(f"\n[{companion.link_state}]")
                print(headless_render(companion.model, settings))
            time.sleep(1)
    except KeyboardInterrupt:
        return 0


def _run_status_item(host, companion, settings: Settings) -> int:
    """Drive ``rumps``' NSStatusItem from the same menu data the tests assert on."""

    class CompanionApp(host.App):  # type: ignore[misc, name-defined]
        def __init__(self) -> None:
            super().__init__("PersonalClaw", quit_button="Quit")
            # Set the seen-revision BEFORE the first draw, or the first tick redraws a
            # menu that is already current.
            self._revision = companion.revision
            self._redraw()
            host.Timer(lambda _t: self._tick(), 1).start()

        def _tick(self) -> None:
            if companion.revision != self._revision:
                self._revision = companion.revision
                self._redraw()

        def _redraw(self) -> None:
            self.title = f"PersonalClaw {companion.model.badge_text}".strip()
            items = build_menu(
                companion.model,
                settings,
                on_resolve=companion.resolve,
                on_toggle_mute=companion.toggle_mute,
                on_refresh=companion.refresh_and_notify,
                open_url=_open_url,
            )
            self.menu.clear()
            for item in items:
                if item.action is None:
                    self.menu.add(host.MenuItem(item.title))
                else:
                    action = item.action
                    self.menu.add(host.MenuItem(item.title, callback=lambda _s, a=action: a()))

    start_background(companion)
    CompanionApp().run()
    return 0


def _open_url(url: str) -> None:
    import subprocess

    if url:
        subprocess.run(["/usr/bin/open", url], check=False, timeout=10)  # noqa: S603


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--configure":
        return _configure(argv[1:])
    if argv and argv[0] == "--check":
        return _check()
    settings = Settings.load()
    if not settings.url or not settings.token:
        return _check()
    companion = build_companion(settings)
    host, reason = resolve_host()
    if host is None:
        print(reason, file=sys.stderr)
        return _run_headless(companion, settings)
    return _run_status_item(host, companion, settings)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
