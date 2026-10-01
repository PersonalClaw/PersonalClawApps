"""Catalog tests for the mistral app — live /v1/models discovery only (no hardcoded
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


# ── Each model is offered for what Mistral's record of it says ─────────────────────────────
# A recorded `GET /v1/models` answer: every record carries Mistral's own `capabilities` flags.


def _record(model_id, **flags):
    capabilities = {
        "completion_chat": False, "function_calling": False, "reasoning": False,
        "completion_fim": False, "fine_tuning": False, "vision": False, "ocr": False,
        "classification": False, "moderation": False, "audio": False,
        "audio_transcription": False, "audio_transcription_realtime": False,
        "audio_speech": False,
    }
    capabilities.update(flags)
    return {
        "id": model_id, "object": "model", "created": 1756746619, "owned_by": "mistralai",
        "capabilities": capabilities, "name": model_id, "description": "",
        "max_context_length": 131072, "aliases": [], "deprecation": None,
        "default_model_temperature": 0.3, "type": "base",
    }


MISTRAL_LISTING = {"object": "list", "data": [
    _record("mistral-medium-2508", completion_chat=True, function_calling=True, vision=True),
    _record("codestral-2508", completion_chat=True, function_calling=True, completion_fim=True),
    _record("magistral-medium-2509", completion_chat=True, reasoning=True, vision=True),
    _record("voxtral-mini-2507", completion_chat=True, audio=True, audio_transcription=True),
    _record("mistral-embed"),
    _record("codestral-embed-2505"),
    _record("mistral-ocr-2505", ocr=True),
    _record("mistral-moderation-2411", moderation=True, classification=True),
    _record("voxtral-mini-transcribe-realtime-2602", audio_transcription_realtime=True),
    _record("voxtral-mini-tts-2603", audio_speech=True),
]}


def test_each_model_is_offered_for_what_mistral_says_it_does(monkeypatch):
    async def _fake_fetch(url, *, policy=None, method="GET", headers=None, data=None):
        return _FakeFetchResponse(200, MISTRAL_LISTING)
    monkeypatch.setattr("personalclaw.sdk.net.fetch", _fake_fetch, raising=False)
    cat = prov.create_catalog({"api_key": "k"})
    caps = {m.id: m.capabilities for m in _run(cat.list_models())}
    assert caps == {
        "mistral-medium-2508": ["chat", "image_modality"],
        "codestral-2508": ["chat"],
        "magistral-medium-2509": ["chat", "image_modality"],
        "voxtral-mini-2507": ["chat", "audio_modality", "stt"],
        "mistral-embed": ["embedding"],
        "codestral-embed-2505": ["embedding"],
        # OCR, moderation, live transcription and speech: nothing here drives them.
        "mistral-ocr-2505": [],
        "mistral-moderation-2411": [],
        "voxtral-mini-transcribe-realtime-2602": [],
        "voxtral-mini-tts-2603": [],
    }
