"""Unit tests for VLLMProvider.

The ``openai`` SDK is NOT installed in this dev environment. These tests
substitute a stub module into ``sys.modules`` before constructing
``VLLMProvider`` so the lazy import (inherited from ``OpenAIProvider``)
resolves to the stub.
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
from personalclaw.llm.registry import (
    CredentialMissing,
    ProviderResolutionError,
    get_default_registry,
)

# ── Fakes for the openai SDK ────────────────────────────────────────────
#
# Copied (rather than imported) from ``test_provider_openai.py`` so the
# two suites stay independent — neither test file relies on the other's
# import side effects.


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


class _FakeEmbeddings:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return types.SimpleNamespace(data=[])


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
        self.chat = _FakeChat(_FakeChatCompletions(chunks=[]))
        self.embeddings = _FakeEmbeddings()
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


# ── lazy SDK import ────────────────────────────────────────


def test_vllm_module_does_not_import_sdk_at_top_level() -> None:
    """Importing the app's provider module MUST NOT pull in ``openai`` (Property 11 —
    the SDK import is deferred to the inherited OpenAIProvider.__init__)."""
    sys.modules.pop("openai", None)
    import provider  # noqa: F401  (app-local)
    assert "openai" not in sys.modules


def test_importing_providers_package_does_not_import_sdk() -> None:
    """``import personalclaw.llm`` MUST NOT pull in ``openai``."""
    sys.modules.pop("openai", None)
    sys.modules.pop("personalclaw.providers", None)
    sys.modules.pop("personalclaw.llm.openai", None)
    sys.modules.pop("personalclaw.llm.vllm", None)
    importlib.import_module("personalclaw.providers")
    assert "openai" not in sys.modules


def test_vllm_constructor_lazy_imports_sdk(fake_openai: types.ModuleType) -> None:
    """Instantiating ``VLLMProvider`` triggers the lazy SDK import (inherited)."""
    from provider import VLLMProvider

    provider = VLLMProvider(model="meta-llama/Llama-3-8B", base_url="http://localhost:8000/v1")

    assert provider is not None
    assert _FakeAsyncOpenAI.constructed[-1]["base_url"] == "http://localhost:8000/v1"


# ── Capability descriptor + registry registration ──────────────────────


def test_vllm_capability_descriptor() -> None:
    from provider import VLLM_CAPABILITY

    assert VLLM_CAPABILITY.type == "vllm"
    expected = {
        Capability.CHAT,
        Capability.CODE_TOOLS,
        Capability.STREAMING,
        Capability.EMBEDDING,
        Capability.VISION,
    }
    assert VLLM_CAPABILITY.capabilities == frozenset(expected)
    assert VLLM_CAPABILITY.supports_tools is True
    assert VLLM_CAPABILITY.supports_streaming is True
    assert VLLM_CAPABILITY.supports_embeddings is True
    assert VLLM_CAPABILITY.supports_vision is True


def test_vllm_registers_with_default_registry() -> None:
    import provider  # noqa: F401  (app-local; registers the vllm type on import)
    cap = get_default_registry().capability_of("vllm")
    assert cap is not None
    assert cap.type == "vllm"


# ── Constructor validation ─────────────────────────────────────────────


def test_vllm_constructor_requires_base_url(fake_openai: types.ModuleType) -> None:
    from provider import VLLMProvider

    with pytest.raises(ValueError, match="base_url"):
        VLLMProvider(model="meta-llama/Llama-3-8B", base_url="")


def test_vllm_constructor_works_without_credential(fake_openai: types.ModuleType) -> None:
    """Unauth'd vLLM deployments must construct without a credential."""
    from provider import VLLMProvider

    provider = VLLMProvider(
        model="meta-llama/Llama-3-8B",
        base_url="http://localhost:8000/v1",
        credential=None,
    )

    assert provider is not None
    assert _FakeAsyncOpenAI.constructed[-1]["base_url"] == "http://localhost:8000/v1"
    # A placeholder api_key is supplied so the upstream client construction
    # succeeds; the actual value is irrelevant to vLLM unauth'd servers.
    assert _FakeAsyncOpenAI.constructed[-1]["api_key"]


def test_vllm_constructor_uses_credential_secret_when_provided(
    fake_openai: types.ModuleType,
) -> None:
    from provider import VLLMProvider

    cred = Credential(name="x", kind="api_key", secret="sk-real", source="env")
    VLLMProvider(
        model="meta-llama/Llama-3-8B",
        base_url="http://localhost:8000/v1",
        credential=cred,
    )

    assert _FakeAsyncOpenAI.constructed[-1]["api_key"] == "sk-real"


# ── Factory ────────────────────────────────────────────────────────────


def test_vllm_factory_requires_base_url_in_options(
    fake_openai: types.ModuleType,
) -> None:
    from provider import VLLM_CAPABILITY, _factory

    reg = ProviderRegistry()
    reg.register_type(VLLM_CAPABILITY, _factory)
    entry = ProviderEntry(
        name="vllm-x",
        type="vllm",
        model="meta-llama/Llama-3-8B",
        options={},  # no base_url
        declared_capabilities=frozenset({Capability.CHAT, Capability.STREAMING}),
    )
    reg.register_entry(entry)

    with pytest.raises(ProviderResolutionError, match="base_url"):
        reg.build("vllm-x")


def test_vllm_factory_builds_without_credential(fake_openai: types.ModuleType) -> None:
    """vLLM entries without a declared credential should build successfully."""
    from provider import VLLM_CAPABILITY, _factory

    reg = ProviderRegistry()
    reg.register_type(VLLM_CAPABILITY, _factory)
    entry = ProviderEntry(
        name="vllm-x",
        type="vllm",
        model="meta-llama/Llama-3-8B",
        options={"base_url": "http://localhost:8000/v1"},
        declared_capabilities=frozenset({Capability.CHAT, Capability.STREAMING}),
    )
    reg.register_entry(entry)

    provider = reg.build("vllm-x")

    assert provider is not None
    assert _FakeAsyncOpenAI.constructed[-1]["base_url"] == "http://localhost:8000/v1"


def test_vllm_factory_accepts_endpoint_key(fake_openai: types.ModuleType) -> None:
    """The Add-instance flow persists the URL under ``options.endpoint`` (the
    settingsSchema field name), NOT ``base_url``. The factory MUST accept it, or
    a UI-configured instance fails to build and the chat turn silently falls back
    to the default provider (regression: 'requires options.base_url' → misroute)."""
    from provider import VLLM_CAPABILITY, _factory

    reg = ProviderRegistry()
    reg.register_type(VLLM_CAPABILITY, _factory)
    entry = ProviderEntry(
        name="ollama-as-vllm",
        type="vllm",
        model="qwen2.5:0.5b",
        options={"endpoint": "http://127.0.0.1:11434/v1"},  # UI-persisted key
        declared_capabilities=frozenset({Capability.CHAT, Capability.STREAMING}),
    )
    reg.register_entry(entry)

    provider = reg.build("ollama-as-vllm")

    assert provider is not None
    assert _FakeAsyncOpenAI.constructed[-1]["base_url"] == "http://127.0.0.1:11434/v1"


def test_vllm_factory_default_model_used_and_not_leaked(fake_openai: types.ModuleType) -> None:
    """The Add-instance flow persists the model id under ``options.default_model``
    (settingsSchema field) with an EMPTY entry.model. The factory must (a) use
    default_model as the model, and (b) NOT leak it into extra_options → the openai
    SDK create() (regression: 'unexpected keyword argument default_model')."""
    from provider import VLLM_CAPABILITY, _factory

    reg = ProviderRegistry()
    reg.register_type(VLLM_CAPABILITY, _factory)
    entry = ProviderEntry(
        name="ollama-as-vllm",
        type="vllm",
        model="",  # pinned model empty — as persisted by the UI
        options={"endpoint": "http://127.0.0.1:11434/v1", "default_model": "qwen2.5:0.5b"},
        declared_capabilities=frozenset({Capability.CHAT, Capability.STREAMING}),
    )
    reg.register_entry(entry)

    provider = reg.build("ollama-as-vllm")

    assert provider is not None
    # (a) model resolved from default_model
    assert provider._model == "qwen2.5:0.5b"
    # (b) default_model / endpoint must NOT survive in the extra_options that reach
    # the SDK create() call.
    assert "default_model" not in provider._extra_options
    assert "endpoint" not in provider._extra_options
    assert "base_url" not in provider._extra_options


def test_vllm_factory_resolves_credential_when_declared(
    fake_openai: types.ModuleType,
) -> None:
    from provider import VLLM_CAPABILITY, _factory

    reg = ProviderRegistry()
    reg.register_type(VLLM_CAPABILITY, _factory)
    entry = ProviderEntry(
        name="vllm-auth",
        type="vllm",
        model="meta-llama/Llama-3-8B",
        options={"base_url": "https://vllm.example.com/v1"},
        credential="vllm_api_key",
        declared_capabilities=frozenset({Capability.CHAT, Capability.STREAMING}),
    )
    reg.register_entry(entry)

    store = _FakeStore(secret="sk-from-store")
    provider = reg.build("vllm-auth", credential_store=store)

    assert provider is not None
    assert store.resolved == ["vllm_api_key"]
    assert _FakeAsyncOpenAI.constructed[-1]["api_key"] == "sk-from-store"


def test_vllm_factory_raises_credential_missing_when_secret_none(
    fake_openai: types.ModuleType,
) -> None:
    from provider import VLLM_CAPABILITY, _factory

    reg = ProviderRegistry()
    reg.register_type(VLLM_CAPABILITY, _factory)
    entry = ProviderEntry(
        name="vllm-auth",
        type="vllm",
        model="meta-llama/Llama-3-8B",
        options={"base_url": "https://vllm.example.com/v1"},
        credential="vllm_api_key",
        declared_capabilities=frozenset({Capability.CHAT}),
    )
    reg.register_entry(entry)

    store = _FakeStore(secret=None)
    with pytest.raises(CredentialMissing):
        reg.build("vllm-auth", credential_store=store)


def test_vllm_factory_raises_when_credential_declared_but_no_store(
    fake_openai: types.ModuleType,
) -> None:
    from provider import VLLM_CAPABILITY, _factory

    reg = ProviderRegistry()
    reg.register_type(VLLM_CAPABILITY, _factory)
    entry = ProviderEntry(
        name="vllm-auth",
        type="vllm",
        model="meta-llama/Llama-3-8B",
        options={"base_url": "https://vllm.example.com/v1"},
        credential="vllm_api_key",
        declared_capabilities=frozenset({Capability.CHAT}),
    )
    reg.register_entry(entry)

    with pytest.raises(CredentialMissing):
        reg.build("vllm-auth")


# ── Streaming inherits OpenAI translation ──────────────────────────────


@pytest.mark.asyncio
async def test_vllm_stream_inherits_openai_translation(
    fake_openai: types.ModuleType,
) -> None:
    """Subclass must behave identically to OpenAIProvider on the streaming path."""
    from provider import VLLMProvider

    chunks = [
        _FakeChunk(choices=[_FakeChoice(delta=_FakeDelta(content="Hello"))]),
        _FakeChunk(choices=[_FakeChoice(delta=_FakeDelta(content=" vLLM"))]),
        _FakeChunk(
            choices=[_FakeChoice(delta=_FakeDelta(content=None), finish_reason="stop")],
            usage=_FakeUsage(prompt_tokens=5, completion_tokens=2),
        ),
    ]

    provider = VLLMProvider(
        model="meta-llama/Llama-3-8B",
        base_url="http://localhost:8000/v1",
    )
    provider._client.chat = _FakeChat(_FakeChatCompletions(chunks=chunks))

    events = [event async for event in provider.stream("hi")]

    text_events = [e for e in events if e.kind == EVENT_TEXT_CHUNK]
    complete_events = [e for e in events if e.kind == EVENT_COMPLETE]

    assert [e.text for e in text_events] == ["Hello", " vLLM"]
    assert len(complete_events) == 1
    assert complete_events[0].input_tokens == 5
    assert complete_events[0].output_tokens == 2
