"""The Telegram status says what the long-poll receiver is doing, not only that a token is saved.

A 401 from ``getUpdates`` ended the poll loop (``_poll_loop`` logged "inbound offline" and
returned), and ``health()`` never looked at the loop: with the receiver started on the token that
is still saved, it answered ``ready / "Bot token configured"`` over a receiver that had stopped.
Only Test showed the problem. A loop that kept failing and retrying (a 409 while another poller
holds the token, Telegram unreachable) read ``ready`` the same way.

The receiver runs for real here: ``start_inbound`` builds its client through
``HTTPTelegramAPI``, which is replaced by a scripted Bot API, so nothing opens a socket.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio

import telegram_runtime.transport as transport_mod
from telegram_runtime.api import TelegramAPI, TelegramAPIError
from telegram_runtime.transport import TelegramTransport

TOKEN = "123456:saved-token-value"


class _ScriptedAPI(TelegramAPI):
    """A Bot API whose ``getUpdates`` answers from a script: an exception to raise, or updates."""

    def __init__(self, answers: list) -> None:
        self.answers = answers
        self.polls = 0
        self.polled = asyncio.Event()

    async def get_me(self):
        return {"username": "claw_bot"}

    async def get_updates(self, offset=0, timeout=50, allowed_updates=None):
        self.polls += 1
        self.polled.set()
        answer = self.answers[min(self.polls, len(self.answers)) - 1]
        if isinstance(answer, BaseException):
            raise answer
        if answer == "hang":
            await asyncio.Event().wait()  # a long-poll still waiting on Telegram
        return list(answer)

    async def send_message(self, *a, **k):
        return {"message_id": 1}

    async def edit_message_text(self, *a, **k):
        return {}

    async def send_document(self, *a, **k):
        return {}

    async def send_photo(self, *a, **k):
        return {}

    async def answer_callback_query(self, *a, **k):
        return True


@pytest_asyncio.fixture
async def started(monkeypatch):
    """Start a transport's real receiver on ``TOKEN`` against a scripted Bot API."""
    transports: list[TelegramTransport] = []

    async def _start(answers: list) -> tuple[TelegramTransport, _ScriptedAPI]:
        api = _ScriptedAPI(answers)
        monkeypatch.setattr(transport_mod, "HTTPTelegramAPI", lambda token, **_kw: api)
        transport = TelegramTransport({"bot_token": TOKEN})
        transports.append(transport)
        await transport.start_inbound(SimpleNamespace(dashboard_state=None))
        return transport, api

    yield _start
    for transport in transports:
        await transport.stop_inbound()


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_token_telegram_rejects_stops_the_receiver_and_the_status_says_so(started):
    transport, api = await started(
        [TelegramAPIError("Unauthorized", error_code=401, method="getUpdates")]
    )
    await asyncio.wait_for(transport._poll_task, timeout=1)  # the loop gave up on the 401

    health = await transport.health()

    assert health["state"] == "error", health
    assert "Telegram rejected the bot token (401 Unauthorized)" in health["detail"], health
    assert "stopped" in health["detail"], health
    assert "Configure" in health["detail"], health
    assert TOKEN not in health["detail"] and "saved-token" not in health["detail"], health
    assert api.polls == 1, "the receiver kept polling on a token Telegram rejected"


@pytest.mark.asyncio
async def test_a_poll_that_keeps_failing_is_not_ready_and_says_it_retries(started):
    conflict = TelegramAPIError(
        "Conflict: terminated by other getUpdates request; make sure that only one bot "
        "instance is running",
        error_code=409,
        method="getUpdates",
    )
    transport, api = await started([conflict, "hang"])
    await asyncio.wait_for(api.polled.wait(), timeout=1)
    await _settle()

    health = await transport.health()

    assert health["state"] == "error", health
    assert "terminated by other getUpdates request" in health["detail"], health
    assert "409" in health["detail"], health
    assert "retrying" in health["detail"], health
    probe = await transport.test()
    assert probe["ok"] is False, probe
    assert probe["detail"].startswith("Authenticated as @claw_bot, but "), probe


@pytest.mark.asyncio
async def test_the_status_is_ready_again_once_a_poll_gets_through(started):
    """The guard for the other direction: a failure is reported until a poll succeeds, not for
    ever. Waits out the loop's real one-second back-off."""
    conflict = TelegramAPIError("Conflict", error_code=409, method="getUpdates")
    transport, api = await started([conflict, [], "hang"])
    for _ in range(40):
        if api.polls >= 3:
            break
        await asyncio.sleep(0.1)
    assert api.polls >= 3, "the receiver did not poll again after its back-off"

    assert (await transport.health()) == {"state": "ready", "detail": "Bot token configured"}


@pytest.mark.asyncio
async def test_telegram_unreachable_says_so_without_the_request(started):
    """A transport failure's text is the HTTP client's, and can carry the request URL, which
    carries the token. The status names what happened instead."""
    unreachable = TelegramAPIError(
        f"network error after 3 retries: https://api.telegram.org/bot{TOKEN}/getUpdates",
        method="getUpdates",
    )
    transport, api = await started([unreachable, "hang"])
    await asyncio.wait_for(api.polled.wait(), timeout=1)
    await _settle()

    health = await transport.health()

    assert health["state"] == "error", health
    assert "Telegram could not be reached" in health["detail"], health
    assert TOKEN not in health["detail"] and "api.telegram.org" not in health["detail"], health


@pytest.mark.asyncio
async def test_a_receiver_waiting_on_its_long_poll_is_ready(started):
    """The guard: a healthy receiver, its long-poll open, still reads ready."""
    transport, api = await started(["hang"])
    await asyncio.wait_for(api.polled.wait(), timeout=1)

    assert (await transport.health()) == {"state": "ready", "detail": "Bot token configured"}
    assert (await transport.test()) == {"ok": True, "detail": "Authenticated as @claw_bot"}
