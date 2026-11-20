"""A Discord REST call that fails keeps nothing that holds the bot token.

Every REST call carries ``Authorization: Bot <token>``, so the HTTP client's own error holds the
token in its request's headers. ``_request`` used to raise its network error ``from`` that error,
quoting its text, and let the client's other request errors escape as they were, so anything that
walks an exception's cause or context could read the token. Now every failure is said in words of
its own, with no cause and no context.
"""

from __future__ import annotations

import httpx
import pytest

from discord_runtime.api import API_BASE, DiscordAPIError, HTTPDiscordAPI

TOKEN = "SECRET.discord.bot-token"


async def _no_sleep(_seconds: float) -> None:
    return None


def _api(handler) -> HTTPDiscordAPI:
    return HTTPDiscordAPI(TOKEN, sleep=_no_sleep, client=httpx.AsyncClient(
        base_url=API_BASE, headers=HTTPDiscordAPI.auth_headers(TOKEN),
        transport=httpx.MockTransport(handler)))


def _holds_the_token(exc: BaseException) -> list[str]:
    """Every place in *exc*'s chain the token shows: a message, or a request's headers."""
    found, seen = [], set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if TOKEN in str(exc):
            found.append(f"{type(exc).__name__}'s text")
        if isinstance(exc, httpx.RequestError):
            try:
                headers = str(dict(exc.request.headers))
            except RuntimeError:
                headers = ""
            if TOKEN in headers:
                found.append(f"{type(exc).__name__}'s request")
        exc = exc.__cause__ or exc.__context__
    return found


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError, httpx.ReadTimeout, httpx.TooManyRedirects, httpx.DecodingError],
)
@pytest.mark.asyncio
async def test_a_call_the_client_cant_make_keeps_no_token(error):
    """🔴 Red before: a network error kept the client's error, with the token in its request's
    headers, as its cause, and a request error that isn't a network one escaped as it was."""
    def refuse(request: httpx.Request) -> httpx.Response:
        raise error(f"cannot reach {request.url}", request=request)

    with pytest.raises(DiscordAPIError) as raised:
        await _api(refuse).get_user("1")
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    assert _holds_the_token(raised.value) == []
    assert error.__name__ in str(raised.value)


@pytest.mark.asyncio
async def test_an_answer_that_isnt_json_keeps_no_cause():
    """🔴 Red before: it kept the JSON decoder's error as its cause."""
    def garbled(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not Discord</html>")

    with pytest.raises(DiscordAPIError, match="non-JSON response") as raised:
        await _api(garbled).get_user("1")
    assert raised.value.__cause__ is None and raised.value.__context__ is None


@pytest.mark.asyncio
async def test_a_network_error_is_still_retried_before_it_is_said():
    """The control: a transport error is retried as before, then said."""
    calls = []

    def refuse(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(DiscordAPIError, match="network error after 3 retries"):
        await _api(refuse).get_user("1")
    assert len(calls) == 4


INTERACTION_TOKEN = "aW50ZXJhY3Rpb246MTIzOnNlY3JldA-interaction-token"


@pytest.mark.parametrize("answer", ["unknown", "unreachable"])
@pytest.mark.asyncio
async def test_a_failed_interaction_callback_keeps_no_interaction_token(answer):
    """🔴 Red before: the callback's path holds the interaction's token, which can post and edit
    that interaction's follow-ups as the bot, and the route every error starts with quoted the
    path. So an expired interaction's 404, or a network error, said the token."""
    def respond(request: httpx.Request) -> httpx.Response:
        if answer == "unreachable":
            raise httpx.ConnectError(f"cannot reach {request.url}", request=request)
        return httpx.Response(404, json={"message": "Unknown interaction", "code": 10062})

    api = _api(respond)
    with pytest.raises(DiscordAPIError) as raised:
        await api.create_interaction_response("123", INTERACTION_TOKEN)
    assert INTERACTION_TOKEN not in str(raised.value)
    assert INTERACTION_TOKEN not in raised.value.route
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    assert raised.value.route == "POST /interactions/{id}/{token}/callback"
    kept = " ".join([*api._routes, *api._routes.values(), *api._buckets])
    assert INTERACTION_TOKEN not in kept
