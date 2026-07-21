"""Google Gemini model provider (standalone app).

Provides:
  - **Chat/Code/Vision** via the OpenAI-compatible endpoint (``register_branded_app``)
  - **Embedding** via the same OpenAI-compat endpoint (``/embeddings``)
  - **Image generation** via the native Gemini ``generateContent`` API with
    ``responseModalities: ["IMAGE"]`` (gemini-*-image models), or Imagen's
    ``:predict`` for accounts that still have access to those models
  - **Video generation** (Veo) via the native ``:predictLongRunning`` API —
    submit, poll the long-running operation, then fetch the video asset

ALL models are DYNAMICALLY DISCOVERED from ``GET /v1beta/models`` and
categorized by each model's ``supportedGenerationMethods``:
  - ``predictLongRunning``            → video generation (Veo)
  - ``predict``                       → image generation (Imagen)
  - ``generateContent`` + image-output → image generation (gemini-*-image)
  - ``embedContent``                  → embedding
  - ``generateContent`` (the rest)    → chat

Base URLs:
  - OpenAI-compat: https://generativelanguage.googleapis.com/v1beta/openai/
  - Native Gemini: https://generativelanguage.googleapis.com/v1beta/

Auth: OpenAI-compat uses ``Authorization: Bearer {key}``; native endpoints use
the ``?key={key}`` query parameter.

Bring your own API key (config ``api_key`` or the ``GEMINI_API_KEY`` env var).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

from personalclaw.sdk.image import (
    ImageGenError,
    ImageGenModel,
    ImageGenProvider,
    ImageResult,
)
from personalclaw.sdk.model import BrandedProviderSpec, Capability, register_branded_app
from personalclaw.sdk.video import (
    VideoGenError,
    VideoGenModel,
    VideoGenProvider,
    VideoResult,
)

logger = logging.getLogger(__name__)

_OPENAI_COMPAT_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"
_NATIVE_BASE = "https://generativelanguage.googleapis.com/v1beta/"

_VIDEO_TIMEOUT_S = 300.0
_VIDEO_POLL_INTERVAL_S = 5.0
_IMAGE_TIMEOUT_S = 120.0

# ── Chat provider (branded, OpenAI-compat) ───────────────────────────────────

SPEC = BrandedProviderSpec(
    type="google",
    protocol="openai",
    default_base_url=_OPENAI_COMPAT_BASE,
    api_key_env="GEMINI_API_KEY",
    default_model="",  # resolved from live /v1/models discovery
    capabilities=frozenset({
        Capability.CHAT, Capability.CODE_TOOLS, Capability.STREAMING,
        Capability.VISION, Capability.EMBEDDING,
    }),
    fallback_models=(),
    notes="Google Gemini — chat, embedding, image gen, and video gen. Bring your own Gemini API key.",
)

_factory, _create_chat_provider, create_catalog = register_branded_app(SPEC)

# Register embedding catalog so the embedding adapter resolves `google:model` refs.
from personalclaw.sdk.model import MediaCatalog, MediaModel, register_media_catalog

register_media_catalog(
    "embedding", "google",
    MediaCatalog(
        models=(
            MediaModel(name="gemini-embedding-001", description="Gemini Embedding (3072 dims)"),
            MediaModel(name="gemini-embedding-2-preview", description="Gemini Embedding 2 (preview)"),
            MediaModel(name="gemini-embedding-2", description="Gemini Embedding 2"),
        ),
        default_model="gemini-embedding-001",
    ),
)

register_media_catalog(
    "tts", "google",
    MediaCatalog(
        models=(
            MediaModel(name="gemini-2.5-flash-preview-tts", description="Gemini 2.5 Flash TTS"),
            MediaModel(name="gemini-3.1-flash-tts-preview", description="Gemini 3.1 Flash TTS"),
        ),
        default_model="gemini-3.1-flash-tts-preview",
    ),
)


def _resolve_api_key(config: dict[str, Any] | None = None) -> str:
    """Resolve the Gemini API key from config or environment."""
    if config:
        key = str(config.get("api_key", "") or "")
        if key:
            return key
    return os.environ.get("GEMINI_API_KEY", "")


# ── Dynamic model discovery (shared, TTL-cached) ─────────────────────────────

_DISCOVERY_TTL_S = 300.0
_discovery_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


async def _discover_models(api_key: str) -> list[dict[str, Any]]:
    """Fetch the full model list from the native /models endpoint (TTL-cached).

    Each entry carries ``supportedGenerationMethods`` — the single source of
    truth for what a model can do. No hardcoded catalogs.
    """
    import aiohttp

    cached = _discovery_cache.get(api_key)
    now = time.monotonic()
    if cached and (now - cached[0]) < _DISCOVERY_TTL_S:
        return cached[1]

    url = f"{_NATIVE_BASE}models?key={api_key}&pageSize=200"
    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.debug("Gemini model discovery HTTP %s", resp.status)
                    return cached[1] if cached else []
                data = json.loads(await resp.text())
    except Exception:
        logger.debug("Gemini model discovery failed", exc_info=True)
        return cached[1] if cached else []

    models = data.get("models", []) or []
    _discovery_cache[api_key] = (now, models)
    return models


def _model_id(m: dict[str, Any]) -> str:
    return str(m.get("name", "")).removeprefix("models/")


def _is_video_gen(m: dict[str, Any]) -> bool:
    return "predictLongRunning" in m.get("supportedGenerationMethods", [])


def _is_imagen(m: dict[str, Any]) -> bool:
    return "predict" in m.get("supportedGenerationMethods", [])


def _is_content_image(m: dict[str, Any]) -> bool:
    """generateContent models that OUTPUT images (gemini-*-image family)."""
    mid = _model_id(m).lower()
    return (
        "generateContent" in m.get("supportedGenerationMethods", [])
        and ("image" in mid or "banana" in mid)
        and "tts" not in mid
    )


# ── Image Provider ────────────────────────────────────────────────────────────


class GeminiImageProvider(ImageGenProvider):
    """Image generation via the Gemini API — models discovered live.

    Two generation paths, chosen per-model by its supported method:
      - ``predict`` (Imagen) → OpenAI-compat ``/images/generations``
      - ``generateContent`` image-output models → native generateContent with
        ``responseModalities: ["IMAGE"]`` (returns inline base64)
    """

    def __init__(self, *, api_key: str = "", name: str = "google") -> None:
        self._api_key = api_key
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @name.setter
    def name(self, value: str) -> None:
        self._name = value

    @property
    def display_name(self) -> str:
        return "Google Gemini (image)"

    def _key(self) -> str:
        return self._api_key or os.environ.get("GEMINI_API_KEY", "")

    async def is_available(self) -> bool:
        return bool(self._key())

    async def list_models(self) -> list[ImageGenModel]:
        from personalclaw.sdk.image import active_image_gen

        resolved = active_image_gen()
        active_model = resolved[1] if resolved and resolved[0].name == "google" else ""

        discovered = await _discover_models(self._key())
        out: list[ImageGenModel] = []
        for m in discovered:
            if _is_imagen(m) or _is_content_image(m):
                mid = _model_id(m)
                out.append(ImageGenModel(
                    name=mid,
                    description=str(m.get("description", "") or m.get("displayName", "")),
                    sizes=[],
                    supports_edit=False,
                    downloaded=True,
                    active=mid == active_model,
                ))
        return out

    async def generate(
        self, prompt: str, *, model: str = "", size: str = "", n: int = 1, **opts: Any,
    ) -> list[ImageResult]:
        key = self._key()
        if not key:
            raise ImageGenError("No Gemini API key configured (set GEMINI_API_KEY).")

        # The bound model id arrives as "models/…" from split_ref; strip the
        # prefix so URL construction doesn't double it (models/models/… → 404).
        model_id = (model or "").removeprefix("models/")
        discovered = await _discover_models(key)
        by_id = {_model_id(m): m for m in discovered}
        if not model_id:
            # Prefer a generateContent image model (broadly available), else Imagen.
            for m in discovered:
                if _is_content_image(m):
                    model_id = _model_id(m)
                    break
            if not model_id:
                for m in discovered:
                    if _is_imagen(m):
                        model_id = _model_id(m)
                        break
        if not model_id:
            raise ImageGenError("No Gemini image-generation model discovered.")

        meta = by_id.get(model_id, {})
        if _is_imagen(meta):
            return await self._generate_via_predict(model_id, prompt, size=size, n=n, key=key)
        return await self._generate_via_content(model_id, prompt, key=key)

    async def _generate_via_predict(
        self, model_id: str, prompt: str, *, size: str, n: int, key: str,
    ) -> list[ImageResult]:
        """Imagen path — OpenAI-compat /images/generations."""
        import aiohttp

        url = f"{_OPENAI_COMPAT_BASE}images/generations"
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        body: dict[str, Any] = {"model": model_id, "prompt": prompt, "n": n}
        if size:
            body["size"] = size

        timeout = aiohttp.ClientTimeout(total=_IMAGE_TIMEOUT_S)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, json=body) as resp:
                    text = await resp.text()
                    if resp.status != 200:
                        raise ImageGenError(
                            f"Imagen generation failed (HTTP {resp.status}): "
                            f"{_error_detail(text)}"
                        )
                    data = json.loads(text)
        except ImageGenError:
            raise
        except asyncio.TimeoutError as e:
            raise ImageGenError("Imagen generation timed out.") from e
        except Exception as e:
            raise ImageGenError(f"Imagen generation request failed: {e}") from e

        results: list[ImageResult] = []
        for item in data.get("data", []):
            if not isinstance(item, dict):
                continue
            img_url = item.get("url", "")
            b64 = item.get("b64_json", "")
            if img_url or b64:
                results.append(ImageResult(
                    url=img_url, b64=b64,
                    revised_prompt=item.get("revised_prompt", ""),
                ))
        if not results:
            raise ImageGenError("Imagen returned no images.")
        return results

    async def _generate_via_content(
        self, model_id: str, prompt: str, *, key: str,
    ) -> list[ImageResult]:
        """gemini-*-image path — generateContent with IMAGE response modality."""
        import aiohttp

        url = f"{_NATIVE_BASE}models/{model_id}:generateContent?key={key}"
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["IMAGE"]},
        }
        timeout = aiohttp.ClientTimeout(total=_IMAGE_TIMEOUT_S)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    url, headers={"Content-Type": "application/json"}, json=body,
                ) as resp:
                    text = await resp.text()
                    if resp.status != 200:
                        raise ImageGenError(
                            f"Gemini image generation failed (HTTP {resp.status}): "
                            f"{_error_detail(text)}"
                        )
                    data = json.loads(text)
        except ImageGenError:
            raise
        except asyncio.TimeoutError as e:
            raise ImageGenError("Gemini image generation timed out.") from e
        except Exception as e:
            raise ImageGenError(f"Gemini image generation request failed: {e}") from e

        results: list[ImageResult] = []
        for cand in data.get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                inline = part.get("inlineData", {})
                if inline and str(inline.get("mimeType", "")).startswith("image/"):
                    b64 = inline.get("data", "")
                    if b64:
                        results.append(ImageResult(
                            b64=b64, mime=inline.get("mimeType", "image/png"),
                        ))
        if not results:
            raise ImageGenError("Gemini returned no image in the response.")
        return results

    async def edit(
        self, prompt: str, *, source_image: str, mask: str = "", model: str = "",
        size: str = "", n: int = 1, **opts: Any,
    ) -> list[ImageResult]:
        raise ImageGenError("Gemini image editing is not supported yet.")


# ── Video Provider (Veo via predictLongRunning) ──────────────────────────────


class GeminiVideoProvider(VideoGenProvider):
    """Video generation via Veo's long-running-operation API.

    ``generate()`` performs the full cycle the platform contract expects:
    submit (:predictLongRunning) → poll the operation until done → return the
    video URI (the capability layer fetches + materializes the bytes).
    """

    def __init__(self, *, api_key: str = "", name: str = "google") -> None:
        self._api_key = api_key
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @name.setter
    def name(self, value: str) -> None:
        self._name = value

    @property
    def display_name(self) -> str:
        return "Google Veo"

    def _key(self) -> str:
        return self._api_key or os.environ.get("GEMINI_API_KEY", "")

    async def is_available(self) -> bool:
        return bool(self._key())

    async def list_models(self) -> list[VideoGenModel]:
        from personalclaw.sdk.video import active_video_gen

        resolved = active_video_gen()
        active_model = resolved[1] if resolved and resolved[0].name == "google" else ""

        discovered = await _discover_models(self._key())
        out: list[VideoGenModel] = []
        for m in discovered:
            if _is_video_gen(m):
                mid = _model_id(m)
                out.append(VideoGenModel(
                    name=mid,
                    description=str(m.get("description", "") or m.get("displayName", "")),
                    aspect_ratios=["16:9", "9:16"],
                    max_duration_s=8,
                    downloaded=True,
                    active=mid == active_model,
                ))
        return out

    async def generate(
        self,
        prompt: str,
        *,
        model: str = "",
        duration_seconds: float = 5.0,
        aspect_ratio: str = "",
        **opts: Any,
    ) -> list[VideoResult]:
        key = self._key()
        if not key:
            raise VideoGenError("No Gemini API key configured (set GEMINI_API_KEY).")

        # Strip the "models/" prefix the binding carries (split_ref keeps it);
        # URL construction prepends it, so without the strip → models/models/… → 404.
        model_id = (model or "").removeprefix("models/")
        if not model_id:
            for m in await _discover_models(key):
                if _is_video_gen(m):
                    model_id = _model_id(m)
                    break
        if not model_id:
            raise VideoGenError("No Gemini video-generation (Veo) model discovered.")

        op_name = await self._submit(model_id, prompt, aspect_ratio=aspect_ratio, key=key)
        return await self._poll_and_fetch(op_name, key=key)

    async def _submit(
        self, model_id: str, prompt: str, *, aspect_ratio: str, key: str,
    ) -> str:
        """Submit the generation job; returns the long-running operation name."""
        import aiohttp

        url = f"{_NATIVE_BASE}models/{model_id}:predictLongRunning?key={key}"
        parameters: dict[str, Any] = {}
        if aspect_ratio:
            parameters["aspectRatio"] = aspect_ratio
        body: dict[str, Any] = {"instances": [{"prompt": prompt}]}
        if parameters:
            body["parameters"] = parameters

        timeout = aiohttp.ClientTimeout(total=60)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    url, headers={"Content-Type": "application/json"}, json=body,
                ) as resp:
                    text = await resp.text()
                    if resp.status != 200:
                        raise VideoGenError(
                            f"Veo submit failed (HTTP {resp.status}): {_error_detail(text)}"
                        )
                    data = json.loads(text)
        except VideoGenError:
            raise
        except Exception as e:
            raise VideoGenError(f"Veo submit request failed: {e}") from e

        op_name = str(data.get("name", ""))
        if not op_name:
            raise VideoGenError("Veo submit returned no operation name.")
        return op_name

    async def _poll_and_fetch(self, op_name: str, *, key: str) -> list[VideoResult]:
        """Poll the operation until done, then extract the video URI(s)."""
        import aiohttp

        url = f"{_NATIVE_BASE}{op_name}?key={key}"
        timeout = aiohttp.ClientTimeout(total=30)
        elapsed = 0.0
        data: dict[str, Any] = {}
        while elapsed < _VIDEO_TIMEOUT_S:
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url) as resp:
                        text = await resp.text()
                        if resp.status == 200:
                            data = json.loads(text)
                            if data.get("done"):
                                break
            except Exception:
                logger.debug("Veo poll error", exc_info=True)
            await asyncio.sleep(_VIDEO_POLL_INTERVAL_S)
            elapsed += _VIDEO_POLL_INTERVAL_S
        else:
            raise VideoGenError("Veo generation timed out (operation not done).")

        err = data.get("error")
        if err:
            raise VideoGenError(f"Veo generation failed: {err.get('message', err)}")

        results: list[VideoResult] = []
        response = data.get("response", {})
        # Documented shape: response.generateVideoResponse.generatedSamples[].video.uri
        gv = response.get("generateVideoResponse", {})
        for sample in gv.get("generatedSamples", []) or []:
            uri = (sample.get("video") or {}).get("uri", "")
            if uri:
                results.append(VideoResult(url=_with_key(uri, key), mime="video/mp4"))
        # Alternate shape: response.predictions[].video / videoUri / bytesBase64Encoded
        for pred in response.get("predictions", []) or []:
            if not isinstance(pred, dict):
                continue
            uri = str(pred.get("videoUri", "") or "")
            video = pred.get("video")
            if not uri and isinstance(video, dict):
                uri = str(video.get("uri", "") or "")
            if uri:
                results.append(VideoResult(url=_with_key(uri, key), mime="video/mp4"))
                continue
            b64 = pred.get("bytesBase64Encoded", "")
            if b64:
                results.append(VideoResult(
                    url=f"data:video/mp4;base64,{b64}", mime="video/mp4",
                ))

        if not results:
            raise VideoGenError("Veo returned no video in the operation result.")
        return results


def _with_key(uri: str, key: str) -> str:
    """Veo file URIs require the API key to download."""
    if "generativelanguage.googleapis.com" in uri and "key=" not in uri:
        sep = "&" if "?" in uri else "?"
        return f"{uri}{sep}key={key}"
    return uri


# ── TTS Provider ─────────────────────────────────────────────────────────────


class GeminiTTSProvider:
    """Text-to-speech via the Gemini native generateContent API.

    Uses ``responseModalities: ["AUDIO"]`` with prebuilt voice config.
    Returns raw L16 audio (24 kHz, mono) decoded from base64 inline data.
    """

    def __init__(self, *, api_key: str = "", name: str = "google") -> None:
        self._api_key = api_key
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @name.setter
    def name(self, value: str) -> None:
        self._name = value

    @property
    def display_name(self) -> str:
        return "Google Gemini TTS"

    def _key(self) -> str:
        return self._api_key or os.environ.get("GEMINI_API_KEY", "")

    def is_available(self) -> bool:
        return bool(self._key())

    async def synthesize(
        self,
        text: str,
        voice: str = "",
        output_path: str = "",
        *,
        speed: float = 1.0,
        **opts: Any,
    ) -> str | None:
        """Synthesize speech from *text* and write audio to *output_path*.

        Returns the output file path on success, or None on failure.
        """
        import aiohttp
        import base64
        import tempfile

        key = self._key()
        if not key:
            logger.warning("GeminiTTS: no API key available.")
            return None

        # ``voice`` carries the bound TTS model id, which may arrive as either a
        # bare id or the fully-qualified ``models/…`` name (split_ref keeps the
        # prefix). Strip it so the URL isn't doubled (``models/models/…`` → 404).
        model = (voice or "gemini-3.1-flash-tts-preview").removeprefix("models/")
        url = f"{_NATIVE_BASE}models/{model}:generateContent?key={key}"

        body = {
            "contents": [{"parts": [{"text": text}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {
                            "voiceName": "Kore",
                        }
                    }
                },
            },
        }

        timeout = aiohttp.ClientTimeout(total=60)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    url, headers={"Content-Type": "application/json"}, json=body,
                ) as resp:
                    text_resp = await resp.text()
                    if resp.status != 200:
                        logger.error(
                            "GeminiTTS: HTTP %s — %s", resp.status,
                            _error_detail(text_resp),
                        )
                        return None
                    data = json.loads(text_resp)
        except asyncio.TimeoutError:
            logger.error("GeminiTTS: request timed out.")
            return None
        except Exception:
            logger.error("GeminiTTS: request failed", exc_info=True)
            return None

        # Extract inline audio data from response.
        inline = None
        for cand in data.get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                inline_data = part.get("inlineData", {})
                if inline_data and "audio" in str(inline_data.get("mimeType", "")):
                    inline = inline_data
                    break
            if inline:
                break

        if not inline or not inline.get("data"):
            logger.error("GeminiTTS: no audio in response.")
            return None

        pcm_bytes = base64.b64decode(inline["data"])

        # Gemini returns raw PCM (audio/l16, 24000Hz, 1ch, 16-bit).
        # The voice pipeline + browser AudioContext need a WAV container.
        mime = str(inline.get("mimeType", ""))
        sample_rate = 24000
        channels = 1
        if "rate=" in mime:
            try:
                sample_rate = int(mime.split("rate=")[1].split(";")[0].strip())
            except (ValueError, IndexError):
                pass
        if "channels=" in mime:
            try:
                channels = int(mime.split("channels=")[1].split(";")[0].strip())
            except (ValueError, IndexError):
                pass

        import struct
        bits_per_sample = 16
        byte_rate = sample_rate * channels * (bits_per_sample // 8)
        block_align = channels * (bits_per_sample // 8)
        data_size = len(pcm_bytes)
        wav_header = struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF", 36 + data_size, b"WAVE",
            b"fmt ", 16, 1, channels, sample_rate,
            byte_rate, block_align, bits_per_sample,
            b"data", data_size,
        )

        if not output_path:
            fd, output_path = tempfile.mkstemp(suffix=".wav")
            os.close(fd)

        with open(output_path, "wb") as f:
            f.write(wav_header)
            f.write(pcm_bytes)

        return output_path


def _error_detail(text: str) -> str:
    try:
        return str(json.loads(text).get("error", {}).get("message", ""))[:200]
    except Exception:
        return text[:200]


# ── Chat factory (multiInstance manifest entry point) ─────────────────────────


def create_provider(config: dict[str, Any] | None = None):
    """Chat provider factory (multi-instance, OpenAI-compat endpoint).

    One ``google`` config entry serves chat + embedding (via the OpenAI-compat
    endpoint) AND image / video / TTS (via the media scanners below). The image/
    video/TTS adapters are built by the media_scanners extension point per config
    entry — NOT as separate provider blocks — so the app surfaces as ONE provider.
    """
    return _create_chat_provider(config or {})


# ── Media-capability config scanners ─────────────────────────────────────────
# The image/video/TTS capabilities resolve through their own registries, which
# build a per-config adapter. Core knows the OpenAI-family built-in; Google
# contributes its adapters via the app-owned ``media_scanners`` extension point.
# Each scanner returns an adapter per config entry of type ``google``, keyed by
# the entry's name so ``<name>:model`` refs resolve to that entry's key.


def _google_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for e in entries:
        ptype = str(e.get("type", ""))
        if ptype == "google" or str((e.get("options") or {}).get("_original_type", "")) == "google":
            out.append(e)
    return out


def _entry_key(e: dict[str, Any]) -> str:
    return str((e.get("options") or {}).get("api_key", "") or "")


def _scan_image(entries: list[dict[str, Any]]) -> list:
    return [
        GeminiImageProvider(api_key=_entry_key(e), name=str(e["name"]))
        for e in _google_entries(entries)
    ]


def _scan_video(entries: list[dict[str, Any]]) -> list:
    return [
        GeminiVideoProvider(api_key=_entry_key(e), name=str(e["name"]))
        for e in _google_entries(entries)
    ]


def _scan_tts(entries: list[dict[str, Any]]) -> list:
    return [
        GeminiTTSProvider(api_key=_entry_key(e), name=str(e["name"]))
        for e in _google_entries(entries)
    ]


try:
    from personalclaw.sdk.model import register_scanner as _reg_scanner

    _reg_scanner("image_gen", _scan_image)
    _reg_scanner("video_gen", _scan_video)
    _reg_scanner("tts", _scan_tts)
except Exception:  # noqa: BLE001 — older core without the extension point
    logger.debug("media_scanners extension point unavailable", exc_info=True)
