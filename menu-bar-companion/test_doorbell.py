"""The doorbell rails: a refetch signal, ONE connection, and a backoff that grows.

The headline test is :func:`test_ws_frame_payload_never_reaches_the_rendered_menu`, and
it carries its own vacuity floor: the same fake socket, read by a deliberately
payload-CONSUMING reader written in this file, produces the opposite result. Without that
half, "the menu shows 2" would prove nothing — 2 is also what a broken app shows when the
frame happens to agree with HTTP.
"""

from __future__ import annotations

import json

import pytest
from _ws_fakes import HANDSHAKE_OK, FakeOpener, FakeSocket, server_frame
from menubar_companion.api import GatewayClient
from menubar_companion.doorbell import (
    BACKOFF_CAP,
    OP_CLOSE,
    OP_PING,
    OP_TEXT,
    Doorbell,
    backoff_delay,
    read_frame,
)
from menubar_companion.model import CompanionModel

# What HTTP says: two approvals, one loop needing input → badge 3.
HTTP_APPROVALS = json.dumps([{"id": "http-a1", "tool": "bash"}, {"id": "http-a2", "tool": "write"}])
HTTP_LOOPS = json.dumps(
    {
        "loops": [
            {"id": "L-live", "name": "ship it", "status": "running"},
            {"id": "L-blocked", "name": "which env?", "status": "needs_input"},
            {"id": "L-done", "name": "old", "status": "complete"},
        ]
    }
)

# What the SOCKET says: a lie, loudly. If any of this shows up in the menu, the socket is
# being read as a data channel.
WS_LIE = json.dumps(
    {
        "type": "approvals",
        "data": [{"id": f"WS-SENTINEL-{i}", "tool": "WS-SENTINEL-TOOL"} for i in range(99)],
    }
).encode()


def _model(opener: FakeOpener) -> CompanionModel:
    client = GatewayClient("http://127.0.0.1:10000", "tok", opener=opener)
    return CompanionModel(client)


def _opener() -> FakeOpener:
    return FakeOpener(
        {"/api/approvals": HTTP_APPROVALS.encode(), "/api/loops": HTTP_LOOPS.encode()}
    )


# ── the headline rail ──


def test_ws_frame_payload_never_reaches_the_rendered_menu():
    """A frame carrying 99 fabricated approvals must change nothing but the refetch."""
    opener = _opener()
    model = _model(opener)
    sock = FakeSocket(HANDSHAKE_OK + server_frame(OP_TEXT, WS_LIE) + server_frame(OP_CLOSE))
    bell = Doorbell(
        "ws://127.0.0.1:10000/api/ws?token=tok",
        "http://127.0.0.1:10000",
        on_ring=lambda: model.refresh(),
        connect=lambda _url: sock,
        sleep=lambda _s: None,
    )
    stop = _StopAfter(1)
    bell.run_forever(stop)

    # The frame DID ring — otherwise this test would pass by the socket doing nothing.
    assert bell.rings == 1
    assert ("GET", "http://127.0.0.1:10000/api/approvals") in opener.calls

    # …and everything rendered came from HTTP.
    assert model.badge == 3, "2 approvals + 1 needs_input, derived from the HTTP reads"
    assert [r.id for r in model.pending_approvals] == ["http-a1", "http-a2"]
    blob = json.dumps([vars(r) for r in model.pending_approvals] + [vars(r) for r in model.runs])
    assert "WS-SENTINEL" not in blob, "socket payload leaked into the rendered model"


def test_vacuity_floor_a_payload_consuming_reader_would_fail_that_assertion():
    """The floor for the test above: prove the assertion can fail.

    Same bytes, same fake socket — but read by a payload-consuming reader instead of the
    shipped payload-blind one. It surfaces the sentinel and the fabricated count, so the
    assertions above are discriminating rather than vacuously true.
    """
    sock = FakeSocket(HANDSHAKE_OK + server_frame(OP_TEXT, WS_LIE) + server_frame(OP_CLOSE))
    sock.recv(len(HANDSHAKE_OK))  # skip the head the shipped handshake would have eaten

    payload = _read_frame_returning_payload(sock)
    consumed = json.loads(payload)["data"]

    assert len(consumed) == 99, "a payload-consuming reader sees the fabricated rows"
    assert "WS-SENTINEL-0" == consumed[0]["id"]
    # And the shipped reader, on the same shape of frame, hands back an int — there is no
    # return path a payload could travel on.
    again = FakeSocket(server_frame(OP_TEXT, WS_LIE))
    assert read_frame(again) == OP_TEXT
    # The DECLARED return type is the guarantee: an int cannot carry a payload out.
    assert read_frame.__annotations__["return"] == "int"


def _read_frame_returning_payload(sock) -> bytes:
    """The design this app refuses: a reader that RETURNS the payload."""
    import struct

    first = sock.recv(2)
    length = first[1] & 0x7F
    if length == 126:
        (length,) = struct.unpack("!H", sock.recv(2))
    elif length == 127:
        (length,) = struct.unpack("!Q", sock.recv(8))
    out = b""
    while len(out) < length:
        out += sock.recv(length - len(out))
    return out


# ── the payload-blind gate is structural, not documentary ──


def test_a_payload_consuming_ring_callback_is_refused_at_construction():
    with pytest.raises(TypeError, match="refetch signal, not a payload"):
        Doorbell("ws://x/api/ws", "http://x", on_ring=lambda payload: None)


def test_the_zero_argument_callback_is_accepted():
    """Vacuity floor for the refusal above: the gate is not rejecting everything."""
    bell = Doorbell("ws://x/api/ws", "http://x", on_ring=lambda: None)
    assert bell.rings == 0


def test_set_ring_goes_through_the_same_gate():
    bell = Doorbell("ws://x/api/ws", "http://x", on_ring=lambda: None)
    with pytest.raises(TypeError):
        bell.set_ring(lambda frame: None)
    bell.set_ring(lambda: None)  # and still accepts the legal shape


# ── control frames are not rings ──


def test_a_ping_is_answered_and_does_not_ring():
    """A 30s server heartbeat must not become a 30s poll wearing a socket's clothes."""
    rings = []
    sock = FakeSocket(HANDSHAKE_OK + server_frame(OP_PING) + server_frame(OP_CLOSE))
    bell = Doorbell(
        "ws://x/api/ws",
        "http://x",
        on_ring=lambda: rings.append(1),
        connect=lambda _u: sock,
        sleep=lambda _s: None,
    )
    bell.run_forever(_StopAfter(1))
    assert rings == [], "a PING is transport bookkeeping, not a state change"
    # A PONG went back, masked — otherwise the gateway drops us at the next heartbeat.
    pong = bytes(sock.sent)[-6:]
    assert pong[0] == 0x8A, "0x80|OP_PONG"
    assert pong[1] & 0x80, "client frames must be masked"


# ── ONE connection ──


def test_one_socket_for_the_process_and_refreshes_do_not_open_more():
    opener = _opener()
    model = _model(opener)
    frames = server_frame(OP_TEXT, b"{}") * 3
    sock = FakeSocket(HANDSHAKE_OK + frames + server_frame(OP_CLOSE))
    bell = Doorbell(
        "ws://x/api/ws",
        "http://x",
        on_ring=lambda: model.refresh(),
        connect=lambda _u: sock,
        sleep=lambda _s: None,
    )
    bell.run_forever(_StopAfter(1))
    assert bell.connect_count == 1
    assert bell.rings == 3

    # Five more refreshes — the thing a menu open or a floor poll does. Still one socket.
    for _ in range(5):
        model.refresh()
    assert bell.connect_count == 1
    assert len(opener.calls) == (3 + 5) * 2, "every refresh is TWO HTTP GETs, not a socket read"


def test_build_companion_constructs_exactly_one_doorbell(monkeypatch):
    """The rail against 'one connection per view': count the constructions."""
    import menubar_companion.app as app_mod
    from menubar_companion.settings import Settings

    built = []
    real = app_mod.Doorbell

    class Counting(real):  # type: ignore[misc, valid-type]
        def __init__(self, *a, **kw):
            built.append(1)
            super().__init__(*a, **kw)

    monkeypatch.setattr(app_mod, "Doorbell", Counting)
    companion = app_mod.build_companion(
        Settings(url="http://127.0.0.1:10000", token="tok"),
        opener=_opener(),
        runner=lambda _argv: None,
    )
    # Drive the surfaces a user touches: refresh, menu render, mute toggle.
    companion.refresh_and_notify()
    companion.refresh_and_notify()
    companion.toggle_mute()
    assert built == [1], f"expected one Doorbell for the whole app, built {len(built)}"


# ── backoff that actually grows ──


def test_backoff_grows_reconnects_and_resets_after_a_good_connect():
    slept: list[float] = []
    attempts = {"n": 0}
    good = FakeSocket(HANDSHAKE_OK + server_frame(OP_TEXT, b"{}") + server_frame(OP_CLOSE))

    def connect(_url):
        attempts["n"] += 1
        if attempts["n"] <= 5:
            raise OSError("connection refused")
        return good

    bell = Doorbell(
        "ws://x/api/ws",
        "http://x",
        on_ring=lambda: None,
        connect=connect,
        sleep=slept.append,
    )
    # Stop once six delays have elapsed: five failures plus the one after the good
    # session's close. Keying the stop on the SLEEPS (not on a call count) keeps it
    # correct however many times ``run_forever`` polls the predicate per iteration.
    bell.run_forever(lambda: len(slept) >= 6)

    assert attempts["n"] == 6, "it kept reconnecting rather than giving up"
    assert bell.connect_count == 1, "only the successful attempt opened a socket"
    assert slept[:5] == [1.0, 2.0, 4.0, 8.0, 16.0], slept
    # VACUITY: a fixed sleep would collapse to one distinct value. Five distinct,
    # strictly increasing values is the ladder, not a skeleton.
    assert len(set(slept[:5])) == 5
    assert all(b > a for a, b in zip(slept[:5], slept[1:5], strict=False))
    # A good connect resets the ladder: the sleep after the close is the base again.
    assert slept[5] == 1.0, slept


def test_backoff_is_capped_and_monotonic():
    ladder = [backoff_delay(i) for i in range(12)]
    assert ladder[0] == 1.0
    assert all(b >= a for a, b in zip(ladder, ladder[1:], strict=False))
    assert max(ladder) == BACKOFF_CAP
    assert ladder[-1] == BACKOFF_CAP


class _StopAfter:
    """``should_stop`` that returns False *n* times, then True."""

    def __init__(self, n: int):
        self.n = n

    def __call__(self) -> bool:
        if self.n <= 0:
            return True
        self.n -= 1
        return False
