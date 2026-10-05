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
    # net.fetch egress chokepoint, so stub fetch — not aiohttp.
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


# ── Each model is offered for what it is, an alias for the model it serves ─────────────────
# A recorded vLLM `GET /v1/models` answer: `root` names the model an alias
# (`--served-model-name`) serves.


def _record(model_id, root, window):
    return {
        "id": model_id, "object": "model", "created": 1745000000, "owned_by": "vllm",
        "root": root, "parent": None, "max_model_len": window,
        "permission": [{"id": "modelperm-0", "object": "model_permission", "allow_view": True}],
    }


VLLM_LISTING = {"object": "list", "data": [
    _record("Qwen/Qwen3-8B", "Qwen/Qwen3-8B", 40960),
    _record("sorter", "BAAI/bge-reranker-v2-m3", 8194),
    _record("eyes", "Qwen/Qwen2.5-VL-7B-Instruct", 32768),
    _record("embedder", "intfloat/e5-mistral-7b-instruct", 4096),
    _record("guard", "meta-llama/Llama-Guard-3-8B", 131072),
]}


def test_each_model_is_offered_for_what_it_is(monkeypatch):
    _stub(monkeypatch, VLLM_LISTING)
    cat = prov.create_catalog({"endpoint": "http://localhost:8000"})
    caps = {m.id: m.capabilities for m in _run(cat.list_models())}
    assert caps == {
        "Qwen/Qwen3-8B": ["chat"],
        "sorter": [],
        "eyes": ["chat", "image_modality"],
        "embedder": ["embedding"],
        "guard": [],
    }
