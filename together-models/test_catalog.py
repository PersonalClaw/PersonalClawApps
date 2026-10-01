"""Catalog tests for the together app — live /v1/models discovery only (no hardcoded
fallback catalog; with nothing to fall back on a discovery failure is raised, core #955)."""

from __future__ import annotations

import asyncio

import provider as prov  # app-local; registers on import
import pytest

from personalclaw.llm.catalog import ModelCatalog, ModelDiscoveryError, ModelManager


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


def test_discovery_failure_is_raised_not_swallowed(monkeypatch):
    # Endpoint 500 -> the failure is RAISED. No hardcoded curated fallback
    # (de-hardcode directive 2026-07-06), and with nothing to fall back on a
    # discovery failure is not the same event as "this endpoint serves no
    # models" (core #955): every caller relays a raised failure onto the
    # provider row, while a silent [] is the one answer a user cannot act on.
    async def _fake_fetch(url, *, policy=None, method="GET", headers=None, data=None):
        return _FakeFetchResponse(500, {})
    monkeypatch.setattr("personalclaw.net.client.fetch", _fake_fetch, raising=False)
    monkeypatch.setattr("personalclaw.sdk.net.fetch", _fake_fetch, raising=False)
    monkeypatch.setattr("personalclaw.net.fetch", _fake_fetch, raising=False)
    assert list(prov.SPEC.fallback_models) == []  # the invariant the raise depends on
    cat = prov.create_catalog({"api_key": "k"})
    with pytest.raises(ModelDiscoveryError) as exc:
        _run(cat.list_models())
    assert "500" in str(exc.value)  # the status the user has to act on, not a bare ""


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


# ── Each model is offered for the type Together gives it ───────────────────────────────────
# A recorded `GET /v1/models` answer: Together answers with a bare JSON array, each record
# naming its `type`.


def _record(model_id, kind, name):
    return {
        "id": model_id, "object": "model", "created": 1733443200, "type": kind,
        "running": False, "display_name": name, "organization": "Example Org",
        "license": "other", "context_length": 32768,
        "pricing": {"hourly": 0, "input": 0.2, "output": 0.2, "base": 0, "finetune": 0},
    }


TOGETHER_LISTING = [
    _record("meta-llama/Llama-3.3-70B-Instruct-Turbo", "chat", "Llama 3.3 70B Instruct Turbo"),
    _record("Qwen/Qwen2.5-VL-72B-Instruct", "chat", "Qwen2.5-VL 72B Instruct"),
    _record("meta-llama/Meta-Llama-3-8B", "language", "Llama 3 8B"),
    _record("codellama/CodeLlama-34b-Python-hf", "code", "Code Llama Python 34B"),
    _record("BAAI/bge-base-en-v1.5", "embedding", "BGE Base EN v1.5"),
    _record("black-forest-labs/FLUX.1-schnell", "image", "FLUX.1 Schnell"),
    _record("meta-llama/Meta-Llama-Guard-3-8B", "moderation", "Llama Guard 3 8B"),
    _record("Salesforce/Llama-Rank-V1", "rerank", "LlamaRank"),
    _record("openai/whisper-large-v3", "transcribe", "Whisper large-v3"),
]


def test_each_model_is_offered_for_the_type_together_gives_it(monkeypatch):
    async def _fake_fetch(url, *, policy=None, method="GET", headers=None, data=None):
        return _FakeFetchResponse(200, TOGETHER_LISTING)
    monkeypatch.setattr("personalclaw.sdk.net.fetch", _fake_fetch, raising=False)
    cat = prov.create_catalog({"api_key": "k"})
    caps = {m.id: m.capabilities for m in _run(cat.list_models())}
    assert caps == {
        "meta-llama/Llama-3.3-70B-Instruct-Turbo": ["chat"],
        "Qwen/Qwen2.5-VL-72B-Instruct": ["chat", "image_modality"],
        "meta-llama/Meta-Llama-3-8B": [],
        "codellama/CodeLlama-34b-Python-hf": [],
        "BAAI/bge-base-en-v1.5": ["embedding"],
        "black-forest-labs/FLUX.1-schnell": ["image_gen"],
        "meta-llama/Meta-Llama-Guard-3-8B": [],
        "Salesforce/Llama-Rank-V1": [],
        "openai/whisper-large-v3": ["stt"],
    }
