"""Telegram Bot API client — raw wire protocol over ``httpx`` (no vendor SDK).

The plan mandates the raw Bot API over ``httpx`` (already a core dependency), not a
third-party Telegram library. This module is the whole surface the transport +
delivery need: typed thin wrappers for ``getMe``, ``getUpdates``, ``sendMessage``,
``editMessageText``, ``sendDocument``, ``sendPhoto`` and ``answerCallbackQuery``,
with the one piece of real logic Telegram forces on every caller — the ``429 Too
Many Requests`` ``retry_after`` backoff.

An :class:`TelegramAPI` ABC lets the transport/delivery/tests swap a fake in
without touching the network; :class:`HTTPTelegramAPI` is the real
``httpx``-backed implementation. Its ``httpx`` client and sleep function are
injectable, so the retry/backoff logic is exercised end-to-end in tests with an
``httpx.MockTransport`` and a no-op sleep — no live Telegram, no wall-clock wait.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"

# Default long-poll timeout (seconds). getUpdates holds the connection open this
# long waiting for an update, so the client read timeout must exceed it.
DEFAULT_POLL_TIMEOUT = 50
# Ceiling on a server-suggested retry_after we will actually honor, so a
# pathological value can't wedge the poller.
MAX_RETRY_AFTER = 60


class TelegramAPIError(Exception):
    """A Bot API call returned ``ok: false`` or exhausted its retries.

    ``error_code`` mirrors Telegram's numeric code when present (e.g. 400/401/429);
    ``description`` is the human-readable reason Telegram returned."""

    def __init__(self, description: str, *, error_code: int = 0, method: str = "") -> None:
        self.description = description
        self.error_code = error_code
        self.method = method
        super().__init__(f"{method or 'telegram'}: {description} (code={error_code})")


class TelegramAPI(ABC):
    """The Bot API surface the Telegram channel needs. Swap a fake in for tests."""

    @abstractmethod
    async def get_me(self) -> dict[str, Any]:
        """``getMe`` — verify the token; returns the bot ``User`` (id/username/…)."""

    @abstractmethod
    async def get_updates(
        self, offset: int = 0, timeout: int = DEFAULT_POLL_TIMEOUT,
        allowed_updates: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """``getUpdates`` long-poll — returns the list of pending ``Update`` objects."""

    @abstractmethod
    async def send_message(
        self, chat_id: int | str, text: str, *,
        parse_mode: str | None = None, reply_to_message_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
        disable_web_page_preview: bool | None = None,
    ) -> dict[str, Any]:
        """``sendMessage`` — returns the sent ``Message`` (carries ``message_id``)."""

    @abstractmethod
    async def edit_message_text(
        self, chat_id: int | str, message_id: int, text: str, *,
        parse_mode: str | None = None, reply_markup: dict[str, Any] | None = None,
        disable_web_page_preview: bool | None = None,
    ) -> dict[str, Any]:
        """``editMessageText`` — edit an already-sent message (used for streaming)."""

    @abstractmethod
    async def send_document(
        self, chat_id: int | str, file_path: str, *,
        caption: str | None = None, reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        """``sendDocument`` — upload a file as a document attachment."""

    @abstractmethod
    async def send_photo(
        self, chat_id: int | str, file_path: str, *,
        caption: str | None = None, reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        """``sendPhoto`` — upload an image; Telegram renders it inline."""

    @abstractmethod
    async def answer_callback_query(
        self, callback_query_id: str, *, text: str | None = None, show_alert: bool = False,
    ) -> bool:
        """``answerCallbackQuery`` — acknowledge an inline-keyboard button press."""

    async def close(self) -> None:
        """Release any held resources (default: nothing)."""
        return None


class HTTPTelegramAPI(TelegramAPI):
    """``httpx``-backed Bot API client with 429 ``retry_after`` + 5xx backoff."""

    def __init__(
        self,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
        max_retries: int = 3,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._token = token
        # A read timeout comfortably above the long-poll timeout so getUpdates can
        # hold the connection open without the client aborting it first.
        self._client = client or httpx.AsyncClient(
            base_url=f"{API_BASE}/bot{token}",
            timeout=httpx.Timeout(DEFAULT_POLL_TIMEOUT + 15, connect=10.0),
        )
        self._owns_client = client is None
        self._max_retries = max_retries
        self._sleep = sleep or asyncio.sleep

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _call(
        self,
        method: str,
        *,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        read_timeout: float | None = None,
    ) -> Any:
        """POST ``method`` and return its ``result``, honoring 429/5xx backoff.

        Telegram signals rate limits with HTTP 429 and a ``parameters.retry_after``
        seconds hint (also mirrored in the ``Retry-After`` header); we sleep that
        long and retry. Transient 5xx get an exponential backoff. A 4xx other than
        429 is a caller error and raises immediately — retrying would just repeat
        it. Bodies with ``ok: false`` raise :class:`TelegramAPIError`."""
        # Strip Nones so we send only what the caller set (Telegram rejects nulls).
        payload = {k: v for k, v in (data or {}).items() if v is not None}
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = await self._client.post(
                    f"/{method}",
                    data=payload if files else None,
                    json=payload if not files else None,
                    files=files,
                    timeout=read_timeout,
                )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt > self._max_retries:
                    raise TelegramAPIError(
                        f"network error after {self._max_retries} retries: {exc}",
                        method=method,
                    ) from exc
                await self._sleep(min(2 ** (attempt - 1), MAX_RETRY_AFTER))
                continue

            if resp.status_code == 429:
                retry_after = self._retry_after(resp)
                if attempt > self._max_retries:
                    raise TelegramAPIError(
                        "rate limited (429): retries exhausted", error_code=429, method=method,
                    )
                logger.warning(
                    "telegram %s rate limited; sleeping %ss (attempt %d/%d)",
                    method, retry_after, attempt, self._max_retries,
                )
                await self._sleep(retry_after)
                continue

            if resp.status_code >= 500:
                if attempt > self._max_retries:
                    raise TelegramAPIError(
                        f"server error {resp.status_code}: retries exhausted",
                        error_code=resp.status_code, method=method,
                    )
                await self._sleep(min(2 ** (attempt - 1), MAX_RETRY_AFTER))
                continue

            body = self._parse_body(resp, method)
            if not body.get("ok", False):
                raise TelegramAPIError(
                    body.get("description", "unknown error"),
                    error_code=int(body.get("error_code", resp.status_code)),
                    method=method,
                )
            return body.get("result")

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float:
        """Extract the retry-after hint from a 429 (body params first, then header)."""
        try:
            params = resp.json().get("parameters", {})
            if "retry_after" in params:
                return min(float(params["retry_after"]), MAX_RETRY_AFTER)
        except Exception:
            pass
        header = resp.headers.get("Retry-After")
        if header:
            try:
                return min(float(header), MAX_RETRY_AFTER)
            except ValueError:
                pass
        return 1.0

    @staticmethod
    def _parse_body(resp: httpx.Response, method: str) -> dict[str, Any]:
        try:
            body = resp.json()
        except Exception as exc:
            raise TelegramAPIError(
                f"non-JSON response (status {resp.status_code})", method=method,
            ) from exc
        if not isinstance(body, dict):
            raise TelegramAPIError("unexpected response shape", method=method)
        return body

    # ── typed wrappers ──

    async def get_me(self) -> dict[str, Any]:
        return await self._call("getMe") or {}

    async def get_updates(
        self, offset: int = 0, timeout: int = DEFAULT_POLL_TIMEOUT,
        allowed_updates: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        data: dict[str, Any] = {"timeout": timeout}
        if offset:
            data["offset"] = offset
        if allowed_updates is not None:
            data["allowed_updates"] = allowed_updates
        # Give the read a margin over the server-side long-poll timeout.
        result = await self._call("getUpdates", data=data, read_timeout=timeout + 15)
        return list(result or [])

    async def send_message(
        self, chat_id: int | str, text: str, *,
        parse_mode: str | None = None, reply_to_message_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
        disable_web_page_preview: bool | None = None,
    ) -> dict[str, Any]:
        return await self._call(
            "sendMessage",
            data={
                "chat_id": chat_id, "text": text, "parse_mode": parse_mode,
                "reply_to_message_id": reply_to_message_id, "reply_markup": reply_markup,
                "disable_web_page_preview": disable_web_page_preview,
            },
        ) or {}

    async def edit_message_text(
        self, chat_id: int | str, message_id: int, text: str, *,
        parse_mode: str | None = None, reply_markup: dict[str, Any] | None = None,
        disable_web_page_preview: bool | None = None,
    ) -> dict[str, Any]:
        return await self._call(
            "editMessageText",
            data={
                "chat_id": chat_id, "message_id": message_id, "text": text,
                "parse_mode": parse_mode, "reply_markup": reply_markup,
                "disable_web_page_preview": disable_web_page_preview,
            },
        ) or {}

    async def send_document(
        self, chat_id: int | str, file_path: str, *,
        caption: str | None = None, reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        with open(file_path, "rb") as fh:
            files = {"document": (_basename(file_path), fh.read())}
        return await self._call(
            "sendDocument",
            data={"chat_id": chat_id, "caption": caption, "reply_to_message_id": reply_to_message_id},
            files=files,
        ) or {}

    async def send_photo(
        self, chat_id: int | str, file_path: str, *,
        caption: str | None = None, reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        with open(file_path, "rb") as fh:
            files = {"photo": (_basename(file_path), fh.read())}
        return await self._call(
            "sendPhoto",
            data={"chat_id": chat_id, "caption": caption, "reply_to_message_id": reply_to_message_id},
            files=files,
        ) or {}

    async def answer_callback_query(
        self, callback_query_id: str, *, text: str | None = None, show_alert: bool = False,
    ) -> bool:
        return bool(
            await self._call(
                "answerCallbackQuery",
                data={
                    "callback_query_id": callback_query_id, "text": text,
                    "show_alert": show_alert or None,
                },
            )
        )


def _basename(path: str) -> str:
    import os

    return os.path.basename(path) or "file"
