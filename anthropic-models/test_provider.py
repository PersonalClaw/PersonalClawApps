"""Unit tests for AnthropicProvider.

The ``anthropic`` SDK is NOT installed in this dev environment. These
tests substitute a stub module into ``sys.modules`` before constructing
``AnthropicProvider`` so the lazy import resolves to the stub.
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
    EVENT_TOOL_CALL,
)
from personalclaw.llm.registry import CredentialMissing, get_default_registry

# ── Fakes for the anthropic SDK ─────────────────────────────────────────


class _FakeStreamIter:
    """Async iterator yielding pre-canned events."""

    def __init__(self, events: list[Any]) -> None:
        self._events = events

    def __aiter__(self) -> "_FakeStreamIter":
        self._iter = iter(self._events)
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class _FakeStreamCM:
    """``async with`` wrapper around a :class:`_FakeStreamIter`."""

    def __init__(self, events: list[Any]) -> None:
        self._events = events

    async def __aenter__(self) -> _FakeStreamIter:
        return _FakeStreamIter(self._events)

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None


class _FakeMessages:
    def __init__(self, stream_events: list[Any]) -> None:
        self._events = stream_events
        self.calls: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any) -> _FakeStreamCM:
        self.calls.append(kwargs)
        return _FakeStreamCM(self._events)


class _FakeAsyncAnthropic:
    constructed: list[dict[str, Any]] = []

    def __init__(self, *, api_key: str, base_url: str | None = None) -> None:
        type(self).constructed.append({"api_key": api_key, "base_url": base_url})
        self.api_key = api_key
        self.base_url = base_url
        # Default empty stream; tests override.
        self.messages = _FakeMessages(stream_events=[])
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_anthropic(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Install a fake ``anthropic`` module into ``sys.modules``."""
    fake = types.ModuleType("anthropic")
    fake.AsyncAnthropic = _FakeAsyncAnthropic  # type: ignore[attr-defined]
    _FakeAsyncAnthropic.constructed = []
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    return fake


# ── lazy SDK import ────────────────────────────────────────


def test_anthropic_module_does_not_import_sdk_at_top_level() -> None:
    """Importing ``personalclaw.providers.anthropic`` MUST NOT pull in ``anthropic``."""
    sys.modules.pop("anthropic", None)
    sys.modules.pop("personalclaw.llm.anthropic", None)
    importlib.import_module("personalclaw.llm.anthropic")
    assert "anthropic" not in sys.modules


def test_importing_providers_package_does_not_import_sdk() -> None:
    """``import personalclaw.llm`` MUST NOT pull in ``anthropic``."""
    sys.modules.pop("anthropic", None)
    sys.modules.pop("personalclaw.providers", None)
    sys.modules.pop("personalclaw.llm.anthropic", None)
    importlib.import_module("personalclaw.providers")
    assert "anthropic" not in sys.modules


def test_anthropic_constructor_lazy_imports_sdk(fake_anthropic: types.ModuleType) -> None:
    """Instantiating ``AnthropicProvider`` triggers the lazy SDK import."""
    from personalclaw.sdk.model import AnthropicProvider

    cred = Credential(name="x", kind="api_key", secret="sk-ant-test", source="env")
    provider = AnthropicProvider(model="claude-3-5-sonnet-20241022", credential=cred)

    assert provider is not None
    assert _FakeAsyncAnthropic.constructed[-1]["api_key"] == "sk-ant-test"
    # No base_url supplied → SDK uses its default endpoint.
    assert _FakeAsyncAnthropic.constructed[-1]["base_url"] is None


def test_anthropic_forwards_base_url_for_compatible_endpoints(
    fake_anthropic: types.ModuleType,
) -> None:
    """A custom ``base_url`` (anthropic-compatible proxy/gateway) reaches the SDK."""
    from personalclaw.sdk.model import AnthropicProvider

    cred = Credential(name="x", kind="api_key", secret="sk-ant-test", source="env")
    provider = AnthropicProvider(
        model="claude-3-5-sonnet-20241022",
        credential=cred,
        base_url="https://anthropic-proxy.example.com/v1",
    )

    assert provider is not None
    assert (
        _FakeAsyncAnthropic.constructed[-1]["base_url"]
        == "https://anthropic-proxy.example.com/v1"
    )


def test_anthropic_factory_reads_base_url_from_options(
    fake_anthropic: types.ModuleType,
) -> None:
    """The factory pulls ``base_url`` from ``entry.options`` and forwards it."""
    from provider import ANTHROPIC_CAPABILITY, _factory

    reg = ProviderRegistry()
    reg.register_type(ANTHROPIC_CAPABILITY, _factory)
    entry = ProviderEntry(
        name="anthropic-compat",
        type="anthropic",
        model="claude-3-5-sonnet-20241022",
        credential="anthropic_api_key",
        options={"base_url": "https://gateway.internal/anthropic"},
        declared_capabilities=frozenset({Capability.CHAT, Capability.STREAMING}),
    )
    reg.register_entry(entry)

    store = _FakeStore(secret="sk-ant-from-store")
    provider = reg.build("anthropic-compat", credential_store=store)

    assert provider is not None
    assert (
        _FakeAsyncAnthropic.constructed[-1]["base_url"]
        == "https://gateway.internal/anthropic"
    )


# ── Capability descriptor + registry registration ──────────────────────


def test_anthropic_capability_descriptor() -> None:
    from provider import ANTHROPIC_CAPABILITY

    assert ANTHROPIC_CAPABILITY.type == "anthropic"
    expected = {
        Capability.CHAT,
        Capability.CODE_TOOLS,
        Capability.STREAMING,
        Capability.VISION,
    }
    assert ANTHROPIC_CAPABILITY.capabilities == frozenset(expected)
    assert Capability.EMBEDDING not in ANTHROPIC_CAPABILITY.capabilities
    assert ANTHROPIC_CAPABILITY.supports_tools is True
    assert ANTHROPIC_CAPABILITY.supports_streaming is True
    assert ANTHROPIC_CAPABILITY.supports_embeddings is False
    assert ANTHROPIC_CAPABILITY.supports_vision is True


def test_anthropic_registers_with_default_registry() -> None:
    importlib.import_module("personalclaw.llm.anthropic")
    cap = get_default_registry().capability_of("anthropic")
    assert cap is not None
    assert cap.type == "anthropic"


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


def test_anthropic_factory_resolves_credential_via_store(
    fake_anthropic: types.ModuleType,
) -> None:
    from provider import ANTHROPIC_CAPABILITY, _factory

    reg = ProviderRegistry()
    reg.register_type(ANTHROPIC_CAPABILITY, _factory)
    entry = ProviderEntry(
        name="anthropic-default",
        type="anthropic",
        model="claude-3-5-sonnet-20241022",
        credential="anthropic_api_key",
        declared_capabilities=frozenset({Capability.CHAT, Capability.STREAMING}),
    )
    reg.register_entry(entry)

    store = _FakeStore(secret="sk-ant-from-store")
    provider = reg.build("anthropic-default", credential_store=store)

    assert provider is not None
    assert store.resolved == ["anthropic_api_key"]
    assert _FakeAsyncAnthropic.constructed[-1]["api_key"] == "sk-ant-from-store"


def test_anthropic_factory_raises_credential_missing_when_secret_none(
    fake_anthropic: types.ModuleType,
) -> None:
    from provider import ANTHROPIC_CAPABILITY, _factory

    reg = ProviderRegistry()
    reg.register_type(ANTHROPIC_CAPABILITY, _factory)
    entry = ProviderEntry(
        name="anthropic-default",
        type="anthropic",
        model="claude-3-5-sonnet-20241022",
        credential="anthropic_api_key",
        declared_capabilities=frozenset({Capability.CHAT}),
    )
    reg.register_entry(entry)

    store = _FakeStore(secret=None)
    with pytest.raises(CredentialMissing):
        reg.build("anthropic-default", credential_store=store)


def test_anthropic_factory_raises_when_credential_declared_but_no_store(
    fake_anthropic: types.ModuleType,
) -> None:
    from provider import ANTHROPIC_CAPABILITY, _factory

    reg = ProviderRegistry()
    reg.register_type(ANTHROPIC_CAPABILITY, _factory)
    entry = ProviderEntry(
        name="anthropic-default",
        type="anthropic",
        model="claude-3-5-sonnet-20241022",
        credential="anthropic_api_key",
        declared_capabilities=frozenset({Capability.CHAT}),
    )
    reg.register_entry(entry)

    with pytest.raises(CredentialMissing):
        reg.build("anthropic-default")


# ── create_provider (app-factory path) — no hardcoded default model ─────


def test_create_provider_unpinned_uses_catalog_default_not_literal(
    fake_anthropic: types.ModuleType,
) -> None:
    """An unpinned instance config must resolve its model from the curated catalog
    (via ``_pick_default_model``), never from a stale hardcoded literal — the
    de-hardcode directive. The removed literal was ``claude-sonnet-4-20250514``."""
    from provider import _pick_default_model, create_provider

    provider = create_provider({"api_key": "sk-ant-test"})

    assert provider._model == _pick_default_model()
    assert provider._model == "claude-opus-4-8"
    assert provider._model != "claude-sonnet-4-20250514"  # the removed hardcode


def test_create_provider_honors_explicit_model(
    fake_anthropic: types.ModuleType,
) -> None:
    """A pinned ``model`` (or ``default_model``) in the config still wins."""
    from provider import create_provider

    pinned = create_provider({"api_key": "sk-ant-test", "model": "claude-sonnet-5"})
    assert pinned._model == "claude-sonnet-5"

    fallback = create_provider({"api_key": "sk-ant-test", "default_model": "claude-haiku-4-5"})
    assert fallback._model == "claude-haiku-4-5"


# ── Streaming translation ──────────────────────────────────────────────


def _ms_event(input_tokens: int = 0, output_tokens: int = 0) -> types.SimpleNamespace:
    """Build a fake ``message_start`` event."""
    usage = types.SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    message = types.SimpleNamespace(usage=usage)
    return types.SimpleNamespace(type="message_start", message=message)


def _content_block_start_text(index: int) -> types.SimpleNamespace:
    block = types.SimpleNamespace(type="text")
    return types.SimpleNamespace(type="content_block_start", index=index, content_block=block)


def _content_block_start_tool(index: int, tool_id: str, name: str) -> types.SimpleNamespace:
    block = types.SimpleNamespace(type="tool_use", id=tool_id, name=name)
    return types.SimpleNamespace(type="content_block_start", index=index, content_block=block)


def _text_delta(index: int, text: str) -> types.SimpleNamespace:
    delta = types.SimpleNamespace(type="text_delta", text=text)
    return types.SimpleNamespace(type="content_block_delta", index=index, delta=delta)


def _input_json_delta(index: int, partial: str) -> types.SimpleNamespace:
    delta = types.SimpleNamespace(type="input_json_delta", partial_json=partial)
    return types.SimpleNamespace(type="content_block_delta", index=index, delta=delta)


def _content_block_stop(index: int) -> types.SimpleNamespace:
    return types.SimpleNamespace(type="content_block_stop", index=index)


def _message_delta(output_tokens: int) -> types.SimpleNamespace:
    usage = types.SimpleNamespace(output_tokens=output_tokens)
    delta = types.SimpleNamespace(stop_reason="end_turn")
    return types.SimpleNamespace(type="message_delta", delta=delta, usage=usage)


def _message_stop() -> types.SimpleNamespace:
    return types.SimpleNamespace(type="message_stop")


@pytest.mark.asyncio
async def test_anthropic_stream_translates_text_deltas(
    fake_anthropic: types.ModuleType,
) -> None:
    from personalclaw.sdk.model import AnthropicProvider

    events = [
        _ms_event(input_tokens=10),
        _content_block_start_text(0),
        _text_delta(0, "Hello"),
        _text_delta(0, " world"),
        _content_block_stop(0),
        _message_delta(output_tokens=2),
        _message_stop(),
    ]

    cred = Credential(name="x", kind="api_key", secret="sk-ant-test", source="env")
    provider = AnthropicProvider(model="claude-3-5-sonnet-20241022", credential=cred)
    provider._client.messages = _FakeMessages(stream_events=events)

    out = [event async for event in provider.stream("hi")]

    text_events = [e for e in out if e.kind == EVENT_TEXT_CHUNK]
    complete_events = [e for e in out if e.kind == EVENT_COMPLETE]

    assert [e.text for e in text_events] == ["Hello", " world"]
    assert len(complete_events) == 1
    assert complete_events[0].input_tokens == 10
    assert complete_events[0].output_tokens == 2


@pytest.mark.asyncio
async def test_anthropic_stream_translates_tool_use(
    fake_anthropic: types.ModuleType,
) -> None:
    from personalclaw.sdk.model import AnthropicProvider

    events = [
        _ms_event(input_tokens=5),
        _content_block_start_tool(0, tool_id="toolu_1", name="get_weather"),
        _input_json_delta(0, '{"city":'),
        _input_json_delta(0, '"sf"}'),
        _content_block_stop(0),
        _message_delta(output_tokens=3),
        _message_stop(),
    ]

    cred = Credential(name="x", kind="api_key", secret="sk-ant-test", source="env")
    provider = AnthropicProvider(model="claude-3-5-sonnet-20241022", credential=cred)
    provider._client.messages = _FakeMessages(stream_events=events)

    out = [event async for event in provider.stream("call the tool")]

    tool_events = [e for e in out if e.kind == EVENT_TOOL_CALL]
    assert len(tool_events) == 1
    assert tool_events[0].tool_call_id == "toolu_1"
    assert tool_events[0].title == "get_weather"
    assert tool_events[0].tool_input == '{"city":"sf"}'

    complete_events = [e for e in out if e.kind == EVENT_COMPLETE]
    assert len(complete_events) == 1
    assert complete_events[0].input_tokens == 5
    assert complete_events[0].output_tokens == 3


# ── Shutdown ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_anthropic_shutdown_closes_client(fake_anthropic: types.ModuleType) -> None:
    from personalclaw.sdk.model import AnthropicProvider

    cred = Credential(name="x", kind="api_key", secret="sk-ant-test", source="env")
    provider = AnthropicProvider(model="claude-3-5-sonnet-20241022", credential=cred)

    await provider.shutdown()

    assert provider._client.closed is True
    assert provider._history == []
