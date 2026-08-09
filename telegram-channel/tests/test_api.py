"""HTTPTelegramAPI over an httpx.MockTransport — every wrapper + the 429 backoff.

No live Telegram, no wall-clock wait: the httpx client is backed by a
``MockTransport`` that returns canned Bot API envelopes, and the retry ``sleep`` is
a no-op that just records the durations it was asked to wait. That lets the
``429 retry_after`` / 5xx backoff logic be exercised end-to-end deterministically."""

from __future__ import annotations

import json

import httpx
import pytest

from telegram_runtime.api import (
    DEFAULT_POLL_TIMEOUT,
    MAX_RETRY_AFTER,
    HTTPTelegramAPI,
    TelegramAPIError,
)


def _ok(result):
    return httpx.Response(200, json={"ok": True, "result": result})


class _Recorder:
    """Captures every request the client makes + serves scripted responses."""

    def __init__(self, responder):
        self.requests: list[httpx.Request] = []
        self._responder = responder

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responder(request)

    def method(self, i: int = -1) -> str:
        return self.requests[i].url.path.rsplit("/", 1)[-1]

    def payload(self, i: int = -1) -> dict:
        req = self.requests[i]
        if req.content:
            try:
                return json.loads(req.content)
            except ValueError:
                return {}
        return {}


def _api(responder, *, max_retries: int = 3):
    slept: list[float] = []

    async def _sleep(secs: float) -> None:
        slept.append(secs)

    rec = _Recorder(responder)
    client = httpx.AsyncClient(
        base_url="https://api.telegram.org/botTEST",
        transport=httpx.MockTransport(rec.handler),
    )
    api = HTTPTelegramAPI("TEST", client=client, max_retries=max_retries, sleep=_sleep)
    return api, rec, slept


class TestWrappers:
    @pytest.mark.asyncio
    async def test_get_me(self):
        api, rec, _ = _api(lambda r: _ok({"id": 1, "username": "mybot"}))
        me = await api.get_me()
        assert me["username"] == "mybot"
        assert rec.method() == "getMe"
        await api.close()

    @pytest.mark.asyncio
    async def test_get_updates_maps_args_and_returns_list(self):
        api, rec, _ = _api(lambda r: _ok([{"update_id": 5}, {"update_id": 6}]))
        updates = await api.get_updates(offset=5, timeout=50, allowed_updates=["message"])
        assert [u["update_id"] for u in updates] == [5, 6]
        p = rec.payload()
        assert p["offset"] == 5 and p["timeout"] == 50 and p["allowed_updates"] == ["message"]
        await api.close()

    @pytest.mark.asyncio
    async def test_get_updates_omits_zero_offset(self):
        api, rec, _ = _api(lambda r: _ok([]))
        await api.get_updates(offset=0)
        assert "offset" not in rec.payload()
        await api.close()

    @pytest.mark.asyncio
    async def test_send_message_strips_none_and_returns_message(self):
        api, rec, _ = _api(lambda r: _ok({"message_id": 42}))
        msg = await api.send_message("123", "hi", parse_mode="MarkdownV2")
        assert msg["message_id"] == 42
        p = rec.payload()
        assert p == {"chat_id": "123", "text": "hi", "parse_mode": "MarkdownV2"}
        assert "reply_markup" not in p  # None stripped
        await api.close()

    @pytest.mark.asyncio
    async def test_send_message_keeps_reply_markup(self):
        api, rec, _ = _api(lambda r: _ok({"message_id": 1}))
        markup = {"inline_keyboard": [[{"text": "x", "callback_data": "y"}]]}
        await api.send_message("123", "t", reply_markup=markup)
        assert rec.payload()["reply_markup"] == markup
        await api.close()

    @pytest.mark.asyncio
    async def test_edit_message_text(self):
        api, rec, _ = _api(lambda r: _ok({"message_id": 7}))
        await api.edit_message_text("123", 7, "new", parse_mode="MarkdownV2")
        p = rec.payload()
        assert p["message_id"] == 7 and p["text"] == "new"
        assert rec.method() == "editMessageText"
        await api.close()

    @pytest.mark.asyncio
    async def test_send_document(self, tmp_path):
        f = tmp_path / "report.txt"
        f.write_text("data")
        api, rec, _ = _api(lambda r: _ok({"message_id": 9}))
        msg = await api.send_document("123", str(f), caption="cap")
        assert msg["message_id"] == 9
        assert rec.method() == "sendDocument"
        # multipart upload: chat_id + caption ride as form fields
        body = rec.requests[-1].content
        assert b"report.txt" in body and b"123" in body
        await api.close()

    @pytest.mark.asyncio
    async def test_send_photo(self, tmp_path):
        f = tmp_path / "pic.png"
        f.write_bytes(b"\x89PNG")
        api, rec, _ = _api(lambda r: _ok({"message_id": 11}))
        msg = await api.send_photo("123", str(f))
        assert msg["message_id"] == 11
        assert rec.method() == "sendPhoto"
        await api.close()

    @pytest.mark.asyncio
    async def test_answer_callback_query(self):
        api, rec, _ = _api(lambda r: _ok(True))
        assert await api.answer_callback_query("cbid", text="Recorded") is True
        p = rec.payload()
        assert p["callback_query_id"] == "cbid" and p["text"] == "Recorded"
        assert "show_alert" not in p  # False → None → stripped
        assert rec.method() == "answerCallbackQuery"
        await api.close()


class TestRetryAfter:
    @pytest.mark.asyncio
    async def test_429_body_retry_after_then_success(self):
        calls = {"n": 0}

        def responder(r):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(
                    429, json={"ok": False, "error_code": 429, "description": "Too Many Requests",
                               "parameters": {"retry_after": 3}},
                )
            return _ok({"message_id": 1})

        api, rec, slept = _api(responder)
        msg = await api.send_message("123", "hi")
        assert msg["message_id"] == 1
        assert calls["n"] == 2  # retried once
        assert slept == [3.0]  # honored the body's retry_after
        await api.close()

    @pytest.mark.asyncio
    async def test_429_header_retry_after_fallback(self):
        calls = {"n": 0}

        def responder(r):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "2"}, json={"ok": False})
            return _ok({"message_id": 2})

        api, rec, slept = _api(responder)
        await api.send_message("123", "hi")
        assert slept == [2.0]
        await api.close()

    @pytest.mark.asyncio
    async def test_retry_after_capped(self):
        def responder(r):
            return httpx.Response(
                429, json={"ok": False, "parameters": {"retry_after": 9999}},
            )

        api, rec, slept = _api(responder, max_retries=1)
        with pytest.raises(TelegramAPIError) as exc:
            await api.send_message("123", "hi")
        assert exc.value.error_code == 429
        assert all(s <= MAX_RETRY_AFTER for s in slept)
        await api.close()

    @pytest.mark.asyncio
    async def test_429_exhausts_retries_raises(self):
        def responder(r):
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 1}})

        api, rec, slept = _api(responder, max_retries=2)
        with pytest.raises(TelegramAPIError) as exc:
            await api.send_message("123", "hi")
        assert exc.value.error_code == 429
        assert len(rec.requests) == 3  # initial + 2 retries
        await api.close()

    @pytest.mark.asyncio
    async def test_5xx_backoff_then_success(self):
        calls = {"n": 0}

        def responder(r):
            calls["n"] += 1
            if calls["n"] <= 2:
                return httpx.Response(503, text="Service Unavailable")
            return _ok({"message_id": 3})

        api, rec, slept = _api(responder)
        msg = await api.send_message("123", "hi")
        assert msg["message_id"] == 3
        assert len(slept) == 2  # backed off twice before the 3rd try
        await api.close()


class TestErrorMapping:
    @pytest.mark.asyncio
    async def test_4xx_non_429_raises_immediately(self):
        def responder(r):
            return httpx.Response(
                400, json={"ok": False, "error_code": 400, "description": "Bad Request: chat not found"},
            )

        api, rec, slept = _api(responder)
        with pytest.raises(TelegramAPIError) as exc:
            await api.send_message("999", "hi")
        assert exc.value.error_code == 400
        assert "chat not found" in exc.value.description
        assert len(rec.requests) == 1  # no retry on a caller error
        assert slept == []
        await api.close()

    @pytest.mark.asyncio
    async def test_ok_false_body_raises(self):
        def responder(r):
            return httpx.Response(200, json={"ok": False, "error_code": 403, "description": "Forbidden"})

        api, _, _ = _api(responder)
        with pytest.raises(TelegramAPIError) as exc:
            await api.get_me()
        assert exc.value.error_code == 403
        await api.close()

    @pytest.mark.asyncio
    async def test_401_surfaces_error_code(self):
        """The poll loop keys off error_code == 401 to go offline on a bad token."""
        def responder(r):
            return httpx.Response(401, json={"ok": False, "error_code": 401, "description": "Unauthorized"})

        api, _, _ = _api(responder)
        with pytest.raises(TelegramAPIError) as exc:
            await api.get_me()
        assert exc.value.error_code == 401
        await api.close()

    def test_default_poll_timeout_constant(self):
        assert DEFAULT_POLL_TIMEOUT == 50
