"""Wiring: one client, one model, one notifier, ONE doorbell.

The object graph is built here and nowhere else, which is what makes "exactly one
``/api/ws`` connection" checkable rather than aspirational — there is a single
:class:`~menubar_companion.doorbell.Doorbell` construction in the whole package, and
opening the menu or refreshing goes nowhere near it.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from menubar_companion.api import GatewayClient
from menubar_companion.doorbell import Doorbell
from menubar_companion.model import CompanionModel, RefreshResult
from menubar_companion.notify import Notifier
from menubar_companion.settings import Settings


@dataclass
class Companion:
    """The whole running app, minus the chrome."""

    settings: Settings
    client: GatewayClient
    model: CompanionModel
    notifier: Notifier
    doorbell: Doorbell
    #: Connection state as the doorbell last reported it, for the menu.
    link_state: str = "connecting"
    #: Bumped on every refresh, whatever triggered it. A host redraws on change.
    revision: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ── the one thing the socket is allowed to do ──

    def refresh_and_notify(self) -> RefreshResult:
        """Re-read over HTTP, then notify about what is NEW.

        This is the doorbell's ring callback. It takes no arguments — see
        ``doorbell`` for why that signature is the guarantee and not a detail.
        """
        with self._lock:
            result = self.model.refresh()
            self.revision += 1
        if not result.ok:
            return result
        for approval_id in result.new_approvals:
            self.notifier.post("Approval needed", f"A tool is waiting on you ({approval_id})")
        for loop_id in result.new_needs_input:
            self.notifier.post("A loop needs your input", loop_id)
        return result

    # ── menu actions ──

    def resolve(self, approval_id: str, action: str) -> bool:
        """Approve/Deny. A failure lands in ``model.last_error`` and stays visible."""
        outcome = self.model.resolve(approval_id, action)
        with self._lock:
            self.revision += 1
        return outcome.ok

    def toggle_mute(self) -> bool:
        """Flip the Settings mute switch and persist it. Returns the new state."""
        self.settings.set_muted(not self.settings.notifications_muted)
        with self._lock:
            self.revision += 1
        return self.settings.notifications_muted

    def note_link_state(self, state: str) -> None:
        self.link_state = state
        with self._lock:
            self.revision += 1


def build_companion(
    settings: Settings | None = None,
    *,
    connect: Callable[[str], object] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    opener=None,
    runner=None,
) -> Companion:
    """Assemble the app. The ONLY ``Doorbell(...)`` call in the package."""
    cfg = settings or Settings.load()
    client = GatewayClient(cfg.url, cfg.token, opener=opener)
    model = CompanionModel(client)
    notifier = (
        Notifier(lambda: cfg.notifications_muted, runner=runner)
        if runner is not None
        else Notifier(lambda: cfg.notifications_muted)
    )
    companion = Companion(
        settings=cfg,
        client=client,
        model=model,
        notifier=notifier,
        doorbell=Doorbell(  # ← one connection for the process lifetime
            url=client.socket_url(),
            origin=client.origin(),
            on_ring=lambda: None,  # replaced below; see the note
            connect=connect,
            sleep=sleep,
        ),
    )
    # The ring callback needs the Companion, and the Companion needs the Doorbell, so one
    # of the two is installed after construction. It is installed through the same
    # zero-argument gate the constructor used, so the payload-blindness rail still holds.
    companion.doorbell.set_ring(companion.refresh_and_notify)
    companion.doorbell.set_state_callback(companion.note_link_state)
    return companion


def start_background(
    companion: Companion,
    should_stop: Callable[[], bool] = lambda: False,
) -> list[threading.Thread]:
    """Run the socket and the floor poll. Daemon threads: the host owns the main loop.

    The floor poll is NOT a second socket and not a fallback for reading payloads — it
    exists because a change that produces no frame at all (or a frame filtered before it
    reaches us) must still land eventually.
    """
    threads = [
        threading.Thread(
            target=companion.doorbell.run_forever,
            args=(should_stop,),
            name="companion-doorbell",
            daemon=True,
        ),
        threading.Thread(
            target=_poll_floor,
            args=(companion, should_stop),
            name="companion-poll",
            daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()
    return threads


def _poll_floor(companion: Companion, should_stop: Callable[[], bool]) -> None:
    interval = max(5, int(companion.settings.poll_seconds))
    while not should_stop():
        companion.refresh_and_notify()
        for _ in range(interval):
            if should_stop():
                return
            time.sleep(1)
