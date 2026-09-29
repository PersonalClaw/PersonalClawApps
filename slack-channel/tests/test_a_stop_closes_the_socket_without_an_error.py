"""A stop closes the Slack socket in order, and the log does not call that an error.

The Slack SDK closes the socket first and cancels the task reading it after, so on every stop and
restart of the gateway that task woke to the orderly close (``sent 1000 (OK); then received 1000
(OK)``) and logged it as an ERROR: "Failed to receive or enqueue a message: ConnectionClosedOK".
The app's socket client now stops reading before it closes.

Driven over a real WebSocket on the loopback: the app's own socket client connects to a local
stand-in for Slack's Socket Mode endpoint, and the transport stops it the way the gateway does.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest
from slack_sdk.web.async_client import AsyncWebClient
from websockets.asyncio.server import ServerConnection, serve

from slack_runtime.events import SocketModeReceiver
from slack_runtime.transport import SlackTransport

TOKENS = ("fake-bot-token-saved-bot-token", "fake-app-token-1")


@pytest.mark.asyncio
async def test_a_stop_closes_the_socket_in_order_without_an_error(caplog):
    closed_with: list[int | None] = []

    async def _slack(ws: ServerConnection) -> None:
        await ws.send(json.dumps({"type": "hello", "num_connections": 1}))
        await ws.wait_closed()
        closed_with.append(ws.close_code)

    async with serve(_slack, "127.0.0.1", 0) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        socket = SocketModeReceiver(app_token=TOKENS[1], web_client=AsyncWebClient(token=TOKENS[0]))
        socket.wss_uri = f"ws://127.0.0.1:{port}"
        heard = asyncio.Event()

        async def _hello(client: Any, message: dict, raw: str | None) -> None:
            heard.set()

        socket.message_listeners.append(_hello)
        await socket.connect()
        # Reading, as a socket connected for hours is when the gateway stops.
        await asyncio.wait_for(heard.wait(), timeout=5)

        transport = SlackTransport({"bot_token": TOKENS[0], "app_token": TOKENS[1]})
        transport._runtime = type("_Runtime", (), {"_socket_client": socket, "slack": None})()
        caplog.set_level(logging.INFO)
        await transport.stop_inbound()
        await asyncio.sleep(0.2)  # a reader woken by the close would have logged by now

    assert closed_with == [1000], f"the socket was not closed in order: {closed_with}"
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors == [], f"an orderly close was logged as an error: {errors}"
    assert not await socket.is_connected()
