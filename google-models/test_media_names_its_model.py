"""Every Gemini media call names its model, like chat, and one that names none is refused.

Handed no model, the image and video adapters took the first image or Veo model discovery
listed, and speech used gemini-3.1-flash-tts-preview. Discovery and every request the adapters
try to send are recorded, so a refusal is proven by nothing being asked of Gemini.
"""

from __future__ import annotations

import asyncio

import pytest

import provider as prov

NO_MODEL = "No model is chosen for this call"


@pytest.fixture
def asked(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """What the adapter asked of Gemini: each model discovery and each request it tried to send."""
    import aiohttp

    calls: list[str] = []

    async def _discover(_key):
        calls.append("discovery")
        return []

    class _Session:
        def __init__(self, *_a, **_k) -> None:
            calls.append("request")

        async def __aenter__(self):
            raise RuntimeError("no Gemini endpoint in this test")

        async def __aexit__(self, *_exc) -> bool:
            return False

    monkeypatch.setattr(prov, "_discover_models", _discover)
    monkeypatch.setattr(aiohttp, "ClientSession", _Session)
    return calls


def test_an_image_that_names_no_model_is_refused(asked):
    from personalclaw.sdk.image import ImageGenError

    with pytest.raises(ImageGenError, match=NO_MODEL):
        asyncio.run(prov.GeminiImageProvider(api_key="k").generate("a heron"))
    assert asked == []


def test_a_video_that_names_no_model_is_refused(asked):
    from personalclaw.sdk.video import VideoGenError

    with pytest.raises(VideoGenError, match=NO_MODEL):
        asyncio.run(prov.GeminiVideoProvider(api_key="k").generate("a heron"))
    assert asked == []


def test_speech_that_names_no_model_is_refused(asked):
    assert asyncio.run(prov.GeminiTTSProvider(api_key="k").synthesize("hello")) is None
    assert asked == []


def test_speech_says_it_named_no_model_before_it_says_it_has_no_key(asked, monkeypatch, caplog):
    """A call that names no model is refused whatever the key, so that is its reason. With the
    key checked first, an install missing both was told only about the key."""
    import logging

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(prov.GeminiTTSProvider(api_key="").synthesize("hello")) is None
    assert NO_MODEL in caplog.text
    assert "no API key" not in caplog.text
    assert asked == []


def test_can_synthesize_answers_what_synthesize_would_do(monkeypatch):
    """Core asks this before it synthesizes, so it names the same two conditions a call needs."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    keyed, keyless = prov.GeminiTTSProvider(api_key="k"), prov.GeminiTTSProvider(api_key="")
    assert asyncio.run(keyed.can_synthesize("gemini-fake-tts")) is True
    assert asyncio.run(keyed.can_synthesize("")) is False
    assert asyncio.run(keyless.can_synthesize("gemini-fake-tts")) is False


def test_a_media_call_that_names_no_model_is_refused_and_sends_nothing():
    """The image, video and text-to-speech adapters an instance registers each refuse a call
    that names no model, with the SDK's sentence, before anything is sent. Gemini image editing
    is not built, so edit is left out."""
    from pathlib import Path

    from apps_testkit.model_wire import (
        media_adapters,
        media_refusal_expected,
        media_refusal_report,
    )

    adapters = media_adapters(
        Path(__file__).parent,
        prov.create_provider,
        scanners=(prov._scan_image, prov._scan_video, prov._scan_tts),
    )
    report = asyncio.run(media_refusal_report(adapters, edit=False))
    assert report == media_refusal_expected(adapters, edit=False)
