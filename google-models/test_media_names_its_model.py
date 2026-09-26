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
