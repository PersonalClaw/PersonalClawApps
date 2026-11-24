"""A stop on a Bedrock chat closes the model's answer where it is.

``converse_stream`` answers with a blocking stream a worker thread reads. 🔴 Before, ``cancel()``
answered ``no_turn`` and reached nothing, so a stop waited for the model's answer to end: four
minutes in an owner chat. Now the provider keeps each request it has open (``InFlightRequests``)
and the stream its thread reads (``_Reading``): ``cancel()`` closes the stream, the thread leaves
its read, and the turn ends saying it was cancelled, with none of the cut answer's calls.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest
from test_provider import _install_fake_boto3, _stop_event

from personalclaw.sdk.model import EVENT_COMPLETE, EVENT_TEXT_CHUNK, EVENT_TOOL_CALL

TOOLS = [{"name": "write_file", "description": "w", "parameters": {"type": "object"}}]


class _Blocking:
    """A Converse stream that opens a call and writes some of its input, then waits for more of
    the answer until it is closed, the way a slow model's stream does."""

    def __init__(self) -> None:
        self.reached = threading.Event()
        self.closed = threading.Event()
        self.left = threading.Event()

    def __iter__(self) -> Any:
        try:
            yield {"contentBlockDelta": {"delta": {"text": "Writing it now."}}}
            yield {
                "contentBlockStart": {
                    "contentBlockIndex": 1,
                    "start": {"toolUse": {"toolUseId": "tu1", "name": "write_file"}},
                }
            }
            yield {
                "contentBlockDelta": {
                    "contentBlockIndex": 1,
                    "delta": {"toolUse": {"input": '{"path": "PLAN.md"'}},
                }
            }
            self.reached.set()
            if not self.closed.wait(timeout=30):
                yield _stop_event("tool_use")
                return
            raise ConnectionError("read on a closed stream")
        finally:
            self.left.set()

    def close(self) -> None:
        self.closed.set()


async def _provider(monkeypatch: pytest.MonkeyPatch, stream: _Blocking) -> Any:
    from provider import BedrockProvider

    client = _install_fake_boto3(monkeypatch, [])
    client.converse_stream = lambda **_kw: {"stream": stream}  # type: ignore[method-assign]
    provider = BedrockProvider(model="m")
    await provider.start()
    return provider


async def _stopped(provider: Any, stream: _Blocking, answer: Any) -> list[Any]:
    """What *answer* yields when a stop comes while its stream waits for more."""
    seen: list[Any] = []

    async def _read() -> None:
        async for event in answer:
            seen.append(event)

    reader = asyncio.create_task(_read())
    assert await asyncio.to_thread(stream.reached.wait, 5), "the answer never started"
    assert await provider.cancel() == "acked"
    await asyncio.wait_for(reader, timeout=1.0)
    assert await asyncio.to_thread(stream.left.wait, 1.0), "the reader thread is still reading"
    assert stream.closed.is_set()
    return seen


@pytest.mark.asyncio
async def test_a_stop_closes_the_answer_and_emits_none_of_its_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _Blocking()
    provider = await _provider(monkeypatch, stream)
    answer = provider.complete([{"role": "user", "content": "write the plan"}], tools=TOOLS)

    seen = await _stopped(provider, stream, answer)

    assert [e.text for e in seen if e.kind == EVENT_TEXT_CHUNK] == ["Writing it now."]
    assert [e for e in seen if e.kind == EVENT_TOOL_CALL] == [], "a cut call was dispatched"
    assert (seen[-1].kind, seen[-1].stop_reason) == (EVENT_COMPLETE, "cancelled")


@pytest.mark.asyncio
async def test_a_stop_closes_a_chat_answer_and_keeps_none_of_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _Blocking()
    provider = await _provider(monkeypatch, stream)

    seen = await _stopped(provider, stream, provider.stream("write the plan"))

    assert (seen[-1].kind, seen[-1].stop_reason) == (EVENT_COMPLETE, "cancelled")
    assert [turn["role"] for turn in provider._history] == ["user"], provider._history


@pytest.mark.asyncio
async def test_with_no_answer_open_a_stop_has_no_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = _Blocking()
    provider = await _provider(monkeypatch, stream)
    assert await provider.cancel() == "no_turn"
    await _stopped(provider, stream, provider.complete([{"role": "user", "content": "go"}]))
    assert await provider.cancel() == "no_turn", "the cut answer is still held open"


@pytest.mark.asyncio
async def test_an_answer_nobody_stops_ends_as_the_model_ends_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control: the same stream, not stopped, ends with its call."""
    stream = _Blocking()
    provider = await _provider(monkeypatch, stream)
    stream.closed.wait = lambda timeout=None: False  # type: ignore[method-assign]

    seen = [e async for e in provider.complete([{"role": "user", "content": "go"}], tools=TOOLS)]

    assert [e.title for e in seen if e.kind == EVENT_TOOL_CALL] == ["write_file"]
    assert seen[-1].stop_reason == "tool_use"
