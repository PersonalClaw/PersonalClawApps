"""A fake model endpoint that records the request a model app's provider puts on the wire.

Core tells a model app's factory what it wants of THIS call through build kwargs: a sampling
``temperature`` (best-of-N's ladder) and an output ``max_tokens`` budget (derived per model). A
factory that reads them and a request that carries them are two different claims, and only the
second one is what the model sees. A test against a fake SDK client, or against the provider's
attributes, can pass while the real client puts something else on the socket — so every model
app proves it here instead: its real SDK client and its real request, against an endpoint on
``127.0.0.1`` that keeps each body.

One server, three dialects:

- OpenAI Chat Completions (``POST …/chat/completions``, streamed or not) — every app on core's
  ``OpenAIProvider``;
- Anthropic Messages (``POST …/messages``, streamed or not) — every app on ``AnthropicProvider``;
- Bedrock Converse (``POST /model/<id>/converse-stream``), answered in AWS's binary event-stream
  framing, for a boto3 client pointed here with ``AWS_ENDPOINT_URL_BEDROCK_RUNTIME``.

``GET …/models`` answers too, so a provider that discovers a model at ``start()`` finds one.

Build the instance the way the product builds it: :func:`form_options` is what the Add-instance
form saves (the app's own settings fields, no pinned model), and core calls a bound model with the
binding's model as the ``model`` build kwarg. An entry written by hand with keys the form never
saves is how three apps passed their tests while every instance a user saved failed.
"""

from __future__ import annotations

import binascii
import json
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

#: The one model the endpoint serves and every reply names.
MODEL = "wire-test-model"

#: The text every reply carries, so a test can tell the call completed.
REPLY = "ok"


class RecordingModelServer:
    """A chat endpoint on ``127.0.0.1`` that answers every dialect above and keeps every request.

    ``with RecordingModelServer() as server:`` starts it; ``server.url`` is its base URL and
    ``server.requests`` the ``{"method", "path", "headers", "body"}`` of each request, oldest
    first (header names lower-cased).
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(self))
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def record(self, method: str, path: str, headers: dict[str, str], body: Any) -> None:
        with self._lock:
            self.requests.append({"method": method, "path": path, "headers": headers, "body": body})

    def calls(self) -> list[dict[str, Any]]:
        """The model CALLS — every request that asked for a completion (discovery excluded)."""
        with self._lock:
            return [r for r in self.requests if r["method"] == "POST"]

    def __enter__(self) -> "RecordingModelServer":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def form_options(app_dir: Path, url: str) -> dict[str, object]:
    """The options the Add-instance form saves for an instance of this app pointed at *url*.

    Read from the app's own ``settingsSchema``, not written by hand: the form saves the schema's
    field names (``endpoint``, ``default_model``), and a factory that reads a key its form never
    writes (``base_url``) passes a test that builds the entry by hand while every instance a user
    saves fails.
    """
    manifest = json.loads((app_dir / "app.json").read_text(encoding="utf-8"))
    fields = manifest["provider"]["settingsSchema"]["properties"]
    values = {"api_key": "k-wire", "endpoint": url, "default_model": MODEL, "region": "us-east-1"}
    return {key: value for key, value in values.items() if key in fields}


def model_sent(request: dict[str, Any]) -> object:
    """The model one recorded call named: the body's ``model``, or Converse's path segment."""
    if request["path"].startswith("/model/"):
        return request["path"].split("/")[2]
    body = request["body"] if isinstance(request["body"], dict) else {}
    return body.get("model")


def sampling_sent(request: dict[str, Any]) -> tuple[object, object]:
    """``(temperature, output cap)`` as one recorded model call carried them — ``None`` for a
    field the request left out. Read in the request's own dialect: Converse nests both under
    ``inferenceConfig`` (``maxTokens``); the other two carry ``temperature`` and ``max_tokens``
    at the top level of the body."""
    body = request["body"] if isinstance(request["body"], dict) else {}
    if request["path"].endswith(("/converse-stream", "/converse")):
        config = body.get("inferenceConfig") or {}
        return config.get("temperature"), config.get("maxTokens")
    return body.get("temperature"), body.get("max_tokens")


async def one_call(provider: Any, prompt: str = "hi") -> str:
    """Drive ``provider`` through the call core's one-shot path makes — ``start()``, one
    ``stream()`` to completion, ``shutdown()`` — and return the text it streamed."""
    await provider.start()
    text = ""
    try:
        async for event in provider.stream(prompt):
            if getattr(event, "kind", "") == "text_chunk":
                text += getattr(event, "text", "") or ""
    finally:
        await provider.shutdown()
    return text


# ── The dialects ──────────────────────────────────────────────────────────────────────────


def _sse(events: list[tuple[str | None, dict[str, Any] | str]]) -> bytes:
    lines: list[str] = []
    for name, data in events:
        if name:
            lines.append(f"event: {name}")
        lines.append(f"data: {data if isinstance(data, str) else json.dumps(data)}")
        lines.append("")
    return ("\n".join(lines) + "\n").encode()


def _openai_reply(stream: bool) -> tuple[str, bytes]:
    usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
    head = {"id": "chatcmpl-wire", "created": 0, "model": MODEL}
    if not stream:
        body = {
            **head,
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": REPLY},
                    "finish_reason": "stop",
                }
            ],
            "usage": usage,
        }
        return "application/json", json.dumps(body).encode()
    chunk = {**head, "object": "chat.completion.chunk"}
    return "text/event-stream", _sse(
        [
            (
                None,
                {
                    **chunk,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": REPLY},
                            "finish_reason": None,
                        }
                    ],
                },
            ),
            (None, {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
            (None, {**chunk, "choices": [], "usage": usage}),
            (None, "[DONE]"),
        ]
    )


def _anthropic_reply(stream: bool) -> tuple[str, bytes]:
    message = {
        "id": "msg_wire",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "stop_sequence": None,
    }
    if not stream:
        body = {
            **message,
            "content": [{"type": "text", "text": REPLY}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        return "application/json", json.dumps(body).encode()
    return "text/event-stream", _sse(
        [
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        **message,
                        "content": [],
                        "stop_reason": None,
                        "usage": {"input_tokens": 1, "output_tokens": 0},
                    },
                },
            ),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": REPLY},
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 1},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
    )


def _event_stream_message(event_type: str, payload: dict[str, Any]) -> bytes:
    """One AWS event-stream frame: prelude (total length, headers length, CRC), string headers,
    the JSON payload, and the message CRC — what botocore's ``EventStream`` parser reads."""
    headers = b""
    for name, value in (
        (":event-type", event_type),
        (":content-type", "application/json"),
        (":message-type", "event"),
    ):
        key, val = name.encode(), value.encode()
        headers += struct.pack(">B", len(key)) + key + b"\x07" + struct.pack(">H", len(val)) + val
    payload_bytes = json.dumps(payload).encode()
    prelude = struct.pack(">II", 12 + len(headers) + len(payload_bytes) + 4, len(headers))
    prelude += struct.pack(">I", binascii.crc32(prelude) & 0xFFFFFFFF)
    message = prelude + headers + payload_bytes
    return message + struct.pack(">I", binascii.crc32(message) & 0xFFFFFFFF)


def _converse_stream_reply() -> tuple[str, bytes]:
    frames = [
        ("messageStart", {"role": "assistant"}),
        ("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": REPLY}}),
        ("contentBlockStop", {"contentBlockIndex": 0}),
        ("messageStop", {"stopReason": "end_turn"}),
        (
            "metadata",
            {
                "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                "metrics": {"latencyMs": 1},
            },
        ),
    ]
    return "application/vnd.amazon.eventstream", b"".join(
        _event_stream_message(name, payload) for name, payload in frames
    )


def _models_reply() -> tuple[str, bytes]:
    body = {
        "object": "list",
        "data": [
            {
                "id": MODEL,
                "object": "model",
                "type": "model",
                "display_name": MODEL,
                "created_at": "2026-01-01T00:00:00Z",
            }
        ],
        "has_more": False,
        "first_id": MODEL,
        "last_id": MODEL,
    }
    return "application/json", json.dumps(body).encode()


def _handler_for(server: RecordingModelServer) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:  # keep test output quiet
            pass

        def _reply(self, status: int, content_type: str, payload: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)

        def _headers(self) -> dict[str, str]:
            return {name.lower(): value for name, value in self.headers.items()}

        def do_GET(self) -> None:  # noqa: N802 — the http.server hook name
            path = self.path.split("?", 1)[0]
            server.record("GET", path, self._headers(), None)
            if path.rstrip("/").endswith("/models"):
                self._reply(200, *_models_reply())
            else:
                self._reply(404, "application/json", b'{"error": "not found"}')

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                body: Any = json.loads(raw or b"{}")
            except ValueError:
                body = raw.decode("utf-8", "replace")
            server.record("POST", path, self._headers(), body)
            stream = isinstance(body, dict) and bool(body.get("stream"))
            if path.endswith("/chat/completions"):
                self._reply(200, *_openai_reply(stream))
            elif path.endswith("/messages"):
                self._reply(200, *_anthropic_reply(stream))
            elif path.endswith("/converse-stream"):
                self._reply(200, *_converse_stream_reply())
            else:
                self._reply(404, "application/json", b'{"error": "not found"}')

    return _Handler
