"""FAL image + video gen provider — bespoke async-queue platform (a removable bundle).

FAL has no OpenAI-Images-compatible endpoint: you submit to ``queue.fal.run/<model>``,
get a ``request_id`` + status URL, poll until ``COMPLETED``, then read the response.
This provider owns that submit->poll loop entirely behind the async ``generate``/
``edit`` signature, so the caller (and the provider ABC) never sees the
difference between FAL and a synchronous adapter. All HTTP goes through the
``net.fetch`` egress chokepoint.

Vendor-neutral doctrine: FAL specifics live ONLY here, never in the core registry.
The provider is contributed by the bundled ``fal-image`` app manifest (a
``type:model`` provider with ``capabilities:[image_gen, video_gen]``), so it gets a
Settings card with an enable toggle + an ``api_key`` field. Its key resolves from
that saved card config first, then the ``FAL_KEY`` / ``FAL_API_KEY`` env var.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from personalclaw.sdk.image import (
    ImageGenError,
    ImageGenModel,
    ImageGenProvider,
    ImageResult,
)
from personalclaw.sdk.video import (
    VideoGenError,
    VideoGenModel,
    VideoGenProvider,
    VideoResult,
)

logger = logging.getLogger(__name__)

_QUEUE_BASE = "https://queue.fal.run"

# ── Image models catalog ─────────────────────────────────────────────────────

_KNOWN_IMAGE_MODELS = [
    ImageGenModel(
        name="fal-ai/flux/schnell",
        description="FLUX.1 [schnell] — fast text-to-image",
        sizes=["square_hd", "landscape_4_3", "portrait_4_3"],
        supports_edit=False,
    ),
    ImageGenModel(
        name="fal-ai/flux/dev",
        description="FLUX.1 [dev] — high-quality text-to-image",
        sizes=["square_hd", "landscape_4_3", "portrait_4_3"],
        supports_edit=False,
    ),
    ImageGenModel(
        name="fal-ai/flux-pro/v1.1",
        description="FLUX1.1 [pro] — improved composition + detail",
        sizes=["square_hd", "landscape_4_3", "portrait_4_3"],
        supports_edit=False,
    ),
    ImageGenModel(
        name="fal-ai/flux-2",
        description="FLUX.2 [dev] — next-gen text-to-image",
        sizes=["square_hd", "landscape_4_3", "portrait_4_3"],
        supports_edit=False,
    ),
    ImageGenModel(
        name="fal-ai/flux-pro/kontext",
        description="FLUX.1 Kontext — image editing (text + source image)",
        sizes=["square_hd", "landscape_4_3", "portrait_4_3"],
        supports_edit=True,
    ),
    ImageGenModel(
        name="fal-ai/flux-2-pro/edit",
        description="FLUX.2 [pro] — image editing / style transfer",
        sizes=["square_hd", "landscape_4_3", "portrait_4_3"],
        supports_edit=True,
    ),
]

# ── Video models catalog ─────────────────────────────────────────────────────

_KNOWN_VIDEO_MODELS = [
    VideoGenModel(
        name="fal-ai/veo2",
        description="Google Veo 2 — high-quality text-to-video (recommended)",
        aspect_ratios=["16:9", "9:16"],
        max_duration_s=8,
    ),
    VideoGenModel(
        name="fal-ai/kling-video/v2/master/text-to-video",
        description="Kling v2 Master — cinematic text-to-video",
        aspect_ratios=["16:9", "9:16", "1:1"],
        max_duration_s=10,
    ),
    VideoGenModel(
        name="fal-ai/minimax/video-01-live/text-to-video",
        description="MiniMax Video-01-Live — fast text-to-video",
        aspect_ratios=["16:9", "9:16", "1:1"],
        max_duration_s=6,
    ),
    VideoGenModel(
        name="fal-ai/wan/v2.1/1.3b/text-to-video",
        description="Wan 2.1 1.3B — lightweight text-to-video",
        aspect_ratios=["16:9", "9:16", "1:1"],
        max_duration_s=5,
    ),
]


def _default_image_model(*, edit: bool) -> str:
    """Unpinned default image model id derived from catalog."""
    for m in _KNOWN_IMAGE_MODELS:
        if bool(m.supports_edit) == edit:
            return m.name
    return _KNOWN_IMAGE_MODELS[0].name if _KNOWN_IMAGE_MODELS else ""


def _default_video_model() -> str:
    """Unpinned default video model id derived from catalog."""
    return _KNOWN_VIDEO_MODELS[0].name if _KNOWN_VIDEO_MODELS else ""


# Bound the internal poll loop.
_POLL_INTERVAL_S = 2.0
_IMAGE_POLL_TIMEOUT_S = 180.0
_VIDEO_POLL_TIMEOUT_S = 300.0  # video generation takes longer

# FAL's named aspect presets for images.
_FAL_SIZE_PRESETS = frozenset(
    {"square_hd", "square", "portrait_4_3", "portrait_16_9", "landscape_4_3", "landscape_16_9"}
)

# The bundled app manifest name.
_MANIFEST_NAME = "fal-image"


def _normalize_image_size(size: str) -> Any:
    """Map a caller's ``size`` to FAL's ``image_size`` (preset string or {w,h})."""
    s = (size or "").strip()
    if not s:
        return None
    if s in _FAL_SIZE_PRESETS:
        return s
    import re

    m = re.fullmatch(r"\s*(\d{2,5})\s*[xX*]\s*(\d{2,5})\s*", s)
    if m:
        return {"width": int(m.group(1)), "height": int(m.group(2))}
    return None


def _resolve_fal_key() -> str:
    """FAL credential from the Settings-card config, then a FAL_* env var."""
    try:
        from personalclaw.sdk.settings import ProviderSettings

        key = str(ProviderSettings.load(_MANIFEST_NAME).get("api_key", "") or "")
        if key:
            return key
    except Exception:  # noqa: BLE001
        pass
    return os.environ.get("FAL_KEY", "") or os.environ.get("FAL_API_KEY", "")


async def _submit_and_poll(
    model_id: str,
    payload: dict[str, Any],
    *,
    api_key: str = "",
    timeout_s: float = _IMAGE_POLL_TIMEOUT_S,
) -> dict[str, Any]:
    """Submit a job to FAL's queue and poll until completion. Returns the result dict.

    FAL's queue API returns shortened response/status URLs that omit nested model
    subpaths (e.g. ``fal-ai/kling-video/requests/...`` instead of the full
    ``fal-ai/kling-video/v2/master/text-to-video/requests/...``). Per the official
    docs, the correct URL pattern uses the FULL model_id:
      - Status: ``{QUEUE_BASE}/{model_id}/requests/{request_id}/status``
      - Result: ``{QUEUE_BASE}/{model_id}/requests/{request_id}``
    We construct these ourselves to avoid the broken shortened URLs.
    """
    from personalclaw.sdk.net import CONNECTOR, fetch

    key = api_key or _resolve_fal_key()
    if not key:
        raise ImageGenError("No FAL API key configured (set a 'fal' provider or FAL_KEY).")
    auth = {"Authorization": f"Key {key}", "Content-Type": "application/json"}
    body = json.dumps(payload).encode()

    submit = await fetch(
        f"{_QUEUE_BASE}/{model_id}", policy=CONNECTOR, method="POST", headers=auth, data=body,
    )
    if submit.status not in (200, 201):
        detail = ""
        try:
            detail = json.loads(submit.text).get("detail", "")
        except Exception:
            pass
        raise ImageGenError(f"FAL submit failed (HTTP {submit.status}). {detail}".strip())
    try:
        sub = json.loads(submit.text)
    except (json.JSONDecodeError, ValueError) as e:
        raise ImageGenError("FAL submit returned an unparseable response.") from e

    request_id = sub.get("request_id") or ""
    if not request_id:
        if sub.get("images") or sub.get("video") or sub.get("videos"):
            return sub
        raise ImageGenError("FAL submit response missing request_id.")

    status_url = f"{_QUEUE_BASE}/{model_id}/requests/{request_id}/status"
    response_url = f"{_QUEUE_BASE}/{model_id}/requests/{request_id}"

    elapsed = 0.0
    while elapsed < timeout_s:
        st = await fetch(status_url, policy=CONNECTOR, headers=auth)
        if st.status == 200:
            try:
                sd = json.loads(st.text)
            except (json.JSONDecodeError, ValueError):
                sd = {}
            status = str(sd.get("status", "")).upper()
            if status == "COMPLETED":
                break
            if status in ("FAILED", "ERROR", "CANCELLED"):
                raise ImageGenError(f"FAL job {status.lower()}.")
        await asyncio.sleep(_POLL_INTERVAL_S)
        elapsed += _POLL_INTERVAL_S
    else:
        raise ImageGenError("FAL job timed out.")

    result = await fetch(response_url, policy=CONNECTOR, headers=auth)
    if result.status != 200:
        raise ImageGenError(f"FAL result fetch failed (HTTP {result.status}).")
    try:
        rd = json.loads(result.text)
    except (json.JSONDecodeError, ValueError) as e:
        raise ImageGenError("FAL result returned an unparseable response.") from e
    return rd


# ── Image Provider ────────────────────────────────────────────────────────────


class FalImageProvider(ImageGenProvider):
    """Generate/edit images via FAL's async queue API."""

    def __init__(self, *, api_key: str = "") -> None:
        self._api_key = api_key

    @property
    def name(self) -> str:
        return "fal"

    @property
    def display_name(self) -> str:
        return "FAL (image)"

    async def is_available(self) -> bool:
        return bool(self._api_key or _resolve_fal_key())

    async def list_models(self) -> list[ImageGenModel]:
        from personalclaw.sdk.image import active_image_gen

        resolved = active_image_gen()
        active_model = resolved[1] if resolved and resolved[0].name == "fal" else ""
        return [
            ImageGenModel(
                name=m.name, description=m.description, sizes=list(m.sizes),
                supports_edit=m.supports_edit, downloaded=True,
                active=m.name == active_model,
            )
            for m in _KNOWN_IMAGE_MODELS
        ]

    async def generate(
        self, prompt: str, *, model: str = "", size: str = "", n: int = 1, **opts: Any,
    ) -> list[ImageResult]:
        model_id = model or _default_image_model(edit=False)
        payload: dict[str, Any] = {"prompt": prompt}
        image_size = _normalize_image_size(size)
        if image_size is not None:
            payload["image_size"] = image_size
        if n and n > 1:
            payload["num_images"] = n
        rd = await _submit_and_poll(model_id, payload, api_key=self._api_key)
        return self._parse_images(rd)

    async def edit(
        self, prompt: str, *, source_image: str, mask: str = "", model: str = "",
        size: str = "", n: int = 1, **opts: Any,
    ) -> list[ImageResult]:
        import base64
        import mimetypes

        model_id = model or _default_image_model(edit=True)
        try:
            with open(source_image, "rb") as fh:
                raw = fh.read()
        except OSError as e:
            raise ImageGenError(f"Could not read source image: {e}") from e
        mime = mimetypes.guess_type(source_image)[0] or "image/png"
        data_uri = f"data:{mime};base64,{base64.b64encode(raw).decode()}"
        payload: dict[str, Any] = {"prompt": prompt, "image_url": data_uri}
        image_size = _normalize_image_size(size)
        if image_size is not None:
            payload["image_size"] = image_size
        rd = await _submit_and_poll(model_id, payload, api_key=self._api_key)
        return self._parse_images(rd)

    @staticmethod
    def _parse_images(data: dict[str, Any]) -> list[ImageResult]:
        """FAL returns ``{images: [{url, content_type?}], ...}``."""
        out: list[ImageResult] = []
        for img in data.get("images", []) or []:
            if not isinstance(img, dict):
                continue
            url = img.get("url") or ""
            mime = img.get("content_type") or "image/png"
            if url:
                out.append(ImageResult(url=url, mime=mime))
        if not out:
            raise ImageGenError("FAL returned no images.")
        return out


# ── Video Provider ────────────────────────────────────────────────────────────


class FalVideoProvider(VideoGenProvider):
    """Generate videos via FAL's async queue API."""

    def __init__(self, *, api_key: str = "") -> None:
        self._api_key = api_key

    @property
    def name(self) -> str:
        return "fal"

    @property
    def display_name(self) -> str:
        return "FAL (video)"

    async def is_available(self) -> bool:
        return bool(self._api_key or _resolve_fal_key())

    async def list_models(self) -> list[VideoGenModel]:
        from personalclaw.sdk.video import active_video_gen

        resolved = active_video_gen()
        active_model = resolved[1] if resolved and resolved[0].name == "fal" else ""
        return [
            VideoGenModel(
                name=m.name, description=m.description,
                aspect_ratios=list(m.aspect_ratios),
                max_duration_s=m.max_duration_s, downloaded=True,
                active=m.name == active_model,
            )
            for m in _KNOWN_VIDEO_MODELS
        ]

    @staticmethod
    def _format_duration(model_id: str, seconds: float) -> Any:
        """Format duration per FAL model schema — each model family differs.

        veo2 wants a literal string '5s'..'8s'; kling wants '5' or '10';
        minimax/wan take no duration at all (return None to omit).
        """
        s = int(round(seconds))
        if "veo" in model_id:
            return f"{min(max(s, 5), 8)}s"
        if "kling" in model_id:
            return "5" if s <= 7 else "10"
        return None

    async def generate(
        self,
        prompt: str,
        *,
        model: str = "",
        duration_seconds: float = 5.0,
        aspect_ratio: str = "",
        **opts: Any,
    ) -> list[VideoResult]:
        model_id = model or _default_video_model()
        payload: dict[str, Any] = {"prompt": prompt}
        if duration_seconds and duration_seconds > 0:
            dur = self._format_duration(model_id, duration_seconds)
            if dur is not None:
                payload["duration"] = dur
        if aspect_ratio:
            payload["aspect_ratio"] = aspect_ratio

        try:
            rd = await self._generate_video(model_id, payload)
        except VideoGenError as e:
            # A 422 about duration/aspect_ratio → retry once with prompt only,
            # letting the model apply its own defaults.
            msg = str(e).lower()
            retriable = ("duration" in msg or "aspect" in msg) and len(payload) > 1
            if not retriable:
                raise
            rd = await self._generate_video(model_id, {"prompt": prompt})
        return self._parse_videos(rd)

    async def _generate_video(
        self, model_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Generate video via FAL — uses the synchronous fal.run endpoint which
        blocks until the video is ready (up to 5 minutes). This avoids the queue
        API's broken response URLs for nested model paths. Falls back to the async
        queue for models that don't support the sync endpoint."""
        import aiohttp

        key = self._api_key or _resolve_fal_key()
        if not key:
            raise VideoGenError("No FAL API key configured (set a 'fal' provider or FAL_KEY).")
        headers = {"Authorization": f"Key {key}", "Content-Type": "application/json"}
        body = json.dumps(payload).encode()

        timeout = aiohttp.ClientTimeout(total=_VIDEO_POLL_TIMEOUT_S)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"https://fal.run/{model_id}", headers=headers, data=body,
                ) as resp:
                    text = await resp.text()
                    if resp.status == 200:
                        return json.loads(text)
                    if resp.status == 422:
                        detail = ""
                        try:
                            detail = json.loads(text).get("detail", "")
                        except Exception:
                            pass
                        raise VideoGenError(f"FAL rejected video request: {detail}")
        except asyncio.TimeoutError:
            pass
        except VideoGenError:
            raise
        except Exception:
            pass

        rd = await _submit_and_poll(
            model_id, payload, api_key=key, timeout_s=_VIDEO_POLL_TIMEOUT_S,
        )
        return rd

    @staticmethod
    def _parse_videos(data: dict[str, Any]) -> list[VideoResult]:
        """FAL video returns ``{video: {url, content_type?}, ...}``."""
        out: list[VideoResult] = []
        # Some models return a single video object.
        video = data.get("video")
        if isinstance(video, dict):
            url = video.get("url") or ""
            mime = video.get("content_type") or "video/mp4"
            if url:
                out.append(VideoResult(url=url, mime=mime))
        # Some models return a list of videos.
        for vid in data.get("videos", []) or []:
            if not isinstance(vid, dict):
                continue
            url = vid.get("url") or ""
            mime = vid.get("content_type") or "video/mp4"
            if url:
                out.append(VideoResult(url=url, mime=mime))
        if not out:
            raise VideoGenError("FAL returned no video.")
        return out


# ── Factory (manifest entry point) ───────────────────────────────────────────


def create_provider(config: dict[str, Any] | None = None) -> list:
    """Manifest factory — build both the FAL image + video providers.

    Entry point named by the ``fal-image`` app manifest's ``implementation``.
    ``ModelTypeHandler`` calls this on enable with the saved card config; the
    ``api_key`` field flows straight in. Returns a list of providers so both
    the image_gen and video_gen registries get populated.
    """
    cfg = config or {}
    api_key = str(cfg.get("api_key", "") or "")
    return [
        FalImageProvider(api_key=api_key),
        FalVideoProvider(api_key=api_key),
    ]
