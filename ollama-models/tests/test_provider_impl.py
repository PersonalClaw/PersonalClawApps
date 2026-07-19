"""Unit tests for OllamaProvider.

The ``httpx`` SDK may or may not be installed in this dev environment.
These tests substitute a stub module into ``sys.modules`` before
constructing ``OllamaProvider`` so the lazy import resolves to the stub
and exercises a deterministic event stream.
"""

import importlib
import json
import sys
import types
from typing import Any

import pytest

from personalclaw.llm import (
    Capability,
    ProviderEntry,
    ProviderRegistry,
)
from personalclaw.llm.base import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
)
from personalclaw.llm.registry import get_default_registry

# ── Fakes for the httpx SDK ─────────────────────────────────────────────


class _FakeStreamResponse:
    """Async context manager yielding pre-canned NDJSON lines."""

    def __init__(self, lines: list[str], status_code: int = 200, error_body: str = "") -> None:
        self._lines = lines
        self.status_code = status_code
        self._error_body = error_body

    async def __aenter__(self) -> "_FakeStreamResponse":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    async def aread(self) -> bytes:
        return self._error_body.encode()

    async def aiter_lines(self):  # noqa: ANN201 — async generator
        for line in self._lines:
            yield line


class _FakePostResponse:
    """Plain response object for non-streaming POSTs (e.g. /api/embed)."""

    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeAsyncClient:
    constructed: list[dict[str, Any]] = []

    def __init__(self, *, base_url: str, timeout: float) -> None:
        type(self).constructed.append({"base_url": base_url, "timeout": timeout})
        self.base_url = base_url
        self.timeout = timeout
        self.closed = False
        # Tests override these to inject canned responses.
        self.stream_lines: list[str] = []
        self.stream_calls: list[dict[str, Any]] = []
        # Optional queue of (lines, status_code, error_body) tuples — each
        # stream() call pops the next, falling back to ``stream_lines`` for a
        # single canned response. Lets a test model a 400-then-retry sequence.
        self.stream_responses: list[tuple[list[str], int, str]] = []
        self.post_payload: dict[str, Any] = {"embeddings": []}
        self.post_calls: list[dict[str, Any]] = []

    def stream(self, method: str, url: str, *, json: dict[str, Any]) -> _FakeStreamResponse:
        self.stream_calls.append({"method": method, "url": url, "json": json})
        if self.stream_responses:
            lines, status, err = self.stream_responses.pop(0)
            return _FakeStreamResponse(lines, status_code=status, error_body=err)
        return _FakeStreamResponse(self.stream_lines)

    async def post(self, url: str, *, json: dict[str, Any]) -> _FakePostResponse:
        self.post_calls.append({"url": url, "json": json})
        return _FakePostResponse(self.post_payload)

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def fake_httpx(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Install a fake ``httpx`` module into ``sys.modules``."""
    fake = types.ModuleType("httpx")
    fake.AsyncClient = _FakeAsyncClient  # type: ignore[attr-defined]
    _FakeAsyncClient.constructed = []
    monkeypatch.setitem(sys.modules, "httpx", fake)
    return fake


# ── lazy SDK import ────────────────────────────────────────


def test_ollama_module_does_not_import_httpx_at_top_level() -> None:
    """Importing ``personalclaw.providers.ollama`` MUST NOT pull in ``httpx``."""
    sys.modules.pop("httpx", None)
    sys.modules.pop("provider", None)
    importlib.import_module("provider")
    assert "httpx" not in sys.modules


def test_importing_providers_package_does_not_import_httpx() -> None:
    """``import personalclaw.llm`` MUST NOT pull in ``httpx``."""
    sys.modules.pop("httpx", None)
    sys.modules.pop("personalclaw.providers", None)
    sys.modules.pop("provider", None)
    importlib.import_module("personalclaw.providers")
    assert "httpx" not in sys.modules


def test_ollama_constructor_lazy_imports_httpx(fake_httpx: types.ModuleType) -> None:
    """Instantiating ``OllamaProvider`` triggers the lazy SDK import."""
    from provider import OllamaProvider

    provider = OllamaProvider(model="llama3.1:8b")

    assert provider is not None
    assert _FakeAsyncClient.constructed[-1]["base_url"] == "http://localhost:11434"


# ── Constructor / endpoint handling ─────────────────────────────────────


def test_ollama_default_endpoint(fake_httpx: types.ModuleType) -> None:
    """The default endpoint is ``http://localhost:11434`` per design § A.5."""
    from provider import OllamaProvider

    provider = OllamaProvider(model="llama3.1:8b")

    assert provider._endpoint == "http://localhost:11434"
    assert _FakeAsyncClient.constructed[-1]["base_url"] == "http://localhost:11434"


def test_ollama_endpoint_override(fake_httpx: types.ModuleType) -> None:
    """``options.endpoint`` overrides the default endpoint."""
    from provider import OllamaProvider

    provider = OllamaProvider(
        model="llama3.1:8b",
        endpoint="http://ollama.internal:11434/",
    )

    # Trailing slash is stripped to make join semantics predictable.
    assert provider._endpoint == "http://ollama.internal:11434"


# ── Capability descriptor + registry registration ──────────────────────


def test_ollama_capability_descriptor() -> None:
    from provider import OLLAMA_CAPABILITY

    assert OLLAMA_CAPABILITY.type == "ollama"
    expected = {
        Capability.CHAT,
        Capability.CODE_TOOLS,
        Capability.SUMMARIZATION,
        Capability.STREAMING,
        Capability.EMBEDDING,
        Capability.VISION,
    }
    assert OLLAMA_CAPABILITY.capabilities == frozenset(expected)
    assert OLLAMA_CAPABILITY.supports_streaming is True
    assert OLLAMA_CAPABILITY.supports_embeddings is True
    # Tools are advertised (Ollama's /api/chat forwards a tools schema and
    # streams tool_calls) but degrade per model — complete() retries tool-less
    # on rejection. Vision is advertised at the TYPE level (moondream/llava/qwen-vl
    # are vision-capable) so a bound vision model resolves + receives images; a
    # text-only model picked for a vision role simply no-ops on the image.
    assert OLLAMA_CAPABILITY.supports_tools is True
    assert OLLAMA_CAPABILITY.supports_vision is True


def test_to_ollama_messages_translates_image_blocks() -> None:
    """Multimodal content blocks (text + image_url data-url) → Ollama's shape: text in
    `content`, bare base64 in the `images` array (no data-url prefix). Without this a
    list content str()'s into garbage and a vision model gets no image."""
    import base64

    from provider import _to_ollama_messages

    raw = b"jpegbytes"
    b64 = base64.b64encode(raw).decode()
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "describe"},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
    ]}]
    out = _to_ollama_messages(msgs)
    assert out[0]["content"] == "describe"
    assert out[0]["images"] == [b64]  # bare base64, no "data:" prefix
    # a plain string message is untouched (no images key)
    plain = _to_ollama_messages([{"role": "user", "content": "hi"}])
    assert plain == [{"role": "user", "content": "hi"}]


def test_ollama_registers_with_default_registry() -> None:
    importlib.import_module("provider")
    cap = get_default_registry().capability_of("ollama")
    assert cap is not None
    assert cap.type == "ollama"


# ── Factory / endpoint override via ProviderEntry.options ──────────────


def test_ollama_factory_uses_default_endpoint(fake_httpx: types.ModuleType) -> None:
    from provider import OLLAMA_CAPABILITY, _factory

    reg = ProviderRegistry()
    reg.register_type(OLLAMA_CAPABILITY, _factory)
    entry = ProviderEntry(
        name="ollama-default",
        type="ollama",
        model="llama3.1:8b",
        declared_capabilities=frozenset({Capability.CHAT, Capability.STREAMING}),
    )
    reg.register_entry(entry)

    provider = reg.build("ollama-default")

    assert provider is not None
    assert _FakeAsyncClient.constructed[-1]["base_url"] == "http://localhost:11434"


def test_ollama_factory_uses_options_endpoint(fake_httpx: types.ModuleType) -> None:
    from provider import OLLAMA_CAPABILITY, _factory

    reg = ProviderRegistry()
    reg.register_type(OLLAMA_CAPABILITY, _factory)
    entry = ProviderEntry(
        name="ollama-remote",
        type="ollama",
        model="llama3.1:8b",
        options={"endpoint": "http://ollama.internal:11434"},
        declared_capabilities=frozenset({Capability.CHAT, Capability.STREAMING}),
    )
    reg.register_entry(entry)

    provider = reg.build("ollama-remote")

    assert provider is not None
    assert _FakeAsyncClient.constructed[-1]["base_url"] == "http://ollama.internal:11434"


# ── Streaming translation ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ollama_stream_parses_ndjson(fake_httpx: types.ModuleType) -> None:
    """Each NDJSON chunk's ``message.content`` becomes an EVENT_TEXT_CHUNK,
    and the final ``done: true`` line yields EVENT_COMPLETE with token counts."""
    from provider import OllamaProvider

    lines = [
        json.dumps({"message": {"role": "assistant", "content": "Hello"}, "done": False}),
        json.dumps({"message": {"role": "assistant", "content": " world"}, "done": False}),
        json.dumps(
            {
                "message": {"role": "assistant", "content": ""},
                "done": True,
                "prompt_eval_count": 10,
                "eval_count": 2,
            }
        ),
    ]

    provider = OllamaProvider(model="llama3.1:8b")
    provider._client.stream_lines = lines

    events = [event async for event in provider.stream("hi")]

    text_events = [e for e in events if e.kind == EVENT_TEXT_CHUNK]
    complete_events = [e for e in events if e.kind == EVENT_COMPLETE]

    assert [e.text for e in text_events] == ["Hello", " world"]
    assert len(complete_events) == 1
    assert complete_events[0].input_tokens == 10
    assert complete_events[0].output_tokens == 2

    # Verify the request was POST /api/chat with stream=True.
    assert provider._client.stream_calls[0]["method"] == "POST"
    assert provider._client.stream_calls[0]["url"] == "/api/chat"
    body = provider._client.stream_calls[0]["json"]
    assert body["model"] == "llama3.1:8b"
    assert body["stream"] is True


@pytest.mark.asyncio
async def test_ollama_stream_skips_non_json_lines(
    fake_httpx: types.ModuleType,
) -> None:
    """Defensive: malformed NDJSON lines are logged and skipped, not raised."""
    from provider import OllamaProvider

    lines = [
        "",  # blank line
        "not json",
        json.dumps({"message": {"content": "ok"}, "done": False}),
        json.dumps({"done": True, "prompt_eval_count": 1, "eval_count": 1}),
    ]

    provider = OllamaProvider(model="llama3.1:8b")
    provider._client.stream_lines = lines

    events = [event async for event in provider.stream("hi")]
    text_events = [e for e in events if e.kind == EVENT_TEXT_CHUNK]
    assert [e.text for e in text_events] == ["ok"]


# ── Tool calls (native loop via complete()) ────────────────────────────


_OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files in the workspace.",
            "parameters": {"type": "object", "properties": {}},
        },
    }
]


@pytest.mark.asyncio
async def test_ollama_complete_forwards_tools_and_emits_tool_call(
    fake_httpx: types.ModuleType,
) -> None:
    """complete() forwards the tools schema and turns a streamed
    ``message.tool_calls`` (object-shaped arguments) into one EVENT_TOOL_CALL."""
    from provider import OllamaProvider

    lines = [
        json.dumps(
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "list_files", "arguments": {"path": "."}}}
                    ],
                },
                "done": True,
                "prompt_eval_count": 12,
                "eval_count": 3,
            }
        ),
    ]

    provider = OllamaProvider(model="qwen2.5:7b")
    provider._client.stream_lines = lines

    messages = [{"role": "user", "content": "list the files"}]
    events = [e async for e in provider.complete(messages, tools=_OPENAI_TOOLS)]

    # The tools schema was forwarded on the request body.
    body = provider._client.stream_calls[0]["json"]
    assert body["tools"] == _OPENAI_TOOLS

    tool_calls = [e for e in events if e.kind == EVENT_TOOL_CALL]
    assert len(tool_calls) == 1
    assert tool_calls[0].title == "list_files"
    # Arguments are serialized back to the OpenAI JSON-string shape the loop
    # parses with parse_tool_arguments().
    assert json.loads(tool_calls[0].tool_input) == {"path": "."}
    assert tool_calls[0].tool_call_id  # a stable id is always present


@pytest.mark.asyncio
async def test_ollama_complete_retries_tool_less_on_unsupported(
    fake_httpx: types.ModuleType,
) -> None:
    """A model that 400s the tools request retries tool-less and still
    completes — and the no-tools verdict is cached for later turns."""
    from provider import OllamaProvider

    ok_lines = [
        json.dumps({"message": {"content": "no tools, just text"}, "done": False}),
        json.dumps({"done": True, "prompt_eval_count": 5, "eval_count": 4}),
    ]
    provider = OllamaProvider(model="llama2:7b")
    # First stream() → 400 mentioning tool support; second → a clean turn.
    provider._client.stream_responses = [
        ([], 400, "this model does not support tools"),
        (ok_lines, 200, ""),
    ]

    messages = [{"role": "user", "content": "hi"}]
    events = [e async for e in provider.complete(messages, tools=_OPENAI_TOOLS)]

    text = "".join(e.text for e in events if e.kind == EVENT_TEXT_CHUNK)
    assert text == "no tools, just text"
    assert [e for e in events if e.kind == EVENT_TOOL_CALL] == []
    # Retry happened (two stream calls), and the model is now flagged so a
    # later turn skips the doomed first request.
    assert len(provider._client.stream_calls) == 2
    assert provider._tools_unsupported is True
    # The retry body carried no tools.
    assert "tools" not in provider._client.stream_calls[1]["json"]


@pytest.mark.asyncio
async def test_ollama_complete_unrelated_400_does_not_strip_tools(
    fake_httpx: types.ModuleType,
) -> None:
    """A non-tools 400 (e.g. bad request) must NOT be mistaken for a tools
    capability problem — it propagates instead of silently dropping tools."""
    from provider import OllamaProvider

    provider = OllamaProvider(model="qwen2.5:7b")
    provider._client.stream_responses = [([], 400, "invalid request: bad num_ctx")]

    messages = [{"role": "user", "content": "hi"}]
    with pytest.raises(RuntimeError):
        _ = [e async for e in provider.complete(messages, tools=_OPENAI_TOOLS)]
    assert provider._tools_unsupported is False
    assert len(provider._client.stream_calls) == 1


def test_to_ollama_messages_translates_tool_call_round_trip() -> None:
    """OpenAI-shaped assistant tool_calls + tool results map to Ollama's shape:
    arguments become an object, and tool results recover their tool_name."""
    from provider import _to_ollama_messages

    messages = [
        {"role": "user", "content": "list"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "list_files", "arguments": '{"path": "."}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "a.txt\nb.txt"},
    ]

    out = _to_ollama_messages(messages)

    assert out[0] == {"role": "user", "content": "list"}
    assistant = out[1]
    assert assistant["tool_calls"][0]["function"]["arguments"] == {"path": "."}
    tool_result = out[2]
    assert tool_result["role"] == "tool"
    assert tool_result["tool_name"] == "list_files"
    assert tool_result["content"] == "a.txt\nb.txt"


# ── Embeddings ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ollama_embed_returns_vectors(fake_httpx: types.ModuleType) -> None:
    from provider import OllamaProvider

    provider = OllamaProvider(model="llama3.1:8b")
    provider._client.post_payload = {"embeddings": [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]}

    out = await provider.embed(["hello", "world"])

    assert out == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    call = provider._client.post_calls[0]
    assert call["url"] == "/api/embed"
    assert call["json"]["model"] == "llama3.1:8b"
    assert call["json"]["input"] == ["hello", "world"]


@pytest.mark.asyncio
async def test_ollama_embed_empty_inputs_returns_empty(
    fake_httpx: types.ModuleType,
) -> None:
    from provider import OllamaProvider

    provider = OllamaProvider(model="llama3.1:8b")
    assert await provider.embed([]) == []
    # No HTTP call was made for empty input.
    assert provider._client.post_calls == []


@pytest.mark.asyncio
async def test_ollama_embed_uses_embedding_model_override(
    fake_httpx: types.ModuleType,
) -> None:
    from provider import OllamaProvider

    provider = OllamaProvider(
        model="llama3.1:8b",
        extra_options={"embedding_model": "nomic-embed-text"},
    )
    provider._client.post_payload = {"embeddings": [[0.1]]}

    await provider.embed(["x"])

    assert provider._client.post_calls[0]["json"]["model"] == "nomic-embed-text"


# ── Shutdown ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ollama_shutdown_closes_client(fake_httpx: types.ModuleType) -> None:
    from provider import OllamaProvider

    provider = OllamaProvider(model="llama3.1:8b")

    await provider.shutdown()

    assert provider._client.closed is True
    assert provider._history == []
