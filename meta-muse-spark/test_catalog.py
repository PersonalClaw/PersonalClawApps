"""Catalog tests for the meta-muse-spark app — a static one-model catalog.

``list_models`` is static (it runs on hot Settings GETs and must never touch the
network); ``test_connection`` is the only wire probe, and it is stubbed here. The
catalog tests double as the regression rail for the registry calling convention:
``ProviderRegistry.build_catalog`` invokes the factory as ``factory(options,
model=...)`` and swallows a mismatch fail-soft, so a factory with the wrong
signature ships as a provider that silently has no discovery and no working
"Test connection" — exactly the defect this app shipped with.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import provider as prov  # app-local; registers type + catalog on import

from personalclaw.llm.catalog import ModelCatalog, ModelManager
from personalclaw.llm.registry import get_default_registry


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def no_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("META_MODEL_API_KEY", raising=False)


def test_catalog_is_plain_catalog() -> None:
    cat = prov.create_catalog({})
    assert isinstance(cat, ModelCatalog)
    # A hosted API is NOT a manager (no local pull/delete).
    assert not isinstance(cat, ModelManager)


def test_catalog_factory_honors_the_registry_calling_convention() -> None:
    """``build_catalog`` calls ``factory(options_dict, model=...)``; a factory that
    cannot accept that shape is swallowed fail-soft and the provider loses
    discovery + Test connection silently."""
    factory = get_default_registry().catalog_of("meta_muse_spark")
    assert factory is not None
    cat = factory({"api_key": "mk-x"}, model="muse-spark-1.1")
    assert isinstance(cat, ModelCatalog)


def test_static_catalog_lists_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """``list_models`` runs on hot Settings GETs — it must never touch the wire."""

    async def _explode(*a: Any, **k: Any):
        raise AssertionError("list_models must not fetch")

    monkeypatch.setattr("personalclaw.llm.catalog.openai_compatible_list_models", _explode)
    cat = prov.create_catalog({"api_key": "mk-x"})
    models = _run(cat.list_models())
    assert [m.id for m in models] == ["muse-spark-1.1"]
    assert set(models[0].capabilities) == {"chat", "image_modality", "streaming"}


# ── Connectivity probe ───────────────────────────────────────────────────────


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
