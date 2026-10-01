"""Catalog tests for the groq app — live /v1/models discovery only (no hardcoded
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


# ── Each model is offered for what its id says (Groq's listing says nothing more) ─────────
# A recorded `GET /openai/v1/models` answer.


def _record(model_id, owner, window=131072):
    return {
        "id": model_id, "object": "model", "created": 1733447754, "owned_by": owner,
        "active": True, "context_window": window, "public_apps": None,
        "max_completion_tokens": 32768,
    }


GROQ_LISTING = {"object": "list", "data": [
    _record("llama-3.3-70b-versatile", "Meta"),
    _record("openai/gpt-oss-120b", "OpenAI"),
    _record("meta-llama/llama-4-scout-17b-16e-instruct", "Meta"),
    _record("groq/compound", "Groq"),
    _record("meta-llama/llama-guard-4-12b", "Meta"),
    _record("meta-llama/llama-prompt-guard-2-86m", "Meta", window=512),
    _record("whisper-large-v3", "OpenAI", window=448),
    _record("playai-tts", "PlayAI", window=8192),
    _record("canopylabs/orpheus-v1-english", "Canopy Labs", window=4000),
]}


def test_each_model_is_offered_for_what_it_does(monkeypatch):
    async def _fake_fetch(url, *, policy=None, method="GET", headers=None, data=None):
        return _FakeFetchResponse(200, GROQ_LISTING)
    monkeypatch.setattr("personalclaw.sdk.net.fetch", _fake_fetch, raising=False)
    cat = prov.create_catalog({"api_key": "k"})
    caps = {m.id: m.capabilities for m in _run(cat.list_models())}
    assert caps == {
        "llama-3.3-70b-versatile": ["chat"],
        "openai/gpt-oss-120b": ["chat"],
        "meta-llama/llama-4-scout-17b-16e-instruct": ["chat", "image_modality"],
        "groq/compound": ["chat"],
        # Safety classifiers answer a verdict, not a conversation.
        "meta-llama/llama-guard-4-12b": [],
        "meta-llama/llama-prompt-guard-2-86m": [],
        "whisper-large-v3": ["stt"],
        "playai-tts": ["tts"],
        "canopylabs/orpheus-v1-english": ["tts"],
    }
