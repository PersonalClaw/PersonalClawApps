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
    # egress chokepoint — stub fetch, not aiohttp.
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


# ── Each model is offered for what its id says, and only for an API this app speaks ───────
# A recorded `GET /v1/models` answer: OpenAI names each model and nothing more.


def _record(model_id, owner="system"):
    return {"id": model_id, "object": "model", "created": 1745000000, "owned_by": owner}


OPENAI_LISTING = {"object": "list", "data": [_record(m) for m in (
    "gpt-4.1",
    "gpt-5-mini",
    "o4-mini",
    "gpt-4o-audio-preview",
    "text-embedding-3-large",
    "whisper-1",
    "gpt-4o-mini-transcribe",
    "gpt-4o-mini-tts",
    "gpt-image-1",
    "omni-moderation-latest",
    "babbage-002",
    "davinci-002",
    "gpt-3.5-turbo-instruct",
    "gpt-realtime",
    "gpt-4o-realtime-preview",
    "o1-pro",
    "o3-pro",
    "gpt-5-pro",
    "gpt-5-codex",
    "codex-mini-latest",
    "o3-deep-research",
    "computer-use-preview",
)]}


def test_each_model_is_offered_for_what_it_does(monkeypatch):
    _stub_models(monkeypatch, OPENAI_LISTING)
    caps = {m.id: m.capabilities for m in _run(prov.create_catalog({"api_key": "sk-x"}).list_models())}
    assert caps["gpt-4.1"] == ["chat", "image_modality"]
    assert caps["gpt-5-mini"] == ["chat", "image_modality"]
    assert caps["o4-mini"] == ["chat"]
    assert caps["gpt-4o-audio-preview"] == ["chat", "image_modality", "audio_modality"]
    assert caps["text-embedding-3-large"] == ["embedding"]
    assert caps["whisper-1"] == caps["gpt-4o-mini-transcribe"] == ["stt"]
    assert caps["gpt-4o-mini-tts"] == ["tts"]
    assert caps["gpt-image-1"] == ["image_gen"]
    # Moderation, completion-only, realtime-only and Responses-only models answer no chat
    # completion, the one API this app's chat speaks: none is offered.
    for model_id in (
        "omni-moderation-latest", "babbage-002", "davinci-002", "gpt-3.5-turbo-instruct",
        "gpt-realtime", "gpt-4o-realtime-preview", "o1-pro", "o3-pro", "gpt-5-pro",
        "gpt-5-codex", "codex-mini-latest", "o3-deep-research", "computer-use-preview",
    ):
        assert caps[model_id] == [], model_id
