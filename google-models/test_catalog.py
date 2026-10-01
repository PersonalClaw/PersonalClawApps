"""Catalog tests for the google app — live /v1/models discovery only (no hardcoded
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


@pytest.fixture(autouse=True)
def _native_list_answers_nothing(monkeypatch):
    """The chat catalog also reads Gemini's native model list; no test here reaches Gemini, so
    that list answers nothing unless a test serves one."""
    async def _discover(_key):
        return []
    monkeypatch.setattr(prov, "_discover_models", _discover)


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


# ── Each model is offered for what Gemini's own record of it says ──────────────────────────
# Recorded answers: the OpenAI-compatible `GET /v1beta/openai/models`, which names each model,
# and the native `GET /v1beta/models`, whose records carry `supportedGenerationMethods`.


def _native(model_id, *methods):
    return {
        "name": f"models/{model_id}", "version": "001", "displayName": model_id,
        "description": "", "inputTokenLimit": 1048576, "outputTokenLimit": 65536,
        "supportedGenerationMethods": list(methods),
    }


GEMINI_NATIVE = [
    _native("gemini-2.5-flash", "generateContent", "countTokens", "createCachedContent"),
    _native("gemma-3-27b-it", "generateContent", "countTokens"),
    _native("gemini-embedding-001", "embedContent", "countTextTokens", "countTokens"),
    _native("gemini-2.5-flash-preview-tts", "countTokens", "generateContent"),
    _native("gemini-2.5-flash-image", "generateContent", "countTokens"),
    _native("imagen-4.0-generate-001", "predict"),
    _native("veo-3.0-generate-001", "predictLongRunning"),
    _native("gemini-2.0-flash-live-001", "bidiGenerateContent", "countTokens"),
    _native("gemini-2.5-flash-native-audio-preview-09-2025", "countTokens", "bidiGenerateContent"),
    _native("aqa", "generateAnswer"),
]


def test_each_model_is_offered_for_what_its_methods_say(monkeypatch):
    compat = {"object": "list", "data": [
        {"id": m["name"], "object": "model", "owned_by": "google"} for m in GEMINI_NATIVE
    ] + [{"id": "models/a-model-the-native-list-lacks", "object": "model", "owned_by": "google"}]}

    async def _fake_fetch(url, *, policy=None, method="GET", headers=None, data=None):
        return _FakeFetchResponse(200, compat)

    async def _discover(key):
        assert key == "k"
        return GEMINI_NATIVE

    monkeypatch.setattr("personalclaw.sdk.net.fetch", _fake_fetch, raising=False)
    monkeypatch.setattr(prov, "_discover_models", _discover)
    caps = {m.id: m.capabilities for m in _run(prov.create_catalog({"api_key": "k"}).list_models())}
    assert caps == {
        "models/gemini-2.5-flash": ["chat", "image_modality"],
        "models/gemma-3-27b-it": ["chat"],
        "models/gemini-embedding-001": ["embedding"],
        "models/gemini-2.5-flash-preview-tts": ["tts"],
        "models/gemini-2.5-flash-image": ["image_gen"],
        "models/imagen-4.0-generate-001": ["image_gen"],
        "models/veo-3.0-generate-001": ["video_gen"],
        # Served only through the Live API, or AQA's own method: nothing here speaks those.
        "models/gemini-2.0-flash-live-001": [],
        "models/gemini-2.5-flash-native-audio-preview-09-2025": [],
        "models/aqa": [],
        # No native record: what its id says stands.
        "models/a-model-the-native-list-lacks": ["chat"],
    }
