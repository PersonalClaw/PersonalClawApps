"""Unit tests for the Meta Muse Spark provider app.

The cohort contract: every model-provider bundle pins its OWN seams — capability
descriptor, registry registration, config→provider plumbing (model/endpoint/key
precedence, env fallback), and the discovery catalog — with the vendor SDK
stubbed into ``sys.modules`` (CI installs no vendor SDKs; the ``openai`` client
is constructed inside ``OpenAIProvider.__init__``, so the stub must land first).

The catalog tests double as the regression rail for the registry calling
convention: ``ProviderRegistry.build_catalog`` invokes the factory as
``factory(options, model=...)`` and swallows a mismatch fail-soft, so a factory
with the wrong signature ships as a provider that silently has no discovery and
no working "Test connection" — exactly the defect this app shipped with.
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import pytest

import provider as prov  # app-local; registers type + catalog on import

from personalclaw.llm.catalog import ModelCatalog, ModelManager
from personalclaw.llm.registry import CredentialMissing, get_default_registry


def _run(coro):
    return asyncio.run(coro)


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


def test_catalog_factory_honors_the_registry_calling_convention() -> None:
    """``build_catalog`` calls ``factory(options_dict, model=...)``; a factory that
    cannot accept that shape is swallowed fail-soft and the provider loses
    discovery + Test connection silently."""
    factory = get_default_registry().catalog_of("meta_muse_spark")
    assert factory is not None
    cat = factory({"api_key": "mk-x"}, model="muse-spark-1.1")
    assert isinstance(cat, ModelCatalog)
    # A hosted API is NOT a manager (no local pull/delete).
    assert not isinstance(cat, ModelManager)


def test_static_catalog_lists_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """``list_models`` runs on hot Settings GETs — it must never touch the wire."""

    async def _explode(*a: Any, **k: Any):
        raise AssertionError("list_models must not fetch")

    monkeypatch.setattr("personalclaw.llm.catalog.openai_compatible_list_models", _explode)
    cat = prov.create_catalog({"api_key": "mk-x"})
    models = _run(cat.list_models())
    assert [m.id for m in models] == ["muse-spark-1.1"]
    assert set(models[0].capabilities) == {"chat", "image_modality", "streaming"}


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


# ── Catalog connectivity probe ───────────────────────────────────────────────


def _stub_discovery(monkeypatch: pytest.MonkeyPatch, models: list[Any]) -> None:
    async def _fake(endpoint: str, api_key: str, *, default_base: str = "") -> list[Any]:
        return models

    # Patch the name the app calls (imported into the provider module).
    monkeypatch.setattr(prov, "openai_compatible_list_models", _fake)


def test_connection_without_key_fails_before_network(no_env_key: None) -> None:
    res = _run(prov.create_catalog({}).test_connection())
    assert res.ok is False
    assert "key" in res.detail.lower()


def test_connection_ok_counts_models(no_env_key: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_discovery(monkeypatch, [object(), object()])
    res = _run(prov.create_catalog({"api_key": "mk-x"}).test_connection())
    assert res.ok is True
    assert res.model_count == 2


def test_connection_reports_empty_discovery(
    no_env_key: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_discovery(monkeypatch, [])
    res = _run(prov.create_catalog({"api_key": "mk-bad"}).test_connection())
    assert res.ok is False
    assert "no models" in res.detail.lower()
