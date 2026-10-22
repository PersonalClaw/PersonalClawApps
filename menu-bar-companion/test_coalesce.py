"""One read per burst: the coalescer behind the doorbell.

Every frame an owner socket hears rings the doorbell, a streamed chat's every chunk among them,
and each ring used to be one read of the gateway (two requests) on the socket's own thread. The
:class:`~menubar_companion.coalesce.Coalescer` sits behind the ring: a ring only says a read is
wanted, and one refresher does every read, at once after a quiet spell and at most once a gap
otherwise. The ring still takes no argument, so the doorbell's rule (a frame is a reason to
re-read, never data) is untouched.

The timing tests run the refresher on a fake clock (:func:`_drive`): after each ring, and at each
moment a wanted read falls due, it takes its turn, as a refresher thread that is never late would.
"""

from __future__ import annotations

import threading

import pytest
from _rows import approval, lists, running, waiting_approval
from _ws_fakes import HANDSHAKE_OK, FakeOpener, FakeSocket, server_frame
from menubar_companion.coalesce import GAP_SECS, Coalescer
from menubar_companion.doorbell import OP_CLOSE, OP_TEXT

HTTP_WAITING = [waiting_approval(approval(id="http-a1"))]
HTTP_RUNNING = [running("loop:L1", "ship it", "Running")]


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _counting(clock: FakeClock, during=None):
    reads: list[float] = []

    def refresh() -> None:
        reads.append(clock.now)
        if during is not None:
            during()

    return refresh, reads


def _drive(coalescer: Coalescer, clock: FakeClock, rings: list[float], until: float) -> None:
    """Ring at each time in *rings*, the refresher taking its turn after each ring and whenever a
    wanted read falls due, until *until*."""
    due: float | None = None

    def catch_up(to: float) -> None:
        nonlocal due
        while due is not None and due <= to:
            clock.now = due
            wait = coalescer.step()
            due = None if wait is None else clock.now + wait

    for at in rings:
        catch_up(at)
        clock.now = at
        coalescer.ring()
        wait = coalescer.step()
        due = None if wait is None else clock.now + wait
    catch_up(until)
    clock.now = until


# ── a burst is one read at its start and one at its end ──


def test_two_hundred_rings_inside_100_ms_make_two_reads():
    clock = FakeClock()
    refresh, reads = _counting(clock)
    coalescer = Coalescer(refresh, gap=1.0, clock=clock)
    _drive(coalescer, clock, [i * 0.0005 for i in range(200)], until=5.0)
    assert coalescer.rings == 200
    assert reads == [0.0, 1.0], "the leading read at the first ring, one at the burst's end"


def test_a_ring_every_50_ms_for_10_s_reads_at_most_once_a_gap():
    clock = FakeClock()
    refresh, reads = _counting(clock)
    coalescer = Coalescer(refresh, gap=1.0, clock=clock)
    _drive(coalescer, clock, [i * 0.05 for i in range(200)], until=15.0)
    assert 10 <= len(reads) <= 11, reads
    assert all(b - a >= 1.0 for a, b in zip(reads, reads[1:])), reads
    assert reads[-1] >= 9.95, "the last ring of the stream is read"


def test_the_first_ring_after_a_quiet_minute_reads_with_no_wait():
    clock = FakeClock()
    refresh, reads = _counting(clock)
    coalescer = Coalescer(refresh, gap=1.0, clock=clock)
    _drive(coalescer, clock, [0.0], until=60.0)
    clock.now = 60.0
    coalescer.ring()
    assert coalescer.step() is None, "read in the ring's own turn, nothing left wanted"
    assert reads == [0.0, 60.0]


def test_rings_during_a_read_want_exactly_one_more_read():
    clock = FakeClock()
    coalescer: Coalescer

    def during() -> None:
        if len(reads) == 1:
            for _ in range(3):
                coalescer.ring()

    refresh, reads = _counting(clock, during)
    coalescer = Coalescer(refresh, gap=1.0, clock=clock)
    _drive(coalescer, clock, [0.0], until=10.0)
    assert reads == [0.0, 1.0]


def test_nothing_rung_reads_nothing():
    clock = FakeClock()
    refresh, reads = _counting(clock)
    coalescer = Coalescer(refresh, gap=1.0, clock=clock)
    assert coalescer.step() is None
    assert reads == []


def test_the_gap_is_a_second():
    """A menu opened on a click needs no sub-second freshness, and the first change after a quiet
    spell is read at once whatever the gap. The server's own 0.15 s window gathers a list's
    changes; this gap bounds the frames the doorbell cannot tell apart from them."""
    assert GAP_SECS == 1.0


# ── the ring still takes nothing ──


def test_ring_refuses_an_argument():
    coalescer = Coalescer(lambda: None)
    with pytest.raises(TypeError):
        coalescer.ring(b'{"type": "approvals"}')  # type: ignore[call-arg]


def test_a_refresh_that_wants_an_argument_is_refused():
    with pytest.raises(TypeError, match="refetch signal, not a payload"):
        Coalescer(lambda frame: None)


# ── the refresher thread ──


def test_the_refresher_reads_on_a_ring_and_stops_when_asked():
    read = threading.Event()
    stop = threading.Event()
    coalescer = Coalescer(read.set, gap=0.05)
    thread = threading.Thread(target=coalescer.run_forever, args=(stop.is_set,), daemon=True)
    thread.start()
    try:
        coalescer.ring()
        assert read.wait(5), "a ring is read"
    finally:
        stop.set()
        coalescer.ring()  # wakes an idle wait, so the stop is seen at once
        thread.join(5)
    assert not thread.is_alive()


def test_a_read_that_raises_is_kept_and_the_refresher_carries_on():
    calls: list[int] = []
    second = threading.Event()
    stop = threading.Event()

    def refresh() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("the gateway said something unexpected")
        second.set()

    coalescer = Coalescer(refresh, gap=0.01)
    thread = threading.Thread(target=coalescer.run_forever, args=(stop.is_set,), daemon=True)
    thread.start()
    try:
        coalescer.ring()
        for _ in range(500):
            if coalescer.last_error or not thread.is_alive():
                break
            threading.Event().wait(0.01)
        coalescer.ring()
        assert second.wait(5), "the refresher is still reading"
        assert "said something unexpected" in coalescer.last_error
    finally:
        stop.set()
        coalescer.ring()
        thread.join(5)


# ── wired into the app ──


def _opener() -> FakeOpener:
    return FakeOpener(lists(HTTP_WAITING, HTTP_RUNNING))


class _StopAfter:
    def __init__(self, n: int) -> None:
        self.n = n

    def __call__(self) -> bool:
        self.n -= 1
        return self.n < 0


class _RefresherAlongside(FakeSocket):
    """A socket whose every read gives the refresher a turn, a tenth of a millisecond on: the
    refresher thread running beside the socket's, made deterministic."""

    def __init__(self, script: bytes, turn) -> None:
        super().__init__(script)
        self._turn = turn

    def recv(self, count: int) -> bytes:
        self._turn()
        return super().recv(count)


def _companion(sock_for, clock: FakeClock, opener: FakeOpener):
    from menubar_companion.app import build_companion
    from menubar_companion.settings import Settings

    return build_companion(
        Settings(url="http://127.0.0.1:10000", token="tok"),
        connect=lambda _url: sock_for(),
        sleep=lambda _s: None,
        opener=opener,
        runner=lambda _argv: None,
        clock=clock,
    )


def test_a_burst_of_50_frames_through_the_real_doorbell_is_two_reads():
    clock = FakeClock()
    opener = _opener()
    script = HANDSHAKE_OK + server_frame(OP_TEXT, b'{"type": "chat_chunk"}') * 50
    script += server_frame(OP_CLOSE)
    holder: dict = {}

    def turn() -> None:
        clock.now += 0.0001
        holder["companion"].coalescer.step()

    holder["companion"] = companion = _companion(
        lambda: _RefresherAlongside(script, turn), clock, opener
    )
    companion.doorbell.run_forever(_StopAfter(1))
    assert companion.doorbell.rings == 50
    clock.now += 1.0
    companion.coalescer.step()

    assert len(opener.calls) == 4, f"two reads of two requests each, not {len(opener.calls)}"


def test_a_ring_does_no_http_on_the_sockets_thread():
    clock = FakeClock()
    opener = _opener()
    script = HANDSHAKE_OK + server_frame(OP_TEXT, b"{}") * 5 + server_frame(OP_CLOSE)
    companion = _companion(lambda: FakeSocket(script), clock, opener)
    companion.doorbell.run_forever(_StopAfter(1))
    assert companion.doorbell.rings == 5
    assert opener.calls == [], "the socket's thread only rang"
    assert companion.coalescer.step() is None
    assert len(opener.calls) == 2, "the refresher read once for all five"


def test_the_floor_poll_rings_rather_than_reads():
    from menubar_companion.app import _poll_floor

    clock = FakeClock()
    opener = _opener()
    companion = _companion(lambda: FakeSocket(b""), clock, opener)
    _poll_floor(companion, _StopAfter(1))
    assert companion.coalescer.rings == 1
    assert opener.calls == []
