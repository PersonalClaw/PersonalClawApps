"""Every message this app sends to Discord notifies no one, whatever its text says.

Discord reads a message's content for mentions unless the message says which it allows:
``@everyone`` and ``@here`` notify a whole server or channel, ``<@id>`` a person and ``<@&id>`` a
role. A reply is a model's text, and a model may only be repeating something it read. Nothing this
app sends means to notify anyone, so each message, edit, upload and interaction answer allows no
mentions, and its text still shows as written.
"""

from __future__ import annotations

import json

import httpx
import pytest

from discord_runtime.api import API_BASE, INTERACTION_CALLBACK_MESSAGE, HTTPDiscordAPI
from discord_runtime.delivery import DiscordDelivery

_NO_MENTIONS = {"parse": []}


def _api() -> tuple[HTTPDiscordAPI, list[httpx.Request]]:
    """A real client over a recorded transport: what it hands Discord is what these tests read."""
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "9", "channel_id": "500"})

    client = httpx.AsyncClient(
        base_url=API_BASE, transport=httpx.MockTransport(answer),
        headers=HTTPDiscordAPI.auth_headers("TEST"),
    )

    async def _no_wait(_secs: float) -> None:
        return None

    return HTTPDiscordAPI("TEST", client=client, sleep=_no_wait), seen


def _body(request: httpx.Request) -> dict:
    if request.headers.get("content-type", "").startswith("multipart/"):
        part = request.content.split(b'name="payload_json"', 1)[1].split(b"\r\n\r\n", 1)[1]
        return json.loads(part.split(b"\r\n--", 1)[0])
    return json.loads(request.content)


@pytest.mark.asyncio
async def test_everyone_in_a_reply_does_not_ping():
    api, seen = _api()
    delivery = DiscordDelivery(api, owner=lambda: "42")
    await delivery.deliver_text("500", "Posted to @everyone and @here, cc <@42>.")
    (request,) = seen
    assert _body(request) == {
        "content": "Posted to @everyone and @here, cc <@42>.",
        "allowed_mentions": _NO_MENTIONS,
    }


@pytest.mark.asyncio
async def test_an_edit_an_upload_and_an_answer_allow_no_mentions(tmp_path):
    api, seen = _api()
    report = tmp_path / "report.txt"
    report.write_text("numbers")
    await api.edit_message("500", "9", "Now for @everyone")
    await api.upload_file("500", str(report), content="For @here")
    await api.create_interaction_response(
        "1", "tok", callback_type=INTERACTION_CALLBACK_MESSAGE, data={"content": "Told @everyone"},
    )
    edit, upload, answer = (_body(r) for r in seen)
    assert edit["allowed_mentions"] == upload["allowed_mentions"] == _NO_MENTIONS
    assert answer["data"] == {"content": "Told @everyone", "allowed_mentions": _NO_MENTIONS}
