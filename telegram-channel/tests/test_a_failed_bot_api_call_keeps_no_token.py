"""A Bot API call that fails keeps nothing that holds the bot token.

Every Bot API call goes to ``https://api.telegram.org/bot<token>/<method>``, so the HTTP client's
own error holds the token: in its request's URL, and often in its text. ``_call`` used to raise its
network error ``from`` that error, with the error's text in its message, and let the client's other
request errors escape as they were. Any logger or crash reporter that walks an exception's cause or
context could then write the token. Now every failure is said in words of its own (the method, the
status, the client error's type), with no cause and no context.
"""

from __future__ import annotations

import httpx
import pytest

from telegram_runtime.api import API_BASE, HTTPTelegramAPI, TelegramAPIError

TOKEN = "123456:SECRET-bot-token"


async def _no_sleep(_seconds: float) -> None:
    return None


def _api(handler) -> HTTPTelegramAPI:
    return HTTPTelegramAPI(TOKEN, sleep=_no_sleep, client=httpx.AsyncClient(
        base_url=f"{API_BASE}/bot{TOKEN}", transport=httpx.MockTransport(handler)))


def _holds_the_token(exc: BaseException) -> list[str]:
    """Every place in *exc*'s chain the token shows: a message, or a request's URL or headers."""
    found, seen = [], set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if TOKEN in str(exc):
            found.append(f"{type(exc).__name__}'s text")
        request = getattr(exc, "_request", None) or getattr(exc, "request", None) if isinstance(
            exc, httpx.RequestError) else None
        if request is not None and TOKEN in str(request.url):
            found.append(f"{type(exc).__name__}'s request")
        exc = exc.__cause__ or exc.__context__
    return found


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError, httpx.ReadTimeout, httpx.TooManyRedirects, httpx.DecodingError],
)
@pytest.mark.asyncio
async def test_a_call_the_client_cant_make_keeps_no_token(error):
    """🔴 Red before: a network error kept the client's error as its cause and quoted its text,
    and a request error that isn't a network one escaped as the client's own."""
    def refuse(request: httpx.Request) -> httpx.Response:
        raise error(f"cannot reach {request.url}", request=request)

    with pytest.raises(TelegramAPIError) as raised:
        await _api(refuse).get_me()
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    assert _holds_the_token(raised.value) == []
    assert error.__name__ in str(raised.value)


@pytest.mark.asyncio
async def test_an_answer_that_isnt_json_keeps_no_cause():
    """🔴 Red before: it kept the JSON decoder's error as its cause."""
    def garbled(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not the Bot API</html>")

    with pytest.raises(TelegramAPIError, match="non-JSON response") as raised:
        await _api(garbled).get_me()
    assert raised.value.__cause__ is None and raised.value.__context__ is None


@pytest.mark.asyncio
async def test_a_network_error_is_still_retried_before_it_is_said(monkeypatch):
    """The control: a transport error is retried as before, then said."""
    calls = []

    def refuse(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(TelegramAPIError, match="network error after 3 retries"):
        await _api(refuse).get_me()
    assert len(calls) == 4
