"""Catalog tests for the generic openai-compatible app — discovers live from the
configured endpoint's /v1/models; no curated fallback (endpoint is unknown)."""

from __future__ import annotations

import asyncio

import provider as prov  # app-local; registers on import

from personalclaw.llm.catalog import ModelCatalog, ModelManager


def _run(coro):
    return asyncio.run(coro)


class _FakeResp:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, url, **kw):
        return self._resp


def test_catalog_is_plain_catalog():
    cat = prov.create_catalog({"endpoint": "https://gw/v1"})
    assert isinstance(cat, ModelCatalog)
    assert not isinstance(cat, ModelManager)


def test_lists_live_models(monkeypatch):
    import aiohttp
    monkeypatch.setattr(aiohttp, "ClientSession",
                        lambda *a, **k: _FakeSession(_FakeResp(200, {"data": [{"id": "served-model"}]})))
    cat = prov.create_catalog({"api_key": "k", "endpoint": "https://gw/v1"})
    models = _run(cat.list_models())
    assert [m.id for m in models] == ["served-model"]


def test_no_fallback_when_unreachable(monkeypatch):
    import aiohttp
    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: _FakeSession(_FakeResp(500, {})))
    cat = prov.create_catalog({"api_key": "k", "endpoint": "https://gw/v1"})
    # No curated list for an unknown endpoint → empty (never raises).
    assert _run(cat.list_models()) == []
