"""OpenAICatalog — discovery over the OpenAI-compatible /v1/models endpoint.

Discovery is a pure function of the entry's endpoint/api_key (no live provider
session). The wire client is the shared ``openai_compatible_list_models`` SDK
helper; here we stub aiohttp to assert the catalog surfaces its models + reports
connectivity from key/endpoint presence.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import provider as prov  # app-local, registers type + catalog on import

from personalclaw.llm.catalog import ModelCatalog
from personalclaw.llm.registry import get_default_registry


def _run(coro):
    return asyncio.run(coro)


class _FakeFetchResponse:
    def __init__(self, status, payload):
        import json
        self.status = status
        self.text = json.dumps(payload)


def _stub_models(monkeypatch, payload, status=200):
    # Discovery (openai_compatible_list_models) now routes through the net.fetch
    # egress chokepoint (#41) — stub fetch, not aiohttp.
    async def _fake_fetch(url, *, policy=None, method="GET", headers=None, data=None):
        return _FakeFetchResponse(status, payload)
    monkeypatch.setattr("personalclaw.net.client.fetch", _fake_fetch, raising=False)
    monkeypatch.setattr("personalclaw.sdk.net.fetch", _fake_fetch, raising=False)
    monkeypatch.setattr("personalclaw.net.fetch", _fake_fetch, raising=False)


def test_catalog_registered_and_is_plain_catalog():
    reg = get_default_registry()
    assert reg.catalog_of("openai") is not None
    cat = prov.create_catalog({"api_key": "sk-x", "endpoint": "https://api.openai.com/v1"})
    assert isinstance(cat, ModelCatalog)
    # A hosted API is NOT a manager (no local pull/delete).
    from personalclaw.llm.catalog import ModelManager
    assert not isinstance(cat, ModelManager)


def test_list_models_infers_capabilities(monkeypatch):
    _stub_models(monkeypatch, {"data": [
        {"id": "gpt-4o", "owned_by": "openai"},
        {"id": "text-embedding-3-small", "owned_by": "openai"},
    ]})
    cat = prov.create_catalog({"api_key": "sk-x"})
    models = _run(cat.list_models())
    by_id = {m.id: m for m in models}
    assert "chat" in by_id["gpt-4o"].capabilities
    assert "image_modality" in by_id["gpt-4o"].capabilities
    assert by_id["text-embedding-3-small"].capabilities == ["embedding"]
    assert by_id["gpt-4o"].extra.get("owned_by") == "openai"


def test_test_connection_needs_config():
    # No key + no endpoint → not ok, no network call.
    cat = prov.create_catalog({})
    # Ensure OPENAI_API_KEY isn't silently satisfying it in this env.
    cat._api_key = ""
    res = _run(cat.test_connection())
    assert res.ok is False


def test_test_connection_ok_when_models_returned(monkeypatch):
    _stub_models(monkeypatch, {"data": [{"id": "gpt-4o"}]})
    cat = prov.create_catalog({"api_key": "sk-x"})
    res = _run(cat.test_connection())
    assert res.ok is True
    assert res.model_count == 1
