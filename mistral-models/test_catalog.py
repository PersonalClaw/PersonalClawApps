"""Catalog tests for the mistral app — live /v1/models discovery only (no hardcoded
fallback catalog; when discovery fails the picker is honestly empty)."""

from __future__ import annotations

import asyncio

import provider as prov  # app-local; registers on import

from personalclaw.llm.catalog import ModelCatalog, ModelManager


def _run(coro):
    return asyncio.run(coro)


class _FakeFetchResponse:
    def __init__(self, status, payload):
        import json
        self.status = status
        self.text = json.dumps(payload)


def test_catalog_is_plain_catalog():
    cat = prov.create_catalog({})
    assert isinstance(cat, ModelCatalog)
    assert not isinstance(cat, ModelManager)  # hosted API, no local model management


def test_empty_list_when_endpoint_unreachable(monkeypatch):
    # No live models (endpoint 500) -> EMPTY list. No hardcoded curated fallback
    # (de-hardcode directive 2026-07-06): an OpenAI-compatible provider relies on
    # /v1/models discovery; when it fails the picker shows nothing, not fake ids.
    async def _fake_fetch(url, *, policy=None, method="GET", headers=None, data=None):
        return _FakeFetchResponse(500, {})
    monkeypatch.setattr("personalclaw.net.client.fetch", _fake_fetch, raising=False)
    monkeypatch.setattr("personalclaw.sdk.net.fetch", _fake_fetch, raising=False)
    monkeypatch.setattr("personalclaw.net.fetch", _fake_fetch, raising=False)
    cat = prov.create_catalog({"api_key": "k"})
    assert _run(cat.list_models()) == []


def test_live_models_win_over_fallback(monkeypatch):
    async def _fake_fetch(url, *, policy=None, method="GET", headers=None, data=None):
        return _FakeFetchResponse(200, {"data": [{"id": "live-model-1"}]})
    monkeypatch.setattr("personalclaw.net.client.fetch", _fake_fetch, raising=False)
    monkeypatch.setattr("personalclaw.sdk.net.fetch", _fake_fetch, raising=False)
    monkeypatch.setattr("personalclaw.net.fetch", _fake_fetch, raising=False)
    cat = prov.create_catalog({"api_key": "k", "endpoint": prov.SPEC.default_base_url})
    models = _run(cat.list_models())
    assert [m.id for m in models] == ["live-model-1"]


def test_test_connection_needs_key(monkeypatch):
    monkeypatch.delenv(prov.SPEC.api_key_env, raising=False)
    cat = prov.create_catalog({})
    cat._api_key = ""
    assert _run(cat.test_connection()).ok is False
