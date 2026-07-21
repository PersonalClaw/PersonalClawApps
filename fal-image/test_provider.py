"""FAL image + video gen bundle — the bespoke async-queue provider, fully mocked.

Proves the ABC's async-internally contract: the provider owns a submit->poll loop
behind async generate()/edit(); the caller sees only ImageResult / VideoResult.
All HTTP is mocked (no real FAL calls, no cost).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from provider import FalImageProvider, FalVideoProvider
from personalclaw.sdk.image import ImageGenError
from personalclaw.sdk.video import VideoGenError


def _resp(status: int, payload: dict) -> object:
    """A fake net.FetchResponse matching the REAL shape: .status + .text PROPERTY
    (not a method) + .body + .headers."""
    class _R:
        def __init__(self, s, p):
            self.status = s
            self._p = p
            self.headers = {}
            self.body = json.dumps(p).encode()

        @property
        def text(self):
            return json.dumps(self._p)
    return _R(status, payload)


class TestFalSizeNormalization:
    """FAL rejects a 'WxH' image_size STRING with HTTP 422 but accepts a
    {width,height} OBJECT or a named preset."""

    def test_normalize_wxh_string_to_object(self):
        from provider import _normalize_image_size
        assert _normalize_image_size("1024x1024") == {"width": 1024, "height": 1024}
        assert _normalize_image_size("768x1024") == {"width": 768, "height": 1024}
        assert _normalize_image_size(" 1024 x 768 ") == {"width": 1024, "height": 768}

    def test_normalize_passes_known_preset(self):
        from provider import _normalize_image_size
        assert _normalize_image_size("square_hd") == "square_hd"
        assert _normalize_image_size("landscape_4_3") == "landscape_4_3"

    def test_normalize_drops_unknown_and_empty(self):
        from provider import _normalize_image_size
        assert _normalize_image_size("garbage") is None
        assert _normalize_image_size("") is None
        assert _normalize_image_size("   ") is None

    @pytest.mark.asyncio
    async def test_generate_sends_normalized_size_object(self):
        prov = FalImageProvider(api_key="k")
        seq = [
            _resp(200, {"request_id": "req-123"}),
            _resp(200, {"status": "COMPLETED"}),
            _resp(200, {"images": [{"url": "https://cdn/x.png"}]}),
        ]
        fake_fetch = AsyncMock(side_effect=seq)
        with patch("personalclaw.sdk.net.fetch", fake_fetch), \
             patch("provider._POLL_INTERVAL_S", 0):
            await prov.generate("a fox", model="fal-ai/flux/schnell", size="1024x1024")
        body = json.loads(fake_fetch.call_args_list[0].kwargs["data"])
        assert body["image_size"] == {"width": 1024, "height": 1024}


class TestFalImageProvider:
    @pytest.mark.asyncio
    async def test_generate_submit_poll_result(self):
        prov = FalImageProvider(api_key="k")
        seq = [
            _resp(200, {"request_id": "req-123"}),
            _resp(200, {"status": "IN_PROGRESS"}),
            _resp(200, {"status": "COMPLETED"}),
            _resp(200, {"images": [{"url": "https://cdn/x.png", "content_type": "image/png"}]}),
        ]
        fake_fetch = AsyncMock(side_effect=seq)
        with patch("personalclaw.sdk.net.fetch", fake_fetch):
            with patch("provider._POLL_INTERVAL_S", 0):
                out = await prov.generate("a fox", model="fal-ai/flux/schnell")
        assert len(out) == 1
        assert out[0].url == "https://cdn/x.png"
        assert out[0].mime == "image/png"
        submit_call = fake_fetch.call_args_list[0]
        assert submit_call.kwargs["method"] == "POST"
        assert b"a fox" in submit_call.kwargs["data"]

    @pytest.mark.asyncio
    async def test_generate_inline_result(self):
        prov = FalImageProvider(api_key="k")
        fake_fetch = AsyncMock(
            return_value=_resp(200, {"images": [{"url": "https://cdn/y.png"}]})
        )
        with patch("personalclaw.sdk.net.fetch", fake_fetch):
            out = await prov.generate("a cat")
        assert out[0].url == "https://cdn/y.png"

    @pytest.mark.asyncio
    async def test_edit_sends_data_uri(self, tmp_path):
        prov = FalImageProvider(api_key="k")
        src = tmp_path / "s.png"
        src.write_bytes(b"\x89PNG\r\nsource")
        seq = [
            _resp(200, {"request_id": "req-123"}),
            _resp(200, {"status": "COMPLETED"}),
            _resp(200, {"images": [{"url": "https://cdn/e.png"}]}),
        ]
        fake_fetch = AsyncMock(side_effect=seq)
        with patch("personalclaw.sdk.net.fetch", fake_fetch), \
             patch("provider._POLL_INTERVAL_S", 0):
            out = await prov.edit("make it night", source_image=str(src))
        assert out[0].url == "https://cdn/e.png"
        body = json.loads(fake_fetch.call_args_list[0].kwargs["data"])
        assert body["image_url"].startswith("data:image/png;base64,")

    @pytest.mark.asyncio
    async def test_failed_job_raises(self):
        prov = FalImageProvider(api_key="k")
        seq = [
            _resp(200, {"request_id": "req-123"}),
            _resp(200, {"status": "FAILED"}),
        ]
        with patch("personalclaw.sdk.net.fetch", AsyncMock(side_effect=seq)), \
             patch("provider._POLL_INTERVAL_S", 0):
            with pytest.raises(ImageGenError):
                await prov.generate("x")

    @pytest.mark.asyncio
    async def test_no_key_raises(self, monkeypatch):
        monkeypatch.delenv("FAL_KEY", raising=False)
        monkeypatch.delenv("FAL_API_KEY", raising=False)
        monkeypatch.setattr("provider._resolve_fal_key", lambda: "")
        prov = FalImageProvider(api_key="")
        with pytest.raises(ImageGenError):
            await prov.generate("x")

    @pytest.mark.asyncio
    async def test_submit_http_error_raises(self):
        prov = FalImageProvider(api_key="k")
        with patch("personalclaw.sdk.net.fetch", AsyncMock(return_value=_resp(500, {}))):
            with pytest.raises(ImageGenError):
                await prov.generate("x")

    @pytest.mark.asyncio
    async def test_is_available_reflects_key(self, monkeypatch):
        monkeypatch.setattr("provider._resolve_fal_key", lambda: "")
        assert await FalImageProvider(api_key="").is_available() is False
        assert await FalImageProvider(api_key="k").is_available() is True


class TestFalVideoProvider:
    @pytest.mark.asyncio
    async def test_generate_submit_poll_result(self):
        prov = FalVideoProvider(api_key="k")
        seq = [
            _resp(200, {"request_id": "req-123"}),
            _resp(200, {"status": "COMPLETED"}),
            _resp(200, {"video": {"url": "https://cdn/v.mp4", "content_type": "video/mp4"}}),
        ]
        fake_fetch = AsyncMock(side_effect=seq)
        with patch("personalclaw.sdk.net.fetch", fake_fetch), \
             patch("provider._POLL_INTERVAL_S", 0):
            out = await prov.generate("a sunset timelapse", model="fal-ai/kling-video/v2/master/text-to-video")
        assert len(out) == 1
        assert out[0].url == "https://cdn/v.mp4"
        assert out[0].mime == "video/mp4"
        submit_call = fake_fetch.call_args_list[0]
        assert submit_call.kwargs["method"] == "POST"
        body = json.loads(submit_call.kwargs["data"])
        assert body["prompt"] == "a sunset timelapse"

    @pytest.mark.asyncio
    async def test_generate_sends_duration_and_aspect_ratio(self):
        prov = FalVideoProvider(api_key="k")
        seq = [
            _resp(200, {"request_id": "req-123"}),
            _resp(200, {"status": "COMPLETED"}),
            _resp(200, {"video": {"url": "https://cdn/v.mp4"}}),
        ]
        fake_fetch = AsyncMock(side_effect=seq)
        with patch("personalclaw.sdk.net.fetch", fake_fetch), \
             patch("provider._POLL_INTERVAL_S", 0):
            await prov.generate(
                "waves crashing",
                duration_seconds=8.0,
                aspect_ratio="16:9",
            )
        body = json.loads(fake_fetch.call_args_list[0].kwargs["data"])
        # veo2 (the default video model) takes a literal '<n>s' duration string,
        # not a raw float — see FalVideoProvider._format_duration.
        assert body["duration"] == "8s"
        assert body["aspect_ratio"] == "16:9"

    @pytest.mark.asyncio
    async def test_generate_videos_list_format(self):
        """Some FAL models return videos in a list rather than a single object."""
        prov = FalVideoProvider(api_key="k")
        fake_fetch = AsyncMock(
            return_value=_resp(200, {"videos": [{"url": "https://cdn/v1.mp4"}]})
        )
        with patch("personalclaw.sdk.net.fetch", fake_fetch):
            out = await prov.generate("dancing")
        assert out[0].url == "https://cdn/v1.mp4"

    @pytest.mark.asyncio
    async def test_failed_job_raises(self):
        prov = FalVideoProvider(api_key="k")
        seq = [
            _resp(200, {"request_id": "req-123"}),
            _resp(200, {"status": "FAILED"}),
        ]
        with patch("personalclaw.sdk.net.fetch", AsyncMock(side_effect=seq)), \
             patch("provider._POLL_INTERVAL_S", 0):
            with pytest.raises(ImageGenError):
                await prov.generate("x")

    @pytest.mark.asyncio
    async def test_no_video_in_result_raises(self):
        prov = FalVideoProvider(api_key="k")
        seq = [
            _resp(200, {"request_id": "req-123"}),
            _resp(200, {"status": "COMPLETED"}),
            _resp(200, {"other": "data"}),  # no video/videos key
        ]
        fake_fetch = AsyncMock(side_effect=seq)
        with patch("personalclaw.sdk.net.fetch", fake_fetch), \
             patch("provider._POLL_INTERVAL_S", 0):
            with pytest.raises(VideoGenError):
                await prov.generate("x")

    @pytest.mark.asyncio
    async def test_is_available_reflects_key(self, monkeypatch):
        monkeypatch.setattr("provider._resolve_fal_key", lambda: "")
        assert await FalVideoProvider(api_key="").is_available() is False
        assert await FalVideoProvider(api_key="k").is_available() is True


class TestFalDefaultModel:
    """The unpinned default model must be DERIVED from catalogs, not hardcoded."""

    def test_image_generate_default_is_first_text_to_image_entry(self):
        from provider import _KNOWN_IMAGE_MODELS, _default_image_model

        expected = next(m.name for m in _KNOWN_IMAGE_MODELS if not m.supports_edit)
        assert _default_image_model(edit=False) == expected

    def test_image_edit_default_is_first_edit_capable_entry(self):
        from provider import _KNOWN_IMAGE_MODELS, _default_image_model

        expected = next(m.name for m in _KNOWN_IMAGE_MODELS if m.supports_edit)
        assert _default_image_model(edit=True) == expected

    def test_video_default_is_first_entry(self):
        from provider import _KNOWN_VIDEO_MODELS, _default_video_model

        assert _default_video_model() == _KNOWN_VIDEO_MODELS[0].name

    @pytest.mark.asyncio
    async def test_generate_uses_derived_default_when_unpinned(self):
        from provider import _default_image_model

        prov = FalImageProvider(api_key="k")
        fake_fetch = AsyncMock(
            return_value=_resp(200, {"images": [{"url": "https://cdn/z.png"}]})
        )
        with patch("personalclaw.sdk.net.fetch", fake_fetch):
            await prov.generate("a bird")  # no model= -> derived default
        submit_url = fake_fetch.call_args_list[0].args[0]
        assert submit_url.endswith(_default_image_model(edit=False))


class TestFalFactory:
    """The manifest entry point: create_provider(config) builds providers from
    its Settings-card config (api_key)."""

    def test_create_provider_returns_list(self):
        from provider import create_provider

        providers = create_provider({"api_key": "from-card"})
        assert isinstance(providers, list)
        assert len(providers) == 2
        assert isinstance(providers[0], FalImageProvider)
        assert isinstance(providers[1], FalVideoProvider)
        assert providers[0]._api_key == "from-card"
        assert providers[1]._api_key == "from-card"

    def test_create_provider_empty_config_ok(self):
        from provider import create_provider

        providers = create_provider(None)
        assert len(providers) == 2
        assert providers[0]._api_key == ""
        assert providers[1]._api_key == ""

    def test_resolve_key_prefers_card_then_env(self, monkeypatch):
        import provider as fal

        monkeypatch.setattr(
            "personalclaw.sdk.settings.ProviderSettings.load",
            staticmethod(lambda name: {"api_key": "card-key"}),
        )
        assert fal._resolve_fal_key() == "card-key"

        monkeypatch.setattr(
            "personalclaw.sdk.settings.ProviderSettings.load",
            staticmethod(lambda name: {}),
        )
        monkeypatch.setenv("FAL_KEY", "env-key")
        assert fal._resolve_fal_key() == "env-key"
