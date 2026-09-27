"""The Discord status says what the gateway session is doing, and a refused session stops.

When Discord closed the connection, ``_recv`` returned ``None`` and dropped the close code, so
``run()`` saw an ordinary end and reconnected at once, with no back-off: a close Discord says not to
retry (4004 Authentication failed, and 4010-4014: invalid shard, sharding required, invalid API
version, invalid or disallowed intents) became a tight IDENTIFY loop Discord rate-limits, while
``health()`` answered ``ready``. A rejected token at ``GET /gateway/bot`` was swallowed the same way
("using the default gateway URL") and the session was opened with it anyway.

The socket is a scripted fake, as in ``test_gateway.py``, that closes with a real ``websockets``
close frame; ``connect`` and ``sleep`` are injected, so nothing opens a socket or waits.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

import discord_runtime.transport as transport_mod
from discord_runtime.api import DiscordAPIError
from discord_runtime.gateway import (
    OP_DISPATCH,
    OP_HEARTBEAT,
    OP_HEARTBEAT_ACK,
    OP_HELLO,
    OP_IDENTIFY,
    DiscordGateway,
)
from discord_runtime.transport import DiscordTransport

TOKEN = "MTIz.saved-token.value"
HELLO = {"op": OP_HELLO, "d": {"heartbeat_interval": 41250}}
READY = {
    "op": OP_DISPATCH, "t": "READY", "s": 1,
    "d": {"session_id": "sess-1", "resume_gateway_url": "wss://resume.discord.gg",
          "user": {"id": "bot-1"}},
}
RESUMED = {"op": OP_DISPATCH, "t": "RESUMED", "s": 2, "d": None}


class _WS:
    """A scripted gateway socket: its frames, then either a close frame (a close code), a silent
    end (``None``), or an open socket that waits (``"hang"``). It acks every heartbeat, as the
    gateway does, so a waiting socket is never mistaken for a zombie."""

    def __init__(self, frames: list[dict], then: int | str | None = None) -> None:
        self._queue: asyncio.Queue = asyncio.Queue()
        for frame in frames:
            self._queue.put_nowait(frame)
        self._then = then
        self.sent: list[dict] = []

    def push(self, frame: dict) -> None:
        self._queue.put_nowait(frame)

    async def recv(self):
        if self._queue.empty() and self._then != "hang":
            if isinstance(self._then, int):
                raise ConnectionClosedError(Close(self._then, "closed by the fake"), None)
            return None
        frame = await self._queue.get()
        return None if frame is None else json.dumps(frame)

    async def send(self, raw: str) -> None:
        frame = json.loads(raw)
        self.sent.append(frame)
        if frame.get("op") == OP_HEARTBEAT:  # a real gateway acks every beat
            self._queue.put_nowait({"op": OP_HEARTBEAT_ACK})

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self._queue.put_nowait(None)


class _Harness:
    """A real ``DiscordGateway`` over scripted connections, recording URLs and sleeps."""

    def __init__(self, connections: list[_WS]) -> None:
        self.connections = connections
        self.urls: list[str] = []
        self.slept: list[float] = []
        self.gw = DiscordGateway("TOK", connect=self._connect, sleep=self._sleep)

    async def _connect(self, url: str):
        self.urls.append(url)
        if not self.connections:
            self.gw._stopping = True  # nothing more scripted: end the loop
            return _WS([])
        return self.connections.pop(0)

    async def _sleep(self, secs: float) -> None:
        self.slept.append(secs)
        await asyncio.sleep(0)

    async def run(self) -> None:
        await asyncio.wait_for(self.gw.run(), timeout=2)


def _transport_on(gateway: DiscordGateway) -> DiscordTransport:
    """A transport whose receiver was started on ``TOKEN`` and runs ``gateway``."""
    transport = DiscordTransport({"bot_token": TOKEN})
    transport._inbound_token = TOKEN
    transport._gateway = gateway
    return transport


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [4004, 4010, 4011, 4012, 4013, 4014])
async def test_a_close_discord_says_not_to_retry_ends_the_loop(code):
    h = _Harness([_WS([HELLO], then=code), _WS([HELLO, READY], then="hang")])

    await h.run()

    assert len(h.urls) == 1, f"reconnected after close {code}, which Discord says not to retry"
    assert h.gw.fatal_close_code == code


def test_every_close_that_ends_the_loop_has_a_sentence():
    from discord_runtime.gateway import FATAL_CLOSE_CODES
    from discord_runtime.transport import _REFUSALS

    assert set(_REFUSALS) == set(FATAL_CLOSE_CODES)


@pytest.mark.asyncio
async def test_a_rejected_token_stops_the_session_and_the_status_says_so():
    h = _Harness([_WS([HELLO], then=4004)])
    await h.run()
    transport = _transport_on(h.gw)

    health = await transport.health()

    assert health["state"] == "error", health
    assert "Discord rejected the bot token (close code 4004" in health["detail"], health
    assert "stopped" in health["detail"] and "Configure" in health["detail"], health
    assert TOKEN not in health["detail"], health


@pytest.mark.asyncio
async def test_a_missing_privileged_intent_names_the_portal_switch():
    """4014 is what a new bot gets until Message Content Intent is ticked in the Developer
    Portal: the most likely first-run failure, and the status is where the owner looks."""
    h = _Harness([_WS([HELLO], then=4014)])
    await h.run()

    health = await _transport_on(h.gw).health()

    assert health["state"] == "error", health
    assert "close code 4014" in health["detail"], health
    assert "Message Content Intent" in health["detail"], health
    assert "Developer Portal" in health["detail"], health


@pytest.mark.asyncio
async def test_a_dropped_session_is_not_ready_until_it_is_back():
    """A drop Discord allows retrying is retried, and reads as reconnecting until RESUMED."""
    second = _WS([HELLO], then="hang")
    h = _Harness([_WS([HELLO, READY], then=4009), second])
    task = asyncio.ensure_future(h.gw.run())
    try:
        for _ in range(50):
            if len(h.urls) == 2 and second.sent:
                break
            await asyncio.sleep(0)
        transport = _transport_on(h.gw)

        down = await transport.health()
        assert down["state"] == "error", down
        assert "close code 4009" in down["detail"] and "reconnecting" in down["detail"], down

        second.push(RESUMED)
        for _ in range(20):
            await asyncio.sleep(0)
        assert (await transport.health()) == {"state": "ready", "detail": "Bot token configured"}
    finally:
        await h.gw.stop()
        task.cancel()


@pytest.mark.asyncio
async def test_a_connection_closed_before_its_session_backs_off():
    """Closed before READY or RESUMED is a failed attempt, so the next one waits. It used to
    reconnect at once, every time."""
    h = _Harness([_WS([HELLO], then=4008), _WS([HELLO], then=4008)])

    await h.run()

    backoffs = [s for s in h.slept if s < 10]  # the heartbeat's own sleeps are ~20 s and more
    assert backoffs[:2] == [1.0, 2.0], h.slept
    assert h.gw.fatal_close_code is None


@pytest.mark.asyncio
async def test_a_session_that_was_up_resumes_at_once():
    """The guard: a drop after READY reconnects immediately, to resume what it missed."""
    h = _Harness([_WS([HELLO, READY], then=4000), _WS([HELLO, RESUMED], then="hang")])
    task = asyncio.ensure_future(h.gw.run())
    try:
        for _ in range(50):
            if len(h.urls) == 2:
                break
            await asyncio.sleep(0)
        assert len(h.urls) == 2
        assert [s for s in h.slept if s < 10] == [], h.slept
    finally:
        await h.gw.stop()
        task.cancel()


class _RefusingAPI:
    """``HTTPDiscordAPI`` stand-in whose ``GET /gateway/bot`` refuses the token, as Discord does."""

    closed = 0

    def __init__(self, token: str, **_kw) -> None:
        self.token = token

    async def get_gateway_bot(self):
        raise DiscordAPIError("401: Unauthorized", status=401, route="GET /gateway/bot")

    async def close(self) -> None:
        _RefusingAPI.closed += 1


@pytest.mark.asyncio
async def test_a_token_refused_at_gateway_discovery_opens_no_session(monkeypatch):
    opened: list = []

    class _Gateway:
        def __init__(self, *a, **k):
            opened.append((a, k))

        async def run(self):
            await asyncio.sleep(3600)

        async def stop(self):
            pass

    monkeypatch.setattr(transport_mod, "HTTPDiscordAPI", _RefusingAPI)
    monkeypatch.setattr(transport_mod, "DiscordGateway", _Gateway)
    registered: list = []
    services = type("_S", (), {
        "dashboard_state": None,
        "register_channel_delivery": lambda self, d, provider="": registered.append(provider),
    })()
    transport = DiscordTransport({"bot_token": TOKEN})

    await transport.start_inbound(services)
    try:
        assert opened == [], "a gateway session was opened on a token Discord had refused"
        assert registered == [], "a delivery was registered on a token Discord had refused"
        health = await transport.health()
        assert health["state"] == "error", health
        assert "Discord rejected the bot token (401 Unauthorized)" in health["detail"], health
        assert TOKEN not in health["detail"], health
    finally:
        await transport.stop_inbound()


@pytest.mark.asyncio
async def test_a_session_that_identified_and_got_ready_reads_ready():
    """The guard: a healthy session still reads ready."""
    first = _WS([HELLO, READY], then="hang")
    h = _Harness([first])
    task = asyncio.ensure_future(h.gw.run())
    try:
        for _ in range(50):
            if any(f.get("op") == OP_IDENTIFY for f in first.sent) and h.gw.session_id:
                break
            await asyncio.sleep(0)
        assert (await _transport_on(h.gw).health()) == {
            "state": "ready", "detail": "Bot token configured",
        }
    finally:
        await h.gw.stop()
        task.cancel()
