"""A slash command or a button press whose acknowledgement couldn't be sent still runs.

The Socket Mode listener acknowledges each request first. While the socket was reconnecting, that
acknowledgement failed, and the listener dropped the request with a debug line. Slack sends an
Events API envelope again until one is acknowledged, but never a slash command or a button press,
so a `/personalclaw` typed during a reconnect was lost without a word. Those two now run anyway
(they answer through their response_url or the Web API, which don't need the socket). An Events
API envelope is still left for Slack's redelivery, and each failure is logged as a warning.

Driven through the listener the real wiring attaches.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from slack_sdk.socket_mode.request import SocketModeRequest

import slack_runtime.events as events_mod
from slack_runtime.enterprise import VALIDATED, WorkspaceCheck


class _Socket:
    """``SocketModeClient`` stand-in: holds the listener the real wiring attaches, no network."""

    def __init__(self, **_kw) -> None:
        self.socket_mode_request_listeners: list = []


class _Reconnecting:
    """A socket that can't send the acknowledgement: it is reconnecting."""

    async def send_socket_mode_response(self, response) -> None:
        raise ConnectionError("WebSocket not ready")


@pytest.fixture
def listener(monkeypatch):
    """The listener, with what it hands each request to recorded."""
    monkeypatch.setattr(events_mod, "AsyncWebClient", lambda **kw: SimpleNamespace())
    monkeypatch.setattr(events_mod, "SocketModeReceiver", _Socket)
    ran: list = []

    async def _slash(orch, payload):
        ran.append(("slash_commands", payload))

    async def _interactive(payload):
        ran.append(("interactive", payload))

    monkeypatch.setattr(events_mod, "_handle_slash", _slash)
    monkeypatch.setattr(events_mod, "dispatch_interactive", _interactive)
    orch = SimpleNamespace(
        _bot_token="fake-bot-token-ack", _app_token="fake-app-token-ack", _handler_tasks=set()
    )
    events_mod.bind_workspace(orch, WorkspaceCheck(VALIDATED))
    (on_event,) = orch._socket_client.socket_mode_request_listeners
    return on_event, ran, orch


async def _deliver(on_event, orch, kind: str, payload: dict) -> None:
    await on_event(
        _Reconnecting(), SocketModeRequest(type=kind, envelope_id="env-1", payload=payload)
    )
    await asyncio.gather(*list(orch._handler_tasks))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("slash_commands", {"command": "/personalclaw", "text": "status"}),
        ("interactive", {"type": "block_actions", "actions": []}),
    ],
)
async def test_one_slack_never_sends_again_runs_although_its_ack_failed(
    listener, caplog, kind, payload
):
    """🔴 Red before: dropped, with a debug line."""
    on_event, ran, orch = listener
    with caplog.at_level(logging.WARNING, logger=events_mod.logger.name):
        await _deliver(on_event, orch, kind, payload)

    assert ran == [(kind, payload)]
    assert any("running it anyway" in r.getMessage() for r in caplog.records), caplog.text


@pytest.mark.asyncio
async def test_an_events_api_envelope_is_left_for_slacks_redelivery(listener, caplog):
    """🔴 Red before: dropped at debug, so the log never said so."""
    on_event, ran, orch = listener
    with caplog.at_level(logging.WARNING, logger=events_mod.logger.name):
        await _deliver(on_event, orch, "events_api", {"event": {"type": "message"}})

    assert ran == []
    assert any("Slack sends it again" in r.getMessage() for r in caplog.records), caplog.text
