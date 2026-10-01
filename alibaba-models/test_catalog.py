"""The Alibaba Model Studio catalog offers each model for what this app can drive.

DashScope's OpenAI-compatible model list names each model and nothing more, so each model is
read by its id; this app then states image input where Model Studio documents it, and leaves off
the speech jobs, which answer through Model Studio's own speech APIs that this app does not
drive. The listing is a recorded shape, served by a fake: no vendor is called.
"""

from __future__ import annotations

import asyncio
import json

import provider as prov  # app-local; registers on import


class _Answer:
    def __init__(self, payload):
        self.status = 200
        self.text = json.dumps(payload)


def _record(model_id):
    return {"id": model_id, "object": "model", "created": 1745000000, "owned_by": "system"}


DASHSCOPE_LISTING = {"object": "list", "data": [_record(m) for m in (
    "qwen-plus",
    "qwen3-vl-plus",
    "qvq-max",
    "qwen-mt-turbo",
    "text-embedding-v4",
    "gte-rerank-v2",
    "qwen3-asr-flash",
    "paraformer-v2",
    "paraformer-realtime-v2",
    "qwen3-omni-flash-realtime",
    "qwen-tts",
    "cosyvoice-v2",
)]}


def test_each_model_is_offered_for_what_this_app_can_drive(monkeypatch):
    async def _fake_fetch(url, *, policy=None, method="GET", headers=None, data=None):
        return _Answer(DASHSCOPE_LISTING)
    monkeypatch.setattr("personalclaw.sdk.net.fetch", _fake_fetch, raising=False)
    cat = prov.create_catalog({"api_key": "k"})
    caps = {m.id: m.capabilities for m in asyncio.run(cat.list_models())}
    assert caps == {
        "qwen-plus": ["chat"],
        "qwen3-vl-plus": ["chat", "image_modality"],
        "qvq-max": ["chat", "image_modality"],
        "qwen-mt-turbo": ["chat"],
        "text-embedding-v4": ["embedding"],
        # A reranker scores documents; realtime models take a live session; the speech models
        # answer through Model Studio's own speech APIs. None is offered.
        "gte-rerank-v2": [],
        "qwen3-asr-flash": [],
        "paraformer-v2": [],
        "paraformer-realtime-v2": [],
        "qwen3-omni-flash-realtime": [],
        "qwen-tts": [],
        "cosyvoice-v2": [],
    }
