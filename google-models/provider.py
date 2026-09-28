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

Auth: OpenAI-compat uses ``Authorization: Bearer {key}``; the native image, video, speech and
model-list calls send the key in the ``x-goog-api-key`` header, the form Gemini documents,
so no URL they ask for carries it. An HTTP library's error, a traceback and a log line can
each quote a URL.

Bring your own API key (config ``api_key`` or the ``GEMINI_API_KEY`` env var).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import tempfile
import time
from typing import Any
from urllib.parse import urljoin, urlsplit

from personalclaw.sdk.image import (
    ImageGenError,
    ImageGenModel,
    ImageGenProvider,
    ImageResult,
)
from personalclaw.sdk.model import (
    BrandedProviderSpec,
    Capability,
    PromptCache,
    ProviderResolutionError,
    register_branded_app,
    require_model,
)
from personalclaw.sdk.net import sentence_with_detail
from personalclaw.sdk.tts import TtsProvider
from personalclaw.sdk.video import (
    VideoGenError,
    VideoGenModel,
    VideoGenProvider,
    VideoResult,
)

logger = logging.getLogger(__name__)

# Where an instance's key is set, and the variable read when that field is empty. The media calls
# go to Gemini's own addresses, never the instance's Base URL, so no sentence points there.
_KEY_SETTING = (
    "Google Gemini API Key on this Google Gemini instance in Settings → Providers "
    "(or GEMINI_API_KEY)"
)
#: Why a media adapter with no key is unavailable, and what its call is refused with.
_NO_KEY = f"No Gemini API key is set. Add it as {_KEY_SETTING}."
_NO_IMAGE = (
    "Gemini's answer to the {what} request had no image in it. Try again; if it happens again, "
    "change the prompt or choose another model in Settings → Models."
)


def _named(model: str, error: type[Exception]) -> str:
    """The model a media call names, or ``error`` with the SDK's refusal when it names none."""
    try:
        return require_model(model)
    except ProviderResolutionError as exc:
        raise error(str(exc)) from exc


_GEMINI_HOST = "generativelanguage.googleapis.com"
_OPENAI_COMPAT_BASE = f"https://{_GEMINI_HOST}/v1beta/openai/"
_NATIVE_BASE = f"https://{_GEMINI_HOST}/v1beta/"


def _key_header(key: str) -> dict[str, str]:
    """The header a native call sends its API key in (Gemini's documented ``x-goog-api-key``)."""
    return {"x-goog-api-key": key}


# 600s, not 300s: a timeout that fires on a job the upstream provider is still
# happily working on reports FAILURE for something that succeeds — and the user is
# billed either way. Veo's long-running operation can outlive 5 minutes under load,
# so the ceiling is set by the slowest legitimate job, not the median one.
_VIDEO_TIMEOUT_S = 600.0
_VIDEO_POLL_INTERVAL_S = 5.0
_IMAGE_TIMEOUT_S = 120.0
# Fetching the finished video. Veo's clips are seconds long, so both bounds are generous; they
# exist so a server that never stops sending cannot fill the disk.
_VIDEO_DOWNLOAD_TIMEOUT_S = 180.0
_VIDEO_MAX_BYTES = 200 * 1024 * 1024
_MAX_REDIRECTS = 5


class _NotAnObject(ValueError):
    """A 200 whose body is JSON but not the object every one of these calls answers with."""


_JSON_KINDS = {
    list: "a list", str: "a string", int: "a number", float: "a number", bool: "true or false",
    type(None): "null",
}


def _json_object(text: str) -> dict[str, Any]:
    """Gemini's answer, parsed. A list, a string or a number (what a proxy or a changed API can
    answer with) is refused like a body that is not JSON; each of them used to raise
    AttributeError at the first ``.get``, which reached the user as no sentence at all."""
    data = json.loads(text)
    if not isinstance(data, dict):
        kind = _JSON_KINDS.get(type(data), "something else")
        raise _NotAnObject(f"the answer is JSON but {kind}, not an object")
    return data


def _object(value: Any) -> dict[str, Any]:
    """``value`` when it is a JSON object, else an empty one: a field of the wrong type in an
    answer is read as missing, never as an AttributeError."""
    return value if isinstance(value, dict) else {}


def _objects(value: Any) -> list[dict[str, Any]]:
    """The objects in ``value`` when it is a JSON list; anything else holds none."""
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


# ── Chat provider (branded, OpenAI-compat) ───────────────────────────────────

SPEC = BrandedProviderSpec(
    type="google",
    protocol="openai",
    default_base_url=_OPENAI_COMPAT_BASE,
    api_key_env="GEMINI_API_KEY",
    default_model="",  # no curated pick: a call names its binding or the instance's Default Model
    capabilities=frozenset({
        Capability.CHAT, Capability.CODE_TOOLS, Capability.STREAMING,
        Capability.VISION, Capability.EMBEDDING,
    }),
    fallback_models=(),
    # AUTOMATIC - "Implicit caching is enabled by default for all Gemini 2.5 and newer
    # models" (Gemini API caching docs): no setup, no per-request marker, cost savings
    # passed on when a request hits. Gemini's explicit cache is a separate cache-OBJECT
    # API rather than a content marker, so it is outside this contract entirely.
    prompt_cache=PromptCache.AUTOMATIC,
    notes="Google Gemini — chat, embedding, image gen, and video gen. Bring your own Gemini API key.",
)

_factory, _create_chat_provider, create_catalog = register_branded_app(SPEC)

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

    url = f"{_NATIVE_BASE}models?pageSize=200"
    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                url, headers=_key_header(api_key), allow_redirects=False,
            ) as resp:
                if resp.status != 200:
                    logger.debug("Gemini model discovery HTTP %s", resp.status)
                    return cached[1] if cached else []
                data = _json_object(await resp.text())
    except Exception:
        logger.debug("Gemini model discovery failed", exc_info=True)
        return cached[1] if cached else []

    models = _objects(data.get("models"))
    _discovery_cache[api_key] = (now, models)
    return models


def _model_id(m: dict[str, Any]) -> str:
    return str(m.get("name", "")).removeprefix("models/")


def _methods(m: dict[str, Any]) -> list[Any]:
    methods = m.get("supportedGenerationMethods")
    return methods if isinstance(methods, list) else []


def _is_video_gen(m: dict[str, Any]) -> bool:
    return "predictLongRunning" in _methods(m)


def _is_imagen(m: dict[str, Any]) -> bool:
    return "predict" in _methods(m)


def _is_content_image(m: dict[str, Any]) -> bool:
    """generateContent models that OUTPUT images (gemini-*-image family)."""
    mid = _model_id(m).lower()
    return (
        "generateContent" in _methods(m)
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

    async def unavailable_reason(self) -> str:
        """Why images can't be made, for Settings → Models: a missing key is the only way."""
        return "" if self._key() else _NO_KEY

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
        # Like chat, a call names its model (the image binding in Settings → Models), and it
        # is refused when it names none. This used to take the first image model discovery
        # listed. The bound id arrives as "models/…" from split_ref; strip the prefix so URL
        # construction doesn't double it (models/models/… → 404).
        model_id = _named(model, ImageGenError).removeprefix("models/")
        key = self._key()
        if not key:
            raise ImageGenError(_NO_KEY)

        # Discovery says which API the named model speaks (Imagen's predict, or generateContent).
        by_id = {_model_id(m): m for m in await _discover_models(key)}
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
                            _status_message(resp.status, text, what="Imagen", key=key)
                        )
                    data = _json_object(text)
        except ImageGenError:
            raise
        except asyncio.TimeoutError as e:
            raise ImageGenError("Imagen generation timed out.") from e
        except Exception as e:
            raise ImageGenError(_unanswered_message(e, what="Imagen", key=key)) from e

        results: list[ImageResult] = []
        for item in _objects(data.get("data")):
            img_url = str(item.get("url") or "")
            b64 = str(item.get("b64_json") or "")
            if img_url or b64:
                results.append(ImageResult(
                    url=img_url, b64=b64,
                    revised_prompt=str(item.get("revised_prompt") or ""),
                ))
        if not results:
            raise ImageGenError(_NO_IMAGE.format(what="Imagen"))
        return results

    async def _generate_via_content(
        self, model_id: str, prompt: str, *, key: str,
    ) -> list[ImageResult]:
        """gemini-*-image path — generateContent with IMAGE response modality."""
        import aiohttp

        url = f"{_NATIVE_BASE}models/{model_id}:generateContent"
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["IMAGE"]},
        }
        timeout = aiohttp.ClientTimeout(total=_IMAGE_TIMEOUT_S)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    url, headers={**_key_header(key), "Content-Type": "application/json"},
                    json=body, allow_redirects=False,
                ) as resp:
                    text = await resp.text()
                    if resp.status != 200:
                        raise ImageGenError(
                            _status_message(resp.status, text, what="image", key=key)
                        )
                    data = _json_object(text)
        except ImageGenError:
            raise
        except asyncio.TimeoutError as e:
            raise ImageGenError("Gemini image generation timed out.") from e
        except Exception as e:
            raise ImageGenError(_unanswered_message(e, what="image", key=key)) from e

        results: list[ImageResult] = []
        for cand in _objects(data.get("candidates")):
            for part in _objects(_object(cand.get("content")).get("parts")):
                inline = _object(part.get("inlineData"))
                mime = str(inline.get("mimeType") or "")
                b64 = str(inline.get("data") or "")
                if mime.startswith("image/") and b64:
                    results.append(ImageResult(b64=b64, mime=mime))
        if not results:
            raise ImageGenError(_NO_IMAGE.format(what="image"))
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

    async def unavailable_reason(self) -> str:
        """Why video can't be made, for Settings → Models: a missing key is the only way."""
        return "" if self._key() else _NO_KEY

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
        # Like chat, a call names its model (the video binding), and it is refused when it names
        # none; this used to take the first Veo model discovery listed. Strip the "models/"
        # prefix the binding carries (split_ref keeps it): URL construction prepends it.
        model_id = _named(model, VideoGenError).removeprefix("models/")
        key = self._key()
        if not key:
            raise VideoGenError(_NO_KEY)

        op_name = await self._submit(model_id, prompt, aspect_ratio=aspect_ratio, key=key)
        return await self._poll_and_fetch(op_name, key=key)

    async def _submit(
        self, model_id: str, prompt: str, *, aspect_ratio: str, key: str,
    ) -> str:
        """Submit the generation job; returns the long-running operation name."""
        import aiohttp

        url = f"{_NATIVE_BASE}models/{model_id}:predictLongRunning"
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
                    url, headers={**_key_header(key), "Content-Type": "application/json"},
                    json=body, allow_redirects=False,
                ) as resp:
                    text = await resp.text()
                    if resp.status != 200:
                        raise VideoGenError(
                            _status_message(resp.status, text, what="Veo video", key=key)
                        )
                    data = _json_object(text)
        except VideoGenError:
            raise
        except Exception as e:
            raise VideoGenError(_unanswered_message(e, what="Veo video", key=key)) from e

        op_name = str(data.get("name") or "")
        if not op_name:
            raise VideoGenError(
                "Gemini answered the Veo video request without naming the job it started, so "
                "no video can be fetched. Try again in a moment."
            )
        return op_name

    async def _poll_and_fetch(self, op_name: str, *, key: str) -> list[VideoResult]:
        """Poll the operation until done, then fetch the video(s) it made.

        A poll that fails in a way another poll can outlast (a 429, a 5xx, a dropped
        connection) is retried until the deadline; one that cannot (the key refused, the job
        unknown, an answer that is not the JSON object Gemini sends) says so at once, rather
        than after ten minutes as a timeout.
        """
        import aiohttp

        url = f"{_NATIVE_BASE}{op_name}"
        timeout = aiohttp.ClientTimeout(total=30)
        elapsed = 0.0
        data: dict[str, Any] = {}
        last_problem = ""
        while elapsed < _VIDEO_TIMEOUT_S:
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(
                        url, headers=_key_header(key), allow_redirects=False,
                    ) as resp:
                        text = await resp.text()
                        if resp.status == 200:
                            data = _json_object(text)
                            if data.get("done"):
                                break
                        elif resp.status == 404:
                            raise VideoGenError(sentence_with_detail(
                                "Gemini no longer knows the Veo video job PersonalClaw was "
                                "waiting on (HTTP 404), so its video can't be fetched. Try "
                                "again.",
                                _scrubbed(_error_detail(text), key),
                            ))
                        elif 400 <= resp.status < 500 and resp.status not in (408, 429):
                            raise VideoGenError(
                                _status_message(resp.status, text, what="Veo video", key=key)
                            )
                        else:
                            last_problem = f"HTTP {resp.status}: {_error_detail(text)}"
            except VideoGenError:
                raise
            except (_NotAnObject, json.JSONDecodeError, UnicodeDecodeError) as e:
                raise VideoGenError(_unanswered_message(e, what="Veo video", key=key)) from e
            except Exception as e:  # noqa: BLE001 — a dropped poll is retried until the deadline
                logger.debug("Veo poll error", exc_info=True)
                last_problem = str(e) or type(e).__name__
            await asyncio.sleep(_VIDEO_POLL_INTERVAL_S)
            elapsed += _VIDEO_POLL_INTERVAL_S
        else:
            raise VideoGenError(sentence_with_detail(
                f"Veo's video job did not finish within {int(_VIDEO_TIMEOUT_S // 60)} minutes, "
                "so PersonalClaw stopped waiting and no video was saved. Try again later; if it "
                "keeps happening, choose another model in Settings → Models.",
                _scrubbed(last_problem, key),
            ))

        err = data.get("error")
        if err:
            reason = err.get("message", err) if isinstance(err, dict) else err
            raise VideoGenError(sentence_with_detail(
                "Veo's video job failed, so no video was made. Try again; if it fails again, "
                "change the prompt or choose another model in Settings → Models.",
                _scrubbed(str(reason), key),
            ))

        results: list[VideoResult] = []
        response = _object(data.get("response"))
        # Documented shape: response.generateVideoResponse.generatedSamples[].video.uri
        gv = _object(response.get("generateVideoResponse"))
        for sample in _objects(gv.get("generatedSamples")):
            uri = str(_object(sample.get("video")).get("uri") or "")
            if uri:
                results.append(await _video_file(uri, key=key))
        # Alternate shape: response.predictions[].video / videoUri / bytesBase64Encoded
        for pred in _objects(response.get("predictions")):
            uri = str(pred.get("videoUri") or _object(pred.get("video")).get("uri") or "")
            if uri:
                results.append(await _video_file(uri, key=key))
                continue
            b64 = str(pred.get("bytesBase64Encoded") or "")
            if b64:
                try:
                    video = base64.b64decode(b64, validate=True)
                except (binascii.Error, ValueError):
                    continue
                results.append(VideoResult(local_path=_saved_video(video), mime="video/mp4"))

        if not results:
            raise VideoGenError(
                "Veo's video job finished, but Gemini's answer had no video in it. Try again; if "
                "it happens again, change the prompt or choose another model in Settings → "
                "Models."
            )
        return results


# ── Fetching a finished video ────────────────────────────────────────────────


def _on_gemini(url: str) -> bool:
    """Whether ``url`` is Gemini's own address: the one host the API key is ever sent to. The
    host is compared, not searched for, so an address that only mentions it gets no key."""
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.hostname == _GEMINI_HOST


def _saved_video(video: bytes) -> str:
    """Write ``video`` to a new file of its own and return the file's path, for core to save."""
    fd, path = tempfile.mkstemp(prefix="gemini-video-", suffix=".mp4")
    with os.fdopen(fd, "wb") as f:
        f.write(video)
    return path


async def _video_file(uri: str, *, key: str) -> VideoResult:
    """The video Veo made at ``uri``, as core will save it.

    A file on Gemini's own address needs the API key, and the key goes in the header, never the
    URL, so this fetches it here: core would ask for a URL with no way to add the header.
    Gemini's own instructions fetch it with redirects followed. The key is sent only while the
    address stays on Gemini's host; an address anywhere else, whether ``uri`` itself or where a
    redirect leads, is handed to core as it is, and core fetches it through its egress guard,
    with no key.
    """
    import aiohttp

    if not _on_gemini(uri):
        return VideoResult(url=uri, mime="video/mp4")
    url = uri
    timeout = aiohttp.ClientTimeout(total=_VIDEO_DOWNLOAD_TIMEOUT_S)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for _hop in range(_MAX_REDIRECTS + 1):
                async with session.get(
                    url, headers=_key_header(key), allow_redirects=False,
                ) as resp:
                    location = resp.headers.get("Location", "")
                    if resp.status in (301, 302, 303, 307, 308) and location:
                        url = urljoin(url, location)
                        if not _on_gemini(url):
                            return VideoResult(url=url, mime="video/mp4")
                        continue
                    if resp.status != 200:
                        raise VideoGenError(
                            _download_failed(resp.status, await resp.text(), key=key)
                        )
                    mime = resp.headers.get("Content-Type", "").split(";")[0].strip()
                    return VideoResult(
                        local_path=await _streamed_to_file(resp),
                        mime=mime if mime.startswith("video/") else "video/mp4",
                    )
    except VideoGenError:
        raise
    except Exception as e:
        raise VideoGenError(_unanswered_message(e, what="Veo video download", key=key)) from e
    raise VideoGenError(
        f"Gemini redirected the Veo video download more than {_MAX_REDIRECTS} times, so the "
        "video could not be saved. Try again in a moment."
    )


async def _streamed_to_file(resp: Any) -> str:
    """The body of ``resp`` written to a new file, whose path is returned. More than
    ``_VIDEO_MAX_BYTES`` is refused, and a partial file is never left behind."""
    fd, path = tempfile.mkstemp(prefix="gemini-video-", suffix=".mp4")
    size = 0
    try:
        with os.fdopen(fd, "wb") as f:
            async for chunk in resp.content.iter_chunked(65536):
                size += len(chunk)
                if size > _VIDEO_MAX_BYTES:
                    raise VideoGenError(
                        f"The video Veo made is larger than {_VIDEO_MAX_BYTES // (1024 * 1024)} "
                        "MB, so PersonalClaw did not save it. Try again, or choose another model "
                        "in Settings → Models."
                    )
                f.write(chunk)
    except BaseException:
        os.unlink(path)
        raise
    return path


def _download_failed(status: int, text: str, *, key: str) -> str:
    """What a failed fetch of a finished video means. A 404 here is the file, not the model."""
    if status == 404:
        return sentence_with_detail(
            "Gemini no longer has the video Veo made (HTTP 404), so it could not be saved. Try "
            "again.",
            _scrubbed(_error_detail(text), key),
        )
    return _status_message(status, text, what="Veo video download", key=key)


# ── TTS Provider ─────────────────────────────────────────────────────────────


class GeminiTTSProvider(TtsProvider):
    """Text-to-speech via the Gemini native generateContent API.

    Uses ``responseModalities: ["AUDIO"]`` with prebuilt voice config.
    Returns raw L16 audio (24 kHz, mono) decoded from base64 inline data.

    A call is checked in the order that says why it cannot go: the model it names first, then
    the key it would be sent with. A call that names no model is refused whatever the key, so
    reporting a missing key first sent the user to fix the one thing that was not the reason.
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

    async def is_available(self) -> bool:
        return bool(self._key())

    async def can_synthesize(self, voice: str = "") -> bool:
        """Whether a call naming *voice* (the bound model) would be sent now: it names a model,
        and there is a key to send it with."""
        return bool(str(voice or "").strip()) and bool(self._key())

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

        # ``voice`` carries the bound TTS model id, which may arrive as either a
        # bare id or the fully-qualified ``models/…`` name (split_ref keeps the
        # prefix). Strip it so the URL isn't doubled (``models/models/…`` → 404).
        # Like chat, a call that names none is refused; this used to speak with
        # gemini-3.1-flash-tts-preview in its place.
        try:
            model = require_model(voice).removeprefix("models/")
        except ProviderResolutionError as exc:
            logger.warning("GeminiTTS refused: %s", exc)
            return None

        key = self._key()
        if not key:
            logger.warning("GeminiTTS: no API key available.")
            return None
        # The key rides the header Gemini documents, never the URL: a URL is what an HTTP error,
        # a proxy's log and a server's access log each record.
        url = f"{_NATIVE_BASE}models/{model}:generateContent"

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
                    url,
                    headers={**_key_header(key), "Content-Type": "application/json"},
                    json=body,
                    allow_redirects=False,
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


def _scrubbed(words: str, key: str) -> str:
    """``words`` without the API key. No URL these calls ask for carries it, but an answer can
    still quote a request back (a proxy's error page can echo its headers), so the words a
    sentence relays are cleared of it anyway."""
    return words.replace(key, "[REDACTED: credential]") if key else words


def _status_message(status: int, text: str, *, what: str, key: str) -> str:
    """What a Gemini media call's HTTP failure means and what to do, then Gemini's own words.

    Keyed on the status class. A 400 has two causes the status cannot tell apart: Gemini answers
    an API key it does not recognize with 400, the same as a request it cannot accept, so that
    sentence names both.
    """
    if status == 400:
        sentence = (
            f"Gemini refused the {what} request as invalid (HTTP 400). It answers an API key it "
            f"does not recognize this way too, so check {_KEY_SETTING}; if the key is right, "
            "choose another model in Settings → Models or change what you asked for."
        )
    elif status in (401, 403):
        sentence = (
            f"Gemini refused the API key for the {what} request (HTTP {status}). Check "
            f"{_KEY_SETTING}, and that the key's Google Cloud project may use this model."
        )
    elif status == 404:
        sentence = (
            f"Gemini has no such model for the {what} request (HTTP 404). Choose another model "
            "in Settings → Models."
        )
    elif status == 429:
        sentence = (
            f"Gemini's quota or rate limit stopped the {what} request (HTTP 429). Wait a minute "
            "and try again, or check the quota of the key's Google Cloud project."
        )
    elif 500 <= status < 600:
        sentence = (
            f"Gemini failed on its side ({what} request, HTTP {status}). Try again in a few "
            "minutes."
        )
    else:
        sentence = (
            f"Gemini's {what} request failed (HTTP {status}). Try again; if it fails the same "
            "way, choose another model in Settings → Models."
        )
    return sentence_with_detail(sentence, _scrubbed(_error_detail(text), key))


def _unanswered_message(error: Exception, *, what: str, key: str) -> str:
    """The sentence for a Gemini media call that got no usable answer: timed out, unable to
    connect, answered with something that is not JSON, or failed some other way."""
    if isinstance(error, asyncio.TimeoutError):
        sentence = f"The {what} request to Gemini timed out. Try again in a moment."
    elif isinstance(error, (json.JSONDecodeError, UnicodeDecodeError, _NotAnObject)):
        sentence = (
            f"Gemini's answer to the {what} request could not be read. Try again in a moment."
        )
    elif isinstance(error, OSError):
        sentence = (
            f"The connection to Gemini failed during the {what} request. Check this computer's "
            "internet connection, then try again."
        )
    else:
        sentence = f"The {what} request to Gemini failed unexpectedly. Try again in a moment."
    return sentence_with_detail(sentence, _scrubbed(str(error), key))


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
