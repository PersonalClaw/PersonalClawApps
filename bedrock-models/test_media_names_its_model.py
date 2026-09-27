"""Every Bedrock media call names its model, like chat, and one that names none is refused.

The media adapters (embedding, image, video, speech-to-text) are built per instance by core's
media registries and handed the model of the binding in Settings → Models. Handed none, each put
Bedrock's first model of its kind in its place: Titan Embed v2, Nova Canvas, Nova Reel. The
speech adapter ran Amazon Transcribe whatever the binding named, and nothing listed Transcribe,
so Settings → Models had nothing to bind speech-to-text to.

A fake ``boto3`` records every client made and every call sent, so a refusal is proven by
nothing reaching AWS, and a named model by the id each call sends.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import types
from typing import Any

import pytest

import provider as prov

NO_MODEL = "No model is chosen for this call"


class _Body(io.BytesIO):
    """``invoke_model``'s streaming body: ``.read()`` returns the JSON the model answered."""


class _Client:
    """One boto3 client, recording each call it is asked to make."""

    def __init__(self, service: str, sent: list[tuple[str, str, dict[str, Any]]]) -> None:
        self._service = service
        self._sent = sent

    def invoke_model(self, **kwargs: Any) -> dict[str, Any]:
        self._sent.append((self._service, "invoke_model", kwargs))
        body = json.loads(kwargs["body"])
        if "textToImageParams" in body:
            return {"body": _Body(json.dumps({"images": ["aW1hZ2U="]}).encode())}
        return {"body": _Body(json.dumps({"embedding": [0.1, 0.2, 0.3]}).encode())}

    def start_async_invoke(self, **kwargs: Any) -> dict[str, Any]:
        self._sent.append((self._service, "start_async_invoke", kwargs))
        return {"invocationArn": "arn:aws:bedrock:us-east-1:000000000000:async-invoke/job"}

    def get_async_invoke(self, **kwargs: Any) -> dict[str, Any]:
        self._sent.append((self._service, "get_async_invoke", kwargs))
        return {"status": "Completed"}

    def download_file(self, bucket: str, key: str, path: str) -> None:
        self._sent.append((self._service, "download_file", {"Bucket": bucket, "Key": key}))
        with open(path, "wb") as fh:
            fh.write(b"mp4")


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, Any]]]:
    """Install a fake ``boto3``; returns ``(service, operation, kwargs)`` for each call made."""
    calls: list[tuple[str, str, dict[str, Any]]] = []

    class _Session:
        def __init__(self, profile_name: str | None = None) -> None:
            self._profile = profile_name

        def client(self, service: str, region_name: str | None = None, config: Any = None):
            calls.append((service, "client", {"region": region_name}))
            return _Client(service, calls)

    fake = types.ModuleType("boto3")
    fake.Session = _Session  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", fake)
    monkeypatch.setattr(prov, "_VIDEO_POLL_INTERVAL", 0)
    return calls


def _run(coro):
    return asyncio.run(coro)


# ── a call that names no model is refused, and nothing reaches AWS ──────────────────────────


def test_an_embedding_that_names_no_model_is_refused(sent):
    adapter = prov.BedrockEmbeddingProvider(name="my-bedrock")

    assert _run(adapter.embed("a heron by the lake")) is None
    assert _run(adapter.embed_batch(["a heron", "a kestrel"])) == [[], []]
    assert sent == [], "no client was made and nothing was sent"


def test_an_image_that_names_no_model_is_refused(sent):
    from personalclaw.sdk.image import ImageGenError

    with pytest.raises(ImageGenError, match=NO_MODEL):
        _run(prov.BedrockImageProvider(name="my-bedrock").generate("a heron"))
    assert sent == []


def test_a_video_that_names_no_model_is_refused(sent):
    from personalclaw.sdk.video import VideoGenError

    adapter = prov.BedrockVideoProvider(name="my-bedrock", s3_bucket="clips")
    with pytest.raises(VideoGenError, match=NO_MODEL):
        _run(adapter.generate("a heron"))
    assert sent == []


def test_a_transcription_that_names_no_model_is_refused(sent, tmp_path):
    clip = tmp_path / "clip.wav"
    clip.write_bytes(b"RIFF")
    adapter = prov.BedrockSTTProvider(name="my-bedrock", s3_bucket="clips")

    assert _run(adapter.transcribe(str(clip))) is None
    assert sent == []


# ── a named model is the one sent ─────────────────────────────────────────────────────────


def _model_ids(sent: list[tuple[str, str, dict[str, Any]]], operation: str) -> list[str]:
    return [kwargs["modelId"] for _service, op, kwargs in sent if op == operation]


def test_each_media_call_sends_the_model_it_names(sent):
    _run(
        prov.BedrockEmbeddingProvider(name="my-bedrock").embed(
            "a heron", model="cohere.embed-v4:0"
        )
    )
    _run(
        prov.BedrockImageProvider(name="my-bedrock").generate(
            "a heron", model="amazon.nova-canvas-v1:0"
        )
    )
    _run(
        prov.BedrockVideoProvider(name="my-bedrock", s3_bucket="clips").generate(
            "a heron", model="amazon.nova-reel-v1:1"
        )
    )

    assert _model_ids(sent, "invoke_model") == ["cohere.embed-v4:0", "amazon.nova-canvas-v1:0"]
    assert _model_ids(sent, "start_async_invoke") == ["amazon.nova-reel-v1:1"]


# ── Amazon Transcribe is listed, so a speech-to-text binding can name it ────────────────


def _listed(monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]) -> list[Any]:
    prov._BEDROCK_CACHE.clear()
    monkeypatch.setattr(prov, "_list_bedrock_models_sync", lambda region, profile: list(rows))
    try:
        return _run(prov.create_catalog({"region": "us-east-1"}).list_models())
    finally:
        prov._BEDROCK_CACHE.clear()


def test_the_catalog_lists_transcribe_for_speech_to_text(monkeypatch):
    chat = {"id": "us.amazon.nova-pro-v1:0", "name": "Nova Pro", "capabilities": ["chat"]}

    models = _listed(monkeypatch, [chat])

    stt = [m for m in models if "stt" in m.capabilities]
    assert [(m.id, m.name) for m in stt] == [(prov.TRANSCRIBE_MODEL, "Amazon Transcribe")]
    assert [m.id for m in models if "chat" in m.capabilities] == [chat["id"]]


def test_an_account_the_listings_did_not_reach_lists_nothing(monkeypatch):
    """No Transcribe row on its own: an empty catalog is how an unreachable account reads."""
    assert _listed(monkeypatch, []) == []


def test_a_media_call_that_names_no_model_is_refused_and_sends_nothing():
    """Every media adapter an instance registers refuses a call that names no model, with the
    SDK's sentence, before a client is built or anything is sent: the embedding, image, video
    and speech-to-text scanners' adapters, built for the instance the form saves. Nova Canvas
    has no edit, so edit is left out."""
    from pathlib import Path

    from apps_testkit.model_wire import (
        media_adapters,
        media_refusal_expected,
        media_refusal_report,
    )

    adapters = media_adapters(
        Path(__file__).parent,
        prov.create_provider,
        scanners=(prov._scan_embedding, prov._scan_image, prov._scan_video, prov._scan_stt),
    )
    report = asyncio.run(media_refusal_report(adapters, edit=False))
    assert report == media_refusal_expected(adapters, edit=False)
