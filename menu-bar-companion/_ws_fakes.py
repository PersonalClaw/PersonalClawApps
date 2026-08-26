"""Fake WebSocket transport for the doorbell tests: real bytes, no network.

The doorbell's own framing code runs against these bytes, so the tests measure the
shipped ``read_frame``/``handshake`` rather than a mock standing in for them.
"""

from __future__ import annotations

import struct

HANDSHAKE_OK = b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n"


def server_frame(opcode: int, payload: bytes = b"") -> bytes:
    """Build one UNMASKED server→client frame (what a real gateway sends)."""
    header = bytes([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header += bytes([length])
    elif length < (1 << 16):
        header += bytes([126]) + struct.pack("!H", length)
    else:
        header += bytes([127]) + struct.pack("!Q", length)
    return header + payload


class FakeSocket:
    """Replays a fixed byte script, records everything written, then reads as closed."""

    def __init__(self, script: bytes):
        self._buf = bytearray(script)
        self.sent = bytearray()
        self.closed = False

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def recv(self, count: int) -> bytes:
        if not self._buf:
            return b""  # peer closed
        chunk = bytes(self._buf[:count])
        del self._buf[:count]
        return chunk

    def close(self) -> None:
        self.closed = True


# ── HTTP ──


class _Response:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False


class FakeOpener:
    """Stands in for ``urllib.request.urlopen``, routing on the request's path.

    ``routes`` maps a path prefix to either bytes (the JSON body) or an exception to
    raise. Every call is recorded, so a test can assert that a refetch actually happened
    rather than inferring it from rendered output.
    """

    def __init__(self, routes: dict[str, object]):
        self.routes = routes
        self.calls: list[tuple[str, str]] = []

    def __call__(self, req, timeout=None):
        path = req.full_url.split("?", 1)[0]
        for prefix in sorted(self.routes, key=len, reverse=True):
            if prefix in path:
                self.calls.append((req.get_method(), path))
                outcome = self.routes[prefix]
                if isinstance(outcome, Exception):
                    raise outcome
                if callable(outcome):
                    return _Response(outcome())
                return _Response(outcome)  # type: ignore[arg-type]
        raise AssertionError(f"unrouted request: {req.get_method()} {path}")
