"""Unit tests for OpenAIProvider.

The ``openai`` SDK is NOT installed in this dev environment. These tests
substitute a stub module into ``sys.modules`` before constructing
``OpenAIProvider`` so the lazy import resolves to the stub.
"""

import importlib
import sys
import types
from typing import Any

import pytest

from personalclaw.llm import (
    Capability,
    Credential,
    ProviderEntry,
    ProviderRegistry,
)
from personalclaw.llm.base import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
)
from personalclaw.llm.registry import CredentialMissing, get_default_registry

# ── Fakes for the openai SDK ────────────────────────────────────────────


class _FakeStream:
    """Async iterable that yields pre-canned chunks."""

    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = chunks

    def __aiter__(self) -> "_FakeStream":
        self._iter = iter(self._chunks)
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class _FakeChoice:
    def __init__(self, delta: Any, finish_reason: str | None = None) -> None:
        self.delta = delta
        self.finish_reason = finish_reason


class _FakeDelta:
    def __init__(self, content: str | None = None, tool_calls: list[Any] | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _FakeChunk:
    def __init__(
        self,
        choices: list[_FakeChoice] | None = None,
        usage: Any | None = None,
    ) -> None:
        self.choices = choices or []
        self.usage = usage


class _FakeUsage:
    def __init__(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _FakeChatCompletions:
    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = chunks
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _FakeStream:
        self.calls.append(kwargs)
        return _FakeStream(self._chunks)


class _FakeChat:
    def __init__(self, completions: _FakeChatCompletions) -> None:
        self.completions = completions


class _FakeEmbeddingResponse:
    def __init__(self, vectors: list[list[float]]) -> None:
        self.data = [types.SimpleNamespace(embedding=list(v)) for v in vectors]


class _FakeEmbeddings:
    def __init__(self, vectors: list[list[float]]) -> None:
        self._vectors = vectors
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _FakeEmbeddingResponse:
        self.calls.append(kwargs)
        return _FakeEmbeddingResponse(self._vectors)


class _FakeAsyncOpenAI:
    constructed: list[dict[str, Any]] = []

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
    ) -> None:
        type(self).constructed.append({"api_key": api_key, "base_url": base_url})
        self.api_key = api_key
        self.base_url = base_url
        # Default no-op completions/embeddings; tests override.
        self.chat = _FakeChat(_FakeChatCompletions(chunks=[]))
        self.embeddings = _FakeEmbeddings(vectors=[])
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_openai(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Install a fake ``openai`` module into ``sys.modules``."""
    fake = types.ModuleType("openai")
    fake.AsyncOpenAI = _FakeAsyncOpenAI  # type: ignore[attr-defined]
    _FakeAsyncOpenAI.constructed = []
    monkeypatch.setitem(sys.modules, "openai", fake)
    return fake


# ── lazy SDK import ────────────────────────────────────────


def test_openai_module_does_not_import_sdk_at_top_level() -> None:
    """Importing ``personalclaw.providers.openai`` MUST NOT pull in ``openai``."""
    # Drop any cached entry first to make the assertion meaningful.
    sys.modules.pop("openai", None)
    sys.modules.pop("personalclaw.llm.openai", None)
    importlib.import_module("personalclaw.llm.openai")
    assert "openai" not in sys.modules


def test_importing_providers_package_does_not_import_sdk() -> None:
    """``import personalclaw.llm`` MUST NOT pull in ``openai``."""
    sys.modules.pop("openai", None)
    sys.modules.pop("personalclaw.providers", None)
    sys.modules.pop("personalclaw.llm.openai", None)
    importlib.import_module("personalclaw.providers")
    assert "openai" not in sys.modules


def test_openai_constructor_lazy_imports_sdk(fake_openai: types.ModuleType) -> None:
    """Instantiating ``OpenAIProvider`` triggers the lazy SDK import."""
    from personalclaw.sdk.model import OpenAIProvider

    cred = Credential(name="x", kind="api_key", secret="sk-test", source="env")
    provider = OpenAIProvider(model="gpt-4o-mini", credential=cred)

    assert provider is not None
    assert _FakeAsyncOpenAI.constructed[-1]["api_key"] == "sk-test"
    assert _FakeAsyncOpenAI.constructed[-1]["base_url"] is None


# ── Capability descriptor + registry registration ──────────────────────


def test_openai_capability_descriptor() -> None:
    from provider import OPENAI_CAPABILITY

    assert OPENAI_CAPABILITY.type == "openai"
    expected = {
        Capability.CHAT,
        Capability.CODE_TOOLS,
        Capability.STREAMING,
        Capability.EMBEDDING,
        Capability.VISION,
    }
    assert OPENAI_CAPABILITY.capabilities == frozenset(expected)
    assert OPENAI_CAPABILITY.supports_tools is True
    assert OPENAI_CAPABILITY.supports_streaming is True
    assert OPENAI_CAPABILITY.supports_embeddings is True
    assert OPENAI_CAPABILITY.supports_vision is True


def test_openai_registers_with_default_registry() -> None:
    importlib.import_module("personalclaw.llm.openai")
    cap = get_default_registry().capability_of("openai")
    assert cap is not None
    assert cap.type == "openai"


# ── Factory + credential resolution ────────────────────────────────────


class _FakeStore:
    def __init__(self, secret: str | None) -> None:
        self._secret = secret
        self.resolved: list[str] = []

    def resolve(self, name: str) -> Credential:
        self.resolved.append(name)
        return Credential(
            name=name,
            kind="api_key",
            secret=self._secret,
            source="env" if self._secret else "none",
        )


def test_openai_factory_resolves_credential_via_store(
    fake_openai: types.ModuleType,
) -> None:
    from provider import OPENAI_CAPABILITY, _factory

    reg = ProviderRegistry()
    reg.register_type(OPENAI_CAPABILITY, _factory)
    entry = ProviderEntry(
        name="openai-default",
        type="openai",
        model="gpt-4o-mini",
        credential="openai_api_key",
        declared_capabilities=frozenset({Capability.CHAT, Capability.STREAMING}),
    )
    reg.register_entry(entry)

    store = _FakeStore(secret="sk-from-store")
    provider = reg.build("openai-default", credential_store=store)

    assert provider is not None
    assert store.resolved == ["openai_api_key"]
    assert _FakeAsyncOpenAI.constructed[-1]["api_key"] == "sk-from-store"


def test_openai_factory_raises_credential_missing_when_secret_none(
    fake_openai: types.ModuleType,
) -> None:
    from provider import OPENAI_CAPABILITY, _factory

    reg = ProviderRegistry()
    reg.register_type(OPENAI_CAPABILITY, _factory)
    entry = ProviderEntry(
        name="openai-default",
        type="openai",
        model="gpt-4o-mini",
        credential="openai_api_key",
        declared_capabilities=frozenset({Capability.CHAT}),
    )
    reg.register_entry(entry)

    store = _FakeStore(secret=None)
    with pytest.raises(CredentialMissing):
        reg.build("openai-default", credential_store=store)


def test_openai_factory_raises_when_credential_declared_but_no_store(
    fake_openai: types.ModuleType,
) -> None:
    from provider import OPENAI_CAPABILITY, _factory

    reg = ProviderRegistry()
    reg.register_type(OPENAI_CAPABILITY, _factory)
    entry = ProviderEntry(
        name="openai-default",
        type="openai",
        model="gpt-4o-mini",
        credential="openai_api_key",
        declared_capabilities=frozenset({Capability.CHAT}),
    )
    reg.register_entry(entry)

    with pytest.raises(CredentialMissing):
        reg.build("openai-default")


# ── Streaming translation ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_stream_translates_text_deltas(
    fake_openai: types.ModuleType,
) -> None:
    from personalclaw.sdk.model import OpenAIProvider

    chunks = [
        _FakeChunk(choices=[_FakeChoice(delta=_FakeDelta(content="Hello"))]),
        _FakeChunk(choices=[_FakeChoice(delta=_FakeDelta(content=" world"))]),
        _FakeChunk(
            choices=[_FakeChoice(delta=_FakeDelta(content=None), finish_reason="stop")],
            usage=_FakeUsage(prompt_tokens=10, completion_tokens=2),
        ),
    ]

    cred = Credential(name="x", kind="api_key", secret="sk-test", source="env")
    provider = OpenAIProvider(model="gpt-4o-mini", credential=cred)
    provider._client.chat = _FakeChat(_FakeChatCompletions(chunks=chunks))

    events = [event async for event in provider.stream("hi")]

    text_events = [e for e in events if e.kind == EVENT_TEXT_CHUNK]
    complete_events = [e for e in events if e.kind == EVENT_COMPLETE]

    assert [e.text for e in text_events] == ["Hello", " world"]
    assert len(complete_events) == 1
    assert complete_events[0].input_tokens == 10
    assert complete_events[0].output_tokens == 2


# ── Embeddings ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_embed_returns_vectors(fake_openai: types.ModuleType) -> None:
    from personalclaw.sdk.model import OpenAIProvider

    cred = Credential(name="x", kind="api_key", secret="sk-test", source="env")
    # The embedding model is supplied via extra_options (the app threads the
    # embedding use-case binding through as embedding_model — it is NOT hardcoded
    # in the base provider, whose default is "" so a non-OpenAI compatible endpoint
    # never gets an OpenAI-specific id silently).
    provider = OpenAIProvider(
        model="gpt-4o-mini",
        credential=cred,
        extra_options={"embedding_model": "text-embedding-3-small"},
    )
    fake_embeddings = _FakeEmbeddings(vectors=[[0.1, 0.2, 0.3]])
    provider._client.embeddings = fake_embeddings

    out = await provider.embed(["hello"])

    assert out == [[0.1, 0.2, 0.3]]
    assert fake_embeddings.calls[0]["model"] == "text-embedding-3-small"
    assert fake_embeddings.calls[0]["input"] == ["hello"]


@pytest.mark.asyncio
async def test_openai_embed_empty_inputs_returns_empty(
    fake_openai: types.ModuleType,
) -> None:
    from personalclaw.sdk.model import OpenAIProvider

    cred = Credential(name="x", kind="api_key", secret="sk-test", source="env")
    provider = OpenAIProvider(model="gpt-4o-mini", credential=cred)

    assert await provider.embed([]) == []


# ── Shutdown ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_shutdown_closes_client(fake_openai: types.ModuleType) -> None:
    from personalclaw.sdk.model import OpenAIProvider

    cred = Credential(name="x", kind="api_key", secret="sk-test", source="env")
    provider = OpenAIProvider(model="gpt-4o-mini", credential=cred)

    await provider.shutdown()

    assert provider._client.closed is True
    assert provider._history == []
