"""ONE ``/api/ws`` connection, used as a doorbell and never as a data channel.

The rule this module exists to make structural: **a frame means "re-read over HTTP",
and its bytes are not state.** Three things enforce it, in order of strength.

1. :func:`read_frame` returns an ``int`` — the opcode. The payload is received (it has
   to be, to stay framed) and DISCARDED before the function returns. There is no
   return path a payload could travel on, so no caller can consume one by mistake.
2. The ring callback is invoked as ``on_ring()`` with zero arguments, and
   :class:`Doorbell` REFUSES at construction a callback that requires any. A
   payload-consuming design cannot be installed here, let alone reached.
3. The thing the callback actually calls — ``CompanionModel.refresh()`` — also takes
   no server-supplied argument.

Why hand-rolled instead of a WebSocket library: because the payload-blind frame reader
IS the guarantee, and a library that hands you ``message.data`` puts the payload one
attribute access away from being rendered. Reading frames is ~60 lines when you only
need "did something arrive"; a dependency would be larger and weaker.

The connection is also ONE connection. :class:`Doorbell` owns the socket for the
process lifetime and reconnects with a growing backoff; nothing else opens a socket,
and no view opens its own.
"""

from __future__ import annotations

import base64
import inspect
import os
import socket
import ssl
import struct
import time
import urllib.parse
from collections.abc import Callable

#: Opcodes we care about (RFC 6455 §5.2).
OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

#: Frames that mean "something happened" — i.e. ring the doorbell. A control frame is
#: transport bookkeeping and must NOT ring: a 30s server heartbeat ping would otherwise
#: become a 30s poll wearing a socket's clothes.
RINGING_OPCODES = frozenset({OP_CONTINUATION, OP_TEXT, OP_BINARY})

#: Backoff ladder: 1s, 2s, 4s … capped. It GROWS — a fixed retry delay against a
#: gateway that is down is just a tight loop with extra steps.
BACKOFF_BASE = 1.0
BACKOFF_FACTOR = 2.0
BACKOFF_CAP = 30.0

#: Hard ceiling on a single frame we will buffer while discarding it. The payload is
#: thrown away, so this only bounds how much we are willing to read to stay in sync.
MAX_FRAME_BYTES = 8 * 1024 * 1024


class DoorbellClosed(Exception):
    """The peer closed, or the socket died. Reconnect."""


def backoff_delay(attempt: int) -> float:
    """Seconds to wait before retry number *attempt* (0-based). Monotonic, capped."""
    if attempt <= 0:
        return BACKOFF_BASE
    return min(BACKOFF_CAP, BACKOFF_BASE * (BACKOFF_FACTOR**attempt))


def _recv_exact(sock, count: int) -> bytes:
    """Read exactly *count* bytes or raise. Framing has no tolerance for short reads."""
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise DoorbellClosed("socket closed mid-frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _mask(payload: bytes) -> bytes:
    key = os.urandom(4)
    masked = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return key + masked


def send_frame(sock, opcode: int, payload: bytes = b"") -> None:
    """Send one masked client frame. Only ever used for PONG and CLOSE."""
    header = bytes([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header += bytes([0x80 | length])
    elif length < (1 << 16):
        header += bytes([0x80 | 126]) + struct.pack("!H", length)
    else:
        header += bytes([0x80 | 127]) + struct.pack("!Q", length)
    sock.sendall(header + _mask(payload))


def read_frame(sock) -> int:
    """Read one frame and return ONLY its opcode.

    The payload is consumed and dropped on the floor. This signature is the load-bearing
    line of the whole app: a ``-> int`` cannot carry server state into the UI, so
    "refetch signal, never payload" is a property of the type rather than a habit of
    the caller. Do not change this to return bytes.
    """
    first, second = _recv_exact(sock, 2)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        (length,) = struct.unpack("!H", _recv_exact(sock, 2))
    elif length == 127:
        (length,) = struct.unpack("!Q", _recv_exact(sock, 8))
    if length > MAX_FRAME_BYTES:
        raise DoorbellClosed(f"frame too large to skip ({length} bytes)")
    if masked:
        _recv_exact(sock, 4)  # a server frame should not be masked; skip the key anyway
    if length:
        _recv_exact(sock, length)  # ← received, then dropped. Never returned.
    return opcode


def handshake(sock, url: str, origin: str) -> None:
    """Perform the RFC 6455 upgrade for *url* (``ws://``/``wss://`` with ``?token=``).

    ``Origin`` is sent explicitly. The gateway admits an origin in its allowlist, which
    contains the dashboard's own origin — the same header a browser on that URL sends.
    (An origin-LESS upgrade is admitted only for a paired device session, which a plain
    client app is not, so omitting the header would be a 403.)
    """
    parts = urllib.parse.urlsplit(url)
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        f"GET {target} HTTP/1.1\r\n"
        f"Host: {parts.netloc}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"Origin: {origin}\r\n"
        "\r\n"
    )
    sock.sendall(request.encode("ascii"))
    # Read just the response head, byte by byte: anything after the blank line is
    # already frame data and must not be swallowed by a buffered read.
    head = b""
    while b"\r\n\r\n" not in head:
        byte = sock.recv(1)
        if not byte:
            raise DoorbellClosed("gateway closed during handshake")
        head += byte
        if len(head) > 16384:
            raise DoorbellClosed("handshake response too large")
    status = head.split(b"\r\n", 1)[0].decode("latin-1")
    if " 101" not in status:
        raise DoorbellClosed(f"upgrade refused: {status}")


def tcp_connect(url: str, timeout: float = 10.0):
    """Open a TCP (and, for ``wss://``, TLS) connection to *url*'s host."""
    parts = urllib.parse.urlsplit(url)
    secure = parts.scheme == "wss"
    port = parts.port or (443 if secure else 80)
    host = parts.hostname or "127.0.0.1"
    sock = socket.create_connection((host, port), timeout=timeout)
    if secure:
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
    return sock


class Doorbell:
    """The single socket. Rings; carries nothing.

    ``on_ring`` is called with NO arguments. A callback that requires an argument is
    rejected here, at construction, because the only argument it could want is the
    payload this class exists not to hand out.
    """

    def __init__(
        self,
        url: str,
        origin: str,
        on_ring: Callable[[], None],
        *,
        connect: Callable[[str], object] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        on_state: Callable[[str], None] | None = None,
    ):
        _reject_payload_consuming(on_ring)
        self.url = url
        self.origin = origin
        self._on_ring = on_ring
        self._connect = connect or (lambda u: tcp_connect(u))
        self._sleep = sleep
        self._on_state = on_state or (lambda _state: None)
        #: How many times we have opened a socket. The "exactly one connection" rail
        #: reads this: it must be 1 no matter how many refreshes or menu opens happen.
        self.connect_count = 0
        #: Consecutive failed attempts. Reset by a successful connect, which is what
        #: makes the ladder a ladder and not a ratchet.
        self.attempt = 0
        self.rings = 0
        self.last_error = ""

    def set_ring(self, on_ring: Callable[[], None]) -> None:
        """Replace the ring callback, through the SAME zero-argument gate.

        Needed because the callback wants the object that owns this doorbell. Routing it
        through :func:`_reject_payload_consuming` again is the point: a setter that
        skipped the gate would be a hole straight through the guarantee.
        """
        _reject_payload_consuming(on_ring)
        self._on_ring = on_ring

    def set_state_callback(self, on_state: Callable[[str], None]) -> None:
        """Install the connected/disconnected reporter. Takes a STATE, never a frame."""
        self._on_state = on_state

    # ── one session ──

    def _session(self) -> None:
        sock = self._connect(self.url)
        self.connect_count += 1
        try:
            handshake(sock, self.url, self.origin)
            self.attempt = 0  # connected: the ladder starts over next time
            self._on_state("connected")
            while True:
                opcode = read_frame(sock)
                if opcode == OP_CLOSE:
                    raise DoorbellClosed("peer closed")
                if opcode == OP_PING:
                    # The gateway runs heartbeat=30; an unanswered ping gets us dropped.
                    send_frame(sock, OP_PONG)
                    continue
                if opcode == OP_PONG:
                    continue
                if opcode in RINGING_OPCODES:
                    self.rings += 1
                    self._on_ring()  # ← zero arguments. This is the whole contract.
        finally:
            try:
                sock.close()
            except Exception:  # noqa: BLE001 - closing a dead socket is not an error
                pass

    def run_forever(self, should_stop: Callable[[], bool] = lambda: False) -> None:
        """Hold the socket open for as long as the app runs, reconnecting with backoff."""
        while not should_stop():
            try:
                self._session()
            except Exception as exc:  # noqa: BLE001 - any failure is "reconnect"
                self.last_error = str(exc)
                self._on_state("disconnected")
            if should_stop():
                return
            delay = backoff_delay(self.attempt)
            self.attempt += 1
            self._sleep(delay)


def _reject_payload_consuming(on_ring: Callable[[], None]) -> None:
    """Refuse a ring callback that wants an argument.

    The only thing it could want is the frame, and the frame is not data here. Failing
    loudly at construction beats discovering at 3am that a menu is rendering whatever a
    socket said instead of what the gateway returns.
    """
    try:
        inspect.signature(on_ring).bind()
    except TypeError as exc:
        raise TypeError(
            "Doorbell.on_ring must be callable with no arguments — a WS frame is a "
            "refetch signal, not a payload. Re-read over HTTP inside the callback."
        ) from exc
