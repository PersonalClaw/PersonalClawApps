"""VLLMCatalog — discovery over a vLLM server's OpenAI-compatible /v1/models.

vLLM is a local server (endpoint required, auth typically absent); the catalog
reuses the shared ``openai_compatible_list_models`` SDK helper."""

from __future__ import annotations

import asyncio

import provider as prov  # app-local, registers type + catalog on import

from personalclaw.llm.catalog import ModelCatalog, ModelManager
from personalclaw.llm.registry import get_default_registry


def _run(coro):
    return asyncio.run(coro)


class _FakeFetchResponse:
    def __init__(self, status, payload):
        import json
        self.status = status
        self.text = json.dumps(payload)


def _stub(monkeypatch, payload, status=200):
    # The discovery helper (openai_compatible_list_models) now routes through the
    # net.fetch egress chokepoint (#41), so stub fetch — not aiohttp.
    async def _fake_fetch(url, *, policy=None, method="GET", headers=None, data=None):
        return _FakeFetchResponse(status, payload)
    monkeypatch.setattr("personalclaw.net.client.fetch", _fake_fetch, raising=False)
    monkeypatch.setattr("personalclaw.sdk.net.fetch", _fake_fetch, raising=False)
    monkeypatch.setattr("personalclaw.net.fetch", _fake_fetch, raising=False)


def test_catalog_registered_and_default_endpoint():
    assert get_default_registry().catalog_of("vllm") is not None
    cat = prov.create_catalog({})
    assert isinstance(cat, ModelCatalog)
    assert not isinstance(cat, ModelManager)
    assert cat._endpoint == "http://localhost:8000"  # default local server


def test_list_models(monkeypatch):
    _stub(monkeypatch, {"data": [{"id": "meta-llama/Llama-3-8B"}]})
    cat = prov.create_catalog({"endpoint": "http://localhost:8000"})
    models = _run(cat.list_models())
    assert [m.id for m in models] == ["meta-llama/Llama-3-8B"]
    assert "chat" in models[0].capabilities


def test_connection_fails_when_unreachable(monkeypatch):
    _stub(monkeypatch, {"data": []})  # server up but no models / unreachable → empty
    res = _run(prov.create_catalog({"endpoint": "http://localhost:8000"}).test_connection())
    assert res.ok is False
