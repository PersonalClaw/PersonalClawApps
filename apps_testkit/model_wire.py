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

``GET …/models`` answers too, and so do Bedrock's control-plane listings (``GET
/foundation-models``, ``GET /inference-profiles``, for a boto3 client pointed here with
``AWS_ENDPOINT_URL_BEDROCK``). They list :data:`MODEL`, or the ids a test names
(``RecordingModelServer(models=…)``) when what a catalog makes of each id is the point, or
:data:`LISTED` when the point is that nothing may pick one.

Build the instance the way the product builds it: :func:`form_options` is what the Add-instance
form saves (the app's own settings fields, no pinned model), and core calls a bound model with the
binding's model as the ``model`` build kwarg. An entry written by hand with keys the form never
saves is how three apps passed their tests while every instance a user saved failed.

:func:`blank_model_report` is the other half of what a call carries: its model. An instance saved
with its Default Model left empty names no model, and a call on it that names none either is
refused before anything is sent (the SDK's ``require_model``). It is never sent ``"model": ""``,
and no app picks a model in its place — the first one its endpoint lists, the newest in a curated
list, a vendor default. With a Default Model, that is the model the call names (the SDK's
``own_model``). Every chat-model app runs the same report through both of its build paths.
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

#: A model the endpoint lists that NOBODY chose. :func:`blank_model_report` serves it
#: (``RecordingModelServer(models=(LISTED,))``), so a provider that picks a model of its own
#: shows up on the wire as a call naming it.
LISTED = "wire-listed-model"

#: The text every reply carries, so a test can tell the call completed.
REPLY = "ok"

#: A 1×1 PNG as the data URL core puts in an image part.
PNG_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class RecordingModelServer:
    """A chat endpoint on ``127.0.0.1`` that answers every dialect above and keeps every request.

    ``with RecordingModelServer() as server:`` starts it; ``server.url`` is its base URL and
    ``server.requests`` the ``{"method", "path", "headers", "body"}`` of each request, oldest
    first (header names lower-cased). ``models`` is what ``GET …/models`` lists.
    """

    def __init__(self, models: tuple[str, ...] = (MODEL,)) -> None:
        self.models = tuple(models)
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


def images_sent(request: dict[str, Any]) -> list[str]:
    """The image URLs one recorded Chat Completions call carried, in message order: every
    ``image_url`` content part, as the model receives it."""
    body = request["body"] if isinstance(request["body"], dict) else {}
    urls: list[str] = []
    for message in body.get("messages") or []:
        content = message.get("content") if isinstance(message, dict) else None
        for part in content if isinstance(content, list) else []:
            if isinstance(part, dict) and part.get("type") == "image_url":
                urls.append(str((part.get("image_url") or {}).get("url", "")))
    return urls


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


async def image_call(provider: Any) -> str:
    """Drive ``provider`` through one model call carrying an image, the way core's native loop
    makes it: ``start()``, one ``complete()`` whose user turn is a text part and an ``image_url``
    part (core's neutral shape), ``shutdown()``. Returns the text it streamed."""
    await provider.start()
    text = ""
    try:
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is in this image?"},
                    {"type": "image_url", "image_url": {"url": PNG_DATA_URL}},
                ],
            }
        ]
        async for event in provider.complete(messages):
            if getattr(event, "kind", "") == "text_chunk":
                text += getattr(event, "text", "") or ""
    finally:
        await provider.shutdown()
    return text


# ── A call no model is chosen for ─────────────────────────────────────────────────────────


def no_model_refusal() -> str:
    """The sentence the SDK's ``require_model`` refuses a call with when it names no model: what a
    provider says instead of sending ``"model": ""`` or picking a model of its own."""
    from personalclaw.sdk.model import ProviderResolutionError, require_model

    try:
        require_model("")
    except ProviderResolutionError as refused:
        return str(refused)
    raise AssertionError("require_model let a call that names no model through")


async def _two_calls(provider: Any, server: RecordingModelServer) -> tuple[list[str], int]:
    """What became of the two calls core makes of a provider, after ``start()``: one ``stream()``
    (the one-shot path) and one ``complete()`` (the native loop). Each reads ``"refused: <why>"``
    (the SDK's refusal), ``"sent '<model>'"`` (the model the request named), or ``"failed:
    <error>"``, and says so when a request went out before a refusal or failure. Also returns how
    many turns the provider's own conversation holds afterwards."""
    from personalclaw.sdk.model import ProviderResolutionError

    async def _drain(events: Any) -> None:
        async for _event in events:
            pass

    outcomes: list[str] = []
    await provider.start()
    try:
        for call in (
            lambda: provider.stream("hi"),
            lambda: provider.complete([{"role": "user", "content": "hi"}]),
        ):
            before = len(server.calls())
            try:
                await _drain(call())
            except ProviderResolutionError as refused:
                outcome = f"refused: {refused}"
            except Exception as failed:  # noqa: BLE001 — recorded, so the report says what happened
                outcome = f"failed: {type(failed).__name__}"
            else:
                outcome = "sent"
            sent = server.calls()[before:]
            if outcome == "sent" and not sent:
                outcome = "raised nothing, sent nothing"
            elif outcome == "sent":
                outcome = f"sent {model_sent(sent[-1])!r}"
            elif sent:
                outcome += f" (after sending {model_sent(sent[-1])!r})"
            outcomes.append(outcome)
        held = len(getattr(provider, "_history", None) or [])
    finally:
        await provider.shutdown()
    return outcomes, held


async def blank_model_report(
    *,
    app_dir: Path,
    entry_type: str,
    factory: Any,
    create_provider: Any,
    server: RecordingModelServer,
) -> dict[str, object]:
    """What becomes of a call no model is chosen for, through both of an app's build paths:
    ``_factory`` (the registry path, which core builds every call's provider through) and
    ``create_provider`` (the manifest's entry point, handed the instance's settings).

    The instance is saved the way the Add-instance form saves it (:func:`form_options`), with its
    Default Model left empty and, when the app's form has that field, set to :data:`MODEL`.
    ``server`` should list :data:`LISTED` only (``RecordingModelServer(models=(LISTED,))``), so a
    provider that picks a model of its own names it on the wire. Compare the result with
    :func:`blank_model_expected`.
    """
    from personalclaw.sdk.model import ProviderEntry

    def _entry(options: dict[str, object]) -> Any:
        return ProviderEntry(name="wire", type=entry_type, model="", options=dict(options))

    empty = form_options(app_dir, server.url)
    empty.pop("default_model", None)
    from_factory, factory_held = await _two_calls(factory(entry=_entry(empty)), server)
    from_config, config_held = await _two_calls(create_provider(dict(empty)), server)
    report: dict[str, object] = {
        "_factory, Default Model empty": from_factory,
        "create_provider, Default Model empty": from_config,
        "turns a refused call left in the conversation": [factory_held, config_held],
    }
    if _has_default_model(app_dir):
        full = form_options(app_dir, server.url)
        named_by_factory, _ = await _two_calls(factory(entry=_entry(full)), server)
        named_by_config, _ = await _two_calls(create_provider(dict(full)), server)
        report["_factory, Default Model set"] = named_by_factory
        report["create_provider, Default Model set"] = named_by_config
    return report


def _has_default_model(app_dir: Path) -> bool:
    """Whether the app's Add-instance form has a Default Model field."""
    return "default_model" in form_options(app_dir, "")


def blank_model_expected(app_dir: Path) -> dict[str, object]:
    """:func:`blank_model_report` for an app that neither sends an empty model nor picks one: with
    its Default Model empty both calls are refused by the SDK and leave nothing behind, and with it
    set (an app whose form has the field) both calls name it."""
    refused = [f"refused: {no_model_refusal()}"] * 2
    expected: dict[str, object] = {
        "_factory, Default Model empty": refused,
        "create_provider, Default Model empty": refused,
        "turns a refused call left in the conversation": [0, 0],
    }
    if _has_default_model(app_dir):
        named = [f"sent {MODEL!r}"] * 2
        expected["_factory, Default Model set"] = named
        expected["create_provider, Default Model set"] = named
    return expected


# ── A media call no model is chosen for ───────────────────────────────────────────────────


def _media_kind(adapter: Any) -> str | None:
    """Which media contract *adapter* serves: ``image``, ``video``, ``stt``, ``tts``,
    ``embedding`` or ``diarization``, or ``None`` for a chat provider. Read from the SDK's
    contracts, and for text-to-speech from the call too: an adapter a scanner registers need not
    subclass the SDK's class to be called like one."""
    from personalclaw.sdk.diarization import DiarizationProvider
    from personalclaw.sdk.embedding import EmbeddingProvider
    from personalclaw.sdk.image import ImageGenProvider
    from personalclaw.sdk.stt import SttProvider
    from personalclaw.sdk.tts import TtsProvider
    from personalclaw.sdk.video import VideoGenProvider

    for contract, kind in (
        (ImageGenProvider, "image"),
        (VideoGenProvider, "video"),
        (SttProvider, "stt"),
        (TtsProvider, "tts"),
        (EmbeddingProvider, "embedding"),
        (DiarizationProvider, "diarization"),
    ):
        if isinstance(adapter, contract):
            return kind
    return "tts" if callable(getattr(adapter, "synthesize", None)) else None


def media_adapters(
    app_dir: Path, create_provider: Any, *, scanners: tuple[Any, ...] = ()
) -> list[Any]:
    """Every media adapter core builds for one instance of the app at *app_dir*, saved the way
    its Add-instance form saves it with the Default Model left empty and fake credentials: the
    adapters ``create_provider`` returns, and the ones each of the app's media *scanners* (the
    functions it hands the SDK's ``register_scanner``) builds for that instance's config entry.
    Chat providers are left out; nothing here reaches the network."""
    import inspect

    manifest = json.loads((app_dir / "app.json").read_text(encoding="utf-8"))
    provider = manifest.get("provider") or {}
    fields = (provider.get("settingsSchema") or {}).get("properties") or {}
    fakes = {
        "api_key": "k-wire",
        "endpoint": "http://127.0.0.1:9",
        "region": "us-east-1",
        "hf_token": "hf-wire",
    }
    options = {key: value for key, value in fakes.items() if key in fields}
    takes_config = bool(inspect.signature(create_provider).parameters)
    made = create_provider(dict(options)) if takes_config else create_provider()
    adapters = list(made) if isinstance(made, (list, tuple)) else [made]
    entry = {"name": "wire", "type": provider.get("providerType") or app_dir.name, "options": options}
    for scanner in scanners:
        adapters.extend(scanner([dict(entry, options=dict(options))]) or [])
    return [adapter for adapter in adapters if _media_kind(adapter) is not None]


def _media_adapters(adapters: Any) -> list[tuple[str, Any]]:
    """``(kind, adapter)`` for each media adapter in *adapters*: one provider, or the list a
    multi-use ``create_provider`` returns (chat providers in it are skipped)."""
    found = []
    for adapter in adapters if isinstance(adapters, (list, tuple)) else [adapters]:
        kind = _media_kind(adapter)
        if kind is not None:
            found.append((kind, adapter))
    if not found:
        raise AssertionError(f"no media adapter among {adapters!r}")
    return found


class _NoNetwork:
    """Blocks every socket connection and name lookup while it is entered, and counts the tries:
    a refusal happens before anything is sent, so one that reached for the network first is not
    the refusal this report is about. Process-wide, because a provider may reach the network from
    a worker thread."""

    def __init__(self) -> None:
        self.tries = 0
        self._saved: list[tuple[Any, str, Any]] = []

    def _refuse(self, *_args: Any, **_kwargs: Any) -> Any:
        self.tries += 1
        raise OSError("media_refusal_report blocks the network")

    def __enter__(self) -> "_NoNetwork":
        import socket

        for owner, name in (
            (socket.socket, "connect"),
            (socket.socket, "connect_ex"),
            (socket, "create_connection"),
            (socket, "getaddrinfo"),
        ):
            self._saved.append((owner, name, getattr(owner, name)))
            setattr(owner, name, self._refuse)
        return self

    def __exit__(self, *exc: object) -> None:
        for owner, name, original in reversed(self._saved):
            setattr(owner, name, original)
        self._saved.clear()


class _Said:
    """Every log record written while it is entered, whatever its logger."""

    def __init__(self) -> None:
        import logging

        self.messages: list[str] = []
        self._handler = logging.Handler(logging.DEBUG)
        self._handler.emit = lambda record: self.messages.append(record.getMessage())  # type: ignore[method-assign]
        self._root = logging.getLogger()
        self._level = self._root.level

    def __enter__(self) -> "_Said":
        import logging

        self._root.addHandler(self._handler)
        self._root.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc: object) -> None:
        self._root.removeHandler(self._handler)
        self._root.setLevel(self._level)


def _silent_audio(folder: Path) -> Path:
    """A 0.1 s silent WAV: what a speech or diarization call is handed."""
    import wave

    path = folder / "silence.wav"
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(b"\x00\x00" * 1600)
    return path


#: The calls core makes of each kind of media adapter, in the order the report runs them.
_MEDIA_CALLS: dict[str, tuple[str, ...]] = {
    "image": ("generate", "edit"),
    "video": ("generate",),
    "stt": ("transcribe", "transcribe_detailed"),
    "tts": ("synthesize",),
    "embedding": ("embed", "embed_batch"),
    "diarization": ("diarize",),
}


def _media_call_names(kind: str, *, edit: bool) -> tuple[str, ...]:
    return tuple(name for name in _MEDIA_CALLS[kind] if edit or name != "edit")


def _media_call(kind: str, name: str, adapter: Any, folder: Path) -> Any:
    """The coroutine for one call core makes of a *kind* adapter, naming no model: the image,
    video, speech, embedding or diarization binding named none, so ``model=""`` (a voice is
    text-to-speech's model). Inputs the call reads are written into *folder*."""
    import base64

    if kind in ("image", "video") and name == "generate":
        return adapter.generate("a heron at dawn", model="")
    if name == "edit":
        source = folder / "source.png"
        source.write_bytes(base64.b64decode(PNG_DATA_URL.split(",", 1)[1]))
        return adapter.edit("make it dusk", source_image=str(source), model="")
    if name in ("transcribe", "transcribe_detailed"):
        return getattr(adapter, name)(str(_silent_audio(folder)), model="")
    if name == "synthesize":
        return adapter.synthesize("hello", voice="")
    if name == "embed":
        return adapter.embed("a heron", model="")
    if name == "embed_batch":
        return adapter.embed_batch(["a heron", "a kestrel"], model="")
    return adapter.diarize(str(_silent_audio(folder)), model="")


def _returned_nothing(value: Any) -> bool:
    """A speech, embedding or diarization contract's "nothing": ``None``, an empty result, or a
    batch whose every vector is empty."""
    if value is None:
        return True
    if isinstance(value, (list, tuple)):
        return all(isinstance(item, (list, tuple)) and not item for item in value)
    return value in ("", b"")


async def media_refusal_report(adapters: Any, *, edit: bool = True) -> dict[str, str]:
    """What each media call core makes of *adapters* does when it names no model.

    *adapters* is what the app hands core: one provider, or the list a multi-use
    ``create_provider`` returns. Build it with its credentials in place (a key, a region), so the
    one thing a call lacks is its model. Every call names none (``model=""``; for speech, the
    voice), with the network blocked and every log record kept:

    - an image or video call reads ``"refused: <message>"`` when it raises its contract's error
      (``ImageGenError``, ``VideoGenError``: the error core turns into the tool's answer);
    - a speech, embedding or diarization call reads ``"refused: <sentence>"`` when it returns
      nothing and logged the SDK's sentence, and ``"returned nothing, said nothing"`` when it
      returned nothing without it;
    - anything else reads ``"returned <value>"`` or ``"failed: <error>"``;
    - a call that tried the network first adds ``" (after reaching the network)"``;
    - text-to-speech also reads ``can_synthesize("")``, which must not claim a call it refuses.

    *edit* ``False`` leaves out an image adapter's ``edit``, for one without an edit endpoint
    (its contract is to raise that it has none, which says nothing about the model). Compare the
    result with :func:`media_refusal_expected`.
    """
    import tempfile

    from personalclaw.sdk.image import ImageGenError
    from personalclaw.sdk.video import VideoGenError

    sentence = no_model_refusal()
    contract_error = {"image": ImageGenError, "video": VideoGenError}
    report: dict[str, str] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for kind, adapter in _media_adapters(adapters):
            for name in _media_call_names(kind, edit=edit):
                error = contract_error.get(kind, ())
                with _NoNetwork() as network, _Said() as said:
                    try:
                        value = await _media_call(kind, name, adapter, Path(tmp))
                    except error as refused:  # type: ignore[misc]
                        outcome = f"refused: {refused}"
                    except Exception as failed:  # noqa: BLE001 — recorded, so the report says it
                        outcome = f"failed: {type(failed).__name__}: {failed}"
                    else:
                        if kind in contract_error or not _returned_nothing(value):
                            outcome = f"returned {value!r:.60}"
                        elif any(sentence in message for message in said.messages):
                            outcome = f"refused: {sentence}"
                        else:
                            outcome = "returned nothing, said nothing"
                if network.tries:
                    outcome += " (after reaching the network)"
                report[f"{kind} {name}"] = outcome
            if kind == "tts" and callable(getattr(adapter, "can_synthesize", None)):
                with _NoNetwork():
                    report["tts can_synthesize"] = repr(await adapter.can_synthesize(""))
    return report


def media_refusal_expected(adapters: Any, *, edit: bool = True) -> dict[str, str]:
    """:func:`media_refusal_report` for adapters that refuse every media call naming no model with
    the SDK's sentence, before anything is sent, and whose text-to-speech claims no such call."""
    refused = f"refused: {no_model_refusal()}"
    expected: dict[str, str] = {}
    for kind, adapter in _media_adapters(adapters):
        for name in _media_call_names(kind, edit=edit):
            expected[f"{kind} {name}"] = refused
        if kind == "tts" and callable(getattr(adapter, "can_synthesize", None)):
            expected["tts can_synthesize"] = "False"
    return expected


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


def _models_reply(models: tuple[str, ...]) -> tuple[str, bytes]:
    body = {
        "object": "list",
        "data": [
            {
                "id": model,
                "object": "model",
                "type": "model",
                "display_name": model,
                "created_at": "2026-01-01T00:00:00Z",
            }
            for model in models
        ],
        "has_more": False,
        "first_id": models[0] if models else None,
        "last_id": models[-1] if models else None,
    }
    return "application/json", json.dumps(body).encode()


def _foundation_models_reply(models: tuple[str, ...]) -> tuple[str, bytes]:
    """Bedrock's ``ListFoundationModels``: each id as an active on-demand text model."""
    summaries = [
        {
            "modelId": model,
            "modelName": model,
            "providerName": "wire",
            "inputModalities": ["TEXT"],
            "outputModalities": ["TEXT"],
            "inferenceTypesSupported": ["ON_DEMAND"],
            "modelLifecycle": {"status": "ACTIVE"},
        }
        for model in models
    ]
    return "application/json", json.dumps({"modelSummaries": summaries}).encode()


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
                self._reply(200, *_models_reply(server.models))
            elif path.rstrip("/").endswith("/foundation-models"):
                self._reply(200, *_foundation_models_reply(server.models))
            elif path.rstrip("/").endswith("/inference-profiles"):
                self._reply(200, "application/json", b'{"inferenceProfileSummaries": []}')
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
