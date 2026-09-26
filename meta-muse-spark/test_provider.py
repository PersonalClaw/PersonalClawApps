"""Unit tests for the Meta Muse Spark provider app.

The cohort contract: every model-provider bundle pins its OWN seams — capability
descriptor, registry registration, and the config→provider plumbing
(model/endpoint/key precedence, env fallback) — with the vendor SDK stubbed into
``sys.modules`` (CI installs no vendor SDKs; the ``openai`` client is constructed
inside ``OpenAIProvider.__init__``, so the stub must land first). The discovery
catalog has its own file, ``test_catalog.py``, like every sibling model app.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

import provider as prov  # app-local; registers type + catalog on import

from personalclaw.llm.registry import CredentialMissing


# ── Fake openai SDK (constructor recorder) ───────────────────────────────────


class _FakeAsyncOpenAI:
    constructed: list[dict[str, Any]] = []

    def __init__(self, *, api_key: str, base_url: str | None = None) -> None:
        type(self).constructed.append({"api_key": api_key, "base_url": base_url})
        self.api_key = api_key
        self.base_url = base_url

    async def close(self) -> None:
        pass


@pytest.fixture
def fake_openai(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    fake = types.ModuleType("openai")
    fake.AsyncOpenAI = _FakeAsyncOpenAI  # type: ignore[attr-defined]
    _FakeAsyncOpenAI.constructed = []
    monkeypatch.setitem(sys.modules, "openai", fake)
    return fake


@pytest.fixture
def no_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("META_MODEL_API_KEY", raising=False)


# ── Capability descriptor + registration ─────────────────────────────────────


def test_capability_descriptor() -> None:
    cap = prov.META_CAPABILITY
    assert cap.type == "meta_muse_spark"
    assert cap.supports_vision is True
    assert cap.supports_embeddings is False
    assert cap.max_context_tokens == 1_048_576


def test_capability_matches_what_the_provider_implements() -> None:
    """The manifest, the ProviderCapability and the ModelInfo rows must tell the
    same story: chat + streaming + vision, and NO tools support — none is
    implemented or declared anywhere in this bundle."""
    cap = prov.META_CAPABILITY
    assert {c.value for c in cap.capabilities} == {"chat", "streaming", "vision"}
    assert cap.supports_tools is False


# ── create_provider: config → provider plumbing ──────────────────────────────


def test_create_provider_defaults(fake_openai: types.ModuleType, no_env_key: None) -> None:
    p = prov.create_provider({"api_key": "mk-x"})
    assert p._model == "muse-spark-1.1"
    last = _FakeAsyncOpenAI.constructed[-1]
    assert last["api_key"] == "mk-x"
    assert last["base_url"] == prov.META_BASE_URL


def test_create_provider_config_overrides(fake_openai: types.ModuleType, no_env_key: None) -> None:
    p = prov.create_provider(
        {"api_key": "mk-cfg", "model": "muse-spark-next", "endpoint": "https://alt.example/v1"}
    )
    assert p._model == "muse-spark-next"
    assert _FakeAsyncOpenAI.constructed[-1]["base_url"] == "https://alt.example/v1"


def test_create_provider_env_fallback(
    fake_openai: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("META_MODEL_API_KEY", "mk-env")
    prov.create_provider({})
    assert _FakeAsyncOpenAI.constructed[-1]["api_key"] == "mk-env"


def test_create_provider_without_any_key_raises(
    fake_openai: types.ModuleType, no_env_key: None
) -> None:
    """No config key + no env key is a configuration error, not a silent client."""
    with pytest.raises(CredentialMissing):
        prov.create_provider({})


# ── Per-call sampling settings (best-of-N) ───────────────────────────────────


class _Stream:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class _Completions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _Stream:
        self.calls.append(kwargs)
        return _Stream()


def _meta_built(options: dict, **build_kwargs: Any):
    from personalclaw.llm.registry import ProviderEntry

    entry = ProviderEntry(name="meta-x", type="meta", model="muse-spark-1.1", options=options)
    return prov._factory(entry=entry, **build_kwargs)


@pytest.mark.asyncio
async def test_a_per_call_temperature_and_output_budget_reach_the_request(
    fake_openai: types.ModuleType, no_env_key: None
) -> None:
    """The factory hard-wired ``max_tokens=None`` and dropped the ``temperature`` build kwarg,
    so best-of-N sampled N copies of one answer at the endpoint's default."""
    provider = _meta_built({"api_key": "mk-test"}, temperature=0.6, max_tokens=321)
    assert provider.sampling_temperature == 0.6

    completions = _Completions()
    provider._client.chat = types.SimpleNamespace(completions=completions)
    _ = [event async for event in provider.stream("hi")]

    assert completions.calls[-1]["temperature"] == 0.6
    assert completions.calls[-1]["max_tokens"] == 321


def test_no_per_call_settings_leave_the_endpoint_defaults(
    fake_openai: types.ModuleType, no_env_key: None
) -> None:
    provider = _meta_built({"api_key": "mk-test"})
    assert provider.sampling_temperature is None
    assert provider._max_tokens is None
