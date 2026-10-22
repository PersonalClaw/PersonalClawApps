"""One read per burst of rings, behind the doorbell.

An owner socket hears every frame the gateway sends, a streamed chat's every chunk among them,
and the doorbell cannot tell one frame from another without reading it, which it never does. So
the ring only says a read is wanted, and one refresher thread does every read:

- at once, when :data:`GAP_SECS` has passed since the last read, so the first change after a
  quiet spell (a new approval) shows at once;
- otherwise when it has, so a burst of any size is one read at its start and at most one at its
  end, and a steady stream is one read a gap;
- a ring during a read wants exactly one more.

:meth:`Coalescer.ring` takes no argument, and the refresh it calls is held to the doorbell's own
zero-argument gate, so nothing a frame carries can reach a read through here either.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from menubar_companion.doorbell import _reject_payload_consuming

#: The least time between two reads. A menu opened on a click needs no sub-second freshness, and
#: the first ring after a quiet spell reads at once whatever the gap. The gateway gathers a
#: list's own changes into one frame per 0.15 s; this gap bounds the cost of every other frame
#: the socket hears, which the doorbell cannot tell apart from those.
GAP_SECS = 1.0

#: How long an idle refresher waits before it looks at ``should_stop`` again. A ring wakes it at
#: once; this only bounds how long a stop takes to be seen.
IDLE_CHECK_SECS = 1.0


class Coalescer:
    """Rings in, reads out: at most one read a gap, and the first ring after quiet at once."""

    def __init__(
        self,
        refresh: Callable[[], object],
        gap: float = GAP_SECS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        _reject_payload_consuming(refresh)
        self._refresh = refresh
        self.gap = gap
        self._clock = clock
        self._sleep = sleep
        self._cv = threading.Condition()
        self._wanted = False
        #: When the last read started, on ``clock``; ``None`` before the first.
        self._last: float | None = None
        self.rings = 0
        self.reads = 0
        #: What the last read raised, kept for display; the refresher carries on.
        self.last_error = ""

    def ring(self) -> None:
        """A read is wanted. Takes nothing, does no I/O, and returns at once."""
        with self._cv:
            self.rings += 1
            self._wanted = True
            self._cv.notify()

    def _until_due(self) -> float:
        return 0.0 if self._last is None else max(0.0, self._last + self.gap - self._clock())

    def step(self) -> float | None:
        """The refresher's turn: read if a read is wanted and due.

        Returns ``None`` when no read is wanted after it, else the seconds until the wanted one is
        due.
        """
        with self._cv:
            if not self._wanted:
                return None
            wait = self._until_due()
            if wait > 0:
                return wait
            self._wanted = False
            self._last = self._clock()
        self._read()
        with self._cv:
            return self._until_due() if self._wanted else None

    def _read(self) -> None:
        self.reads += 1
        try:
            self._refresh()
        except Exception as exc:  # noqa: BLE001 - a read that raises must not end every later read
            self.last_error = str(exc) or type(exc).__name__

    def run_forever(self, should_stop: Callable[[], bool] = lambda: False) -> None:
        """Do every read, for as long as the app runs."""
        while not should_stop():
            wait = self.step()
            if wait is None:
                with self._cv:
                    if not self._wanted:
                        self._cv.wait(IDLE_CHECK_SECS)
            elif wait > 0:
                self._sleep(wait)
