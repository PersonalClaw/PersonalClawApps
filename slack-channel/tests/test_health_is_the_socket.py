"""The Slack status says whether Socket Mode is connected now, not whether it connected once.

``start_inbound`` set ``_inbound_started`` when Socket Mode first connected, and ``health()``
answered ``ready — Tokens configured, Socket-Mode connected`` from that flag for the life of the
process. The Slack SDK reconnects on its own (``monitor_current_session``) and only logs a failure,
so a socket Slack dropped and would not take back (``apps.connections.open`` refusing a revoked
App Token with ``invalid_auth``) still read connected. Only Test (``auth.test`` on the bot token)
could say anything, and it tests the other token.

The socket client here is the app's real one over a stand-in Web API; nothing opens a socket.
"""

from __future__ import annotations

import pytest
from slack_sdk.errors import SlackApiError

from slack_runtime.transport import SlackTransport

TOKENS = ("fake-bot-token-saved-bot-token", "fake-app-token-1")


class _Socket:
    """The runtime's socket client as the transport reads it."""

    def __init__(self, connected: bool, connect_error: str = "") -> None:
        self._connected = connected
        self.connect_error = connect_error
        self.ping_interval = 10

    async def is_connected(self) -> bool:
        return self._connected


def _started(socket: _Socket) -> SlackTransport:
    """A transport whose Socket Mode connected on ``TOKENS``, holding ``socket``."""
    transport = SlackTransport({"bot_token": TOKENS[0], "app_token": TOKENS[1]})
    transport._inbound_tokens = TOKENS
    transport._inbound_started = True
    transport._runtime = type("_Runtime", (), {"_socket_client": socket, "slack": None})()
    return transport


@pytest.mark.asyncio
async def test_a_socket_slack_dropped_is_not_ready():
    health = await _started(_Socket(connected=False)).health()

    assert health["state"] == "error", health
    assert "Socket Mode is not connected" in health["detail"], health
    assert "every 10 seconds" in health["detail"], health


@pytest.mark.asyncio
async def test_an_app_token_slack_refuses_on_reconnect_says_so():
    health = await _started(_Socket(connected=False, connect_error="invalid_auth")).health()

    assert health["state"] == "error", health
    assert "Slack refuses the App Token (invalid_auth)" in health["detail"], health
    assert "Configure" in health["detail"], health
    assert TOKENS[1] not in health["detail"], health


@pytest.mark.asyncio
async def test_other_reconnect_failures_are_named_and_retried():
    health = await _started(_Socket(connected=False, connect_error="InvalidStatus")).health()

    assert health["state"] == "error", health
    assert "InvalidStatus" in health["detail"] and "every 10 seconds" in health["detail"], health


@pytest.mark.asyncio
async def test_a_connected_socket_reads_ready():
    """The guard: connected is ready, as before."""
    health = await _started(_Socket(connected=True)).health()

    assert health == {"state": "ready", "detail": "Tokens configured, Socket-Mode connected"}


@pytest.mark.asyncio
async def test_newer_tokens_over_a_dropped_socket_do_not_claim_it_is_connected():
    transport = _started(_Socket(connected=False))
    transport._inbound_tokens = ("fake-bot-token-older-bot-token", TOKENS[1])

    health = await transport.health()

    assert health["state"] == "error", health
    assert "still connected" not in health["detail"], health


class _WebClient:
    """The Web API as Socket Mode uses it: ``apps.connections.open`` refusing the App Token."""

    async def apps_connections_open(self, app_token: str):
        raise SlackApiError(
            "The request to the Slack API failed.", response={"ok": False, "error": "invalid_auth"}
        )


@pytest.mark.asyncio
async def test_the_socket_client_keeps_why_it_could_not_connect():
    from slack_runtime.events import SocketModeReceiver

    client = SocketModeReceiver(app_token=TOKENS[1], web_client=_WebClient())
    try:
        with pytest.raises(SlackApiError):
            await client.connect_to_new_endpoint(force=True)

        assert client.connect_error == "invalid_auth"
        assert await client.is_connected() is False
    finally:
        await client.close()
