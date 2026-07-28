"""OpenRouter model provider (standalone app).

ONE key, ONE config entry, hundreds of models across every upstream vendor.

Provides:
  - **Chat / code-tools / streaming / vision** via the OpenAI-compatible
    ``/chat/completions`` endpoint (``register_branded_app``)
  - **Embedding** via the same OpenAI-compatible endpoint (``/embeddings``)
  - **Image generation + editing** via ``POST /images`` (base64 response)
  - **Video generation** via async ``POST /videos`` → poll ``GET /videos/{id}``
    → download ``GET /videos/{id}/content``

Base URL: ``https://openrouter.ai/api/v1``. Auth: ``Authorization: Bearer <key>``.

ALL models are DYNAMICALLY DISCOVERED — nothing is hardcoded. The three discovery
routes are deliberately distinct because OpenRouter's default model list is
TEXT-ONLY: a bare ``GET /models`` returns 367 text/chat models and silently omits
every image, video, and embedding model. So:
  - chat + embedding  → ``GET /models?output_modalities=text,embeddings`` (397)
  - image generation  → ``GET /images/models`` (38, with per-model parameter caps)
  - video generation  → ``GET /videos/models`` (17, with explicit capability arrays)

Bring your own API key (config ``api_key`` or the ``OPENROUTER_API_KEY`` env var).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
import re
import tempfile
import time
from typing import Any

from personalclaw.sdk.image import (
    ImageGenError,
    ImageGenModel,
    ImageGenProvider,
    ImageResult,
)
from personalclaw.sdk.model import (
    BrandedProviderSpec,
    Capability,
    ConnectionResult,
    ModelCatalog,
    ModelInfo,
    get_default_registry,
    register_branded_app,
)
from personalclaw.sdk.net import CONNECTOR, EgressBlocked, egress_policy_for, fetch
from personalclaw.sdk.video import (
    VideoGenError,
    VideoGenModel,
    VideoGenProvider,
    VideoResult,
)

logger = logging.getLogger(__name__)

_BASE = "https://openrouter.ai/api/v1"
_API_KEY_ENV = "OPENROUTER_API_KEY"

# Discovery caches expire on the same 5-minute clock google-models uses: the
# catalogs move on release cadence, not per-request, and a Settings page hit
# would otherwise re-fetch three model lists.
_DISCOVERY_TTL_S = 300.0

_IMAGE_TIMEOUT_S = 120.0

# 600s, not the 300s our sibling apps historically used: OpenRouter verifiably
# serves 20s clips (``openai/sora-2-pro``: supported_durations [4,8,12,16,20]) and
# 15s at 4K (``bytedance/seedance-2.0``), where a 300s ceiling times out a
# generation the upstream provider goes on to finish — reporting failure for work
# that succeeded, and was billed.
_VIDEO_TIMEOUT_S = 600.0
_VIDEO_POLL_INTERVAL_S = 5.0

# OpenRouter's job lifecycle. The published docs table lists only four states; the
# API emits six. Enumerated explicitly so a terminal-but-not-successful job raises
# its OWN message instead of being mistaken for "still working" until the timeout.
_VIDEO_PENDING = ("pending", "in_progress")
_VIDEO_DONE = "completed"
_VIDEO_TERMINAL_BAD = ("failed", "cancelled", "expired")

# Attribution header. ``X-Title`` is the legacy alias and is deliberately NOT sent;
# ``HTTP-Referer`` is omitted because a local-first install has no public URL to
# attribute to.
_ATTRIBUTION_HEADER = "X-OpenRouter-Title"
_ATTRIBUTION_VALUE = "PersonalClaw"

# A WxH pixel geometry — the one ``size`` form OpenRouter's ``/images`` accepts.
# Anything else the caller hands us is matched against the model's own enums.
#
# ``size`` is honored even though NO model advertises it (all 38 declare only
# ``resolution``/``aspect_ratio``), and it is the highest-fidelity option: it returns
# those exact pixels, where ``resolution="1K"`` returns the model's own idea of 1K.
# It is also the form core's own image_generate tool schema suggests to the agent
# ("e.g. '1024x1024'"), so it is the common path and is passed through as-is.
# Upstream resolves it to a resolution TIER and rejects a tier the model doesn't
# list; that tier is NOT a simple function of the dimensions (measured live,
# 1024x1024 ⇒ 1K but 1408x768 ⇒ 2K, so it is neither max-edge nor a published rule),
# so no mapping is attempted here — an out-of-range pixel size surfaces upstream's
# own actionable message rather than being silently snapped to a size the caller
# did not ask for.
_PIXEL_SIZE_RE = re.compile(r"^\s*(\d{2,5})\s*[xX*]\s*(\d{2,5})\s*$")


# ── Chat provider (branded, OpenAI-compatible) ────────────────────────────────

SPEC = BrandedProviderSpec(
    type="openrouter",
    protocol="openai",
    default_base_url=_BASE,
    api_key_env=_API_KEY_ENV,
    default_model="",  # resolved from live /v1/models discovery at start()
    # openai-wire: leave max_tokens unset (only the anthropic wire requires a value).
    max_tokens=None,
    capabilities=frozenset({
        Capability.CHAT, Capability.CODE_TOOLS, Capability.STREAMING,
        Capability.VISION, Capability.EMBEDDING,
    }),
    # No curated fallback list. With default_model="" this makes BrandedCatalog
    # return [] when discovery fails, so a missing/bad key yields an honestly EMPTY
    # picker rather than fabricated model ids the user cannot actually call.
    fallback_models=(),
    notes="OpenRouter — one key for hundreds of models across providers "
          "(OpenAI-compatible). Bring your own OpenRouter API key.",
)

_factory, _create_chat_provider, _stock_catalog = register_branded_app(SPEC)


class OpenRouterCatalog(ModelCatalog):
    """Chat + embedding discovery over OpenRouter's MODALITY-FILTERED model list.

    Why this app doesn't use the stock ``BrandedCatalog``: that helper calls
    ``GET {base}/models`` with no query, and OpenRouter's default listing is
    TEXT-ONLY. Verified live — the bare route returns 367 models, **zero** of which
    output embeddings, while ``?output_modalities=text,embeddings`` returns 397
    including all 30 embedding models. Since this app declares
    ``Capability.EMBEDDING``, using the unfiltered route would advertise embedding
    support while the embedding picker stayed permanently empty.

    Image and video models are deliberately NOT requested here: those capabilities
    resolve through their own registries (the scanners below), fed by
    ``/images/models`` and ``/videos/models``, which carry the per-model parameter
    caps this list does not.

    Capability tagging comes from the response's own ``architecture`` block rather
    than core's id-substring heuristic. Measured on the live list, the heuristic
    misses 105 of the 184 image-input models (``qwen/qwen3.7-flash``,
    ``x-ai/grok-4.5``, … carry no "vision"/"vl" marker) and mis-tags the 9
    chat-with-image-output models as ``image_gen``, which would drop them out of the
    chat pool entirely. OpenRouter states modalities explicitly for all 397 entries,
    so the declared data wins.
    """

    # One request serves both capabilities the chat provider advertises.
    _MODALITIES = "text,embeddings"

    def __init__(self, *, endpoint: str = "", api_key: str = "", default_model: str = "") -> None:
        self._endpoint = (endpoint or _BASE).rstrip("/")
        self._api_key = api_key or os.environ.get(_API_KEY_ENV, "")
        self._default_model = default_model

    async def list_models(self) -> list[ModelInfo]:
        """Live models, or [] on any failure.

        Empty is the honest answer for an unreachable endpoint or absent key: with
        no curated fallback, the picker shows nothing rather than ids the user
        cannot call. Never raises — discovery runs on hot Settings GETs.
        """
        url = f"{self._endpoint}/models?output_modalities={self._MODALITIES}"
        headers: dict[str, str] = {_ATTRIBUTION_HEADER: _ATTRIBUTION_VALUE}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            resp = await fetch(url, policy=_json_policy(), method="GET", headers=headers)
            if resp.status != 200:
                return []
            data = json.loads(resp.text)
        except Exception:  # noqa: BLE001 — discovery is fail-soft by contract
            logger.debug("OpenRouter model discovery failed", exc_info=True)
            return []

        out: list[ModelInfo] = []
        for m in data.get("data") or []:
            if not isinstance(m, dict) or not m.get("id"):
                continue
            mid = str(m["id"])
            out.append(ModelInfo(
                id=mid,
                name=mid,
                capabilities=_capabilities_from_architecture(m.get("architecture")),
                description=_truncate(m.get("description", ""), 200),
                extra={"context_length": m["context_length"]} if m.get("context_length") else {},
            ))
        return out

    async def test_connection(self) -> ConnectionResult:
        """Probe the key against an AUTHENTICATED route, then count models.

        ``GET /models`` cannot validate a key: it is a PUBLIC route that returns 200
        with a garbage key and with no key at all (verified live). Testing the key by
        listing models would therefore report "connected" for a typo'd key — the one
        answer the Settings → "Test connection" button exists to prevent. ``GET /key``
        is the authenticated probe (401 ``{"error":{"message":"User not found."}}`` on
        a bad key), and it is free, so the check costs nothing.
        """
        if not self._api_key:
            return ConnectionResult(
                ok=False, detail=f"No API key configured (set it or {_API_KEY_ENV})"
            )
        try:
            resp = await fetch(
                f"{self._endpoint}/key",
                policy=_json_policy(),
                method="GET",
                headers=_headers(self._api_key),
            )
        except Exception as e:  # noqa: BLE001 — unreachable endpoint / blocked egress
            return ConnectionResult(ok=False, detail=f"Could not reach OpenRouter: {e}")
        if resp.status != 200:
            return ConnectionResult(
                ok=False,
                detail=_status_message(resp.status, resp.text, what="key check"),
            )
        # The key is real. A model count is still worth reporting, but discovery
        # failing now means the endpoint/filter is off, not that the key is bad.
        models = await self.list_models()
        if not models:
            return ConnectionResult(ok=False, detail="Key is valid but no models were listed")
        return ConnectionResult(ok=True, model_count=len(models))


def _capabilities_from_architecture(architecture: Any) -> list[str]:
    """Use-case tags derived from OpenRouter's declared input/output modalities.

    Mirrors core's vocabulary (``chat`` / ``embedding`` / ``image_modality`` / …).
    An embeddings-output model is NOT a chat model, so the two are exclusive; the
    understanding tags (``image_modality`` and friends) stack onto chat because a
    chat model that reads images is both.
    """
    arch = architecture if isinstance(architecture, dict) else {}
    outputs = {str(o) for o in (arch.get("output_modalities") or [])}
    inputs = {str(i) for i in (arch.get("input_modalities") or [])}

    if "embeddings" in outputs:
        return ["embedding"]

    caps = ["chat"]
    # An image-OUTPUT chat model (google/gemini-3-pro-image) still converses, so it
    # keeps ``chat``; its generation path is the image_gen adapter, bound separately.
    if "image" in inputs:
        caps.append("image_modality")
    if "audio" in inputs:
        caps.append("audio_modality")
    if "video" in inputs:
        caps.append("video_modality")
    return caps


def create_catalog(options: dict[str, Any] | None = None, *, model: str = "") -> ModelCatalog:
    """Catalog factory (the shape ``registry.build_catalog`` invokes)."""
    opts = options or {}
    return OpenRouterCatalog(
        endpoint=str(opts.get("endpoint") or opts.get("base_url") or ""),
        api_key=str(opts.get("api_key") or ""),
        default_model=str(model or opts.get("default_model") or opts.get("model") or ""),
    )


# register_branded_app already registered its stock catalog under this type;
# re-registering is last-wins by contract (``register_catalog`` is deliberately not
# strict, so a reload re-registers cleanly), which swaps in the filtered one above.
get_default_registry().register_catalog(SPEC.type, create_catalog)


def create_provider(config: dict[str, Any] | None = None):
    """Chat provider factory (multi-instance, OpenAI-compatible endpoint).

    ONE ``openrouter`` config entry serves chat + embedding (through the
    OpenAI-compatible endpoint) AND image_gen / video_gen (through the media
    scanners below). The media adapters are built per config ENTRY by those
    scanners — NOT returned here — so the app surfaces as ONE provider and each
    adapter can be keyed by its entry's name.
    """
    return _create_chat_provider(config or {})


# ── Shared HTTP helpers ───────────────────────────────────────────────────────


def _headers(key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        _ATTRIBUTION_HEADER: _ATTRIBUTION_VALUE,
    }


def _json_policy():
    """Egress policy for a JSON call (discovery / submit / poll).

    CONNECTOR's own caps (20s, 10MB) are right for a JSON body, and
    ``egress_policy_for`` layers the operator's security.egress config on top so a
    self-hoster proxying OpenRouter through an allow-listed host still reaches it.
    """
    return egress_policy_for(CONNECTOR)


def _long_policy(*, timeout_s: float, max_bytes: int):
    """Egress policy for a call that is slow, or returns a large body.

    CONNECTOR alone would break both cases: its 20s timeout is shorter than an
    image generation, and ``_read_capped`` TRUNCATES SILENTLY at max_bytes (10MB) —
    which would quietly corrupt a 4K PNG or any real MP4 rather than failing.
    """
    return _json_policy().with_overrides(timeout_s=timeout_s, max_bytes=max_bytes)


def _error_detail(text: str) -> str:
    """The human-readable half of OpenRouter's ``{error:{code,message}}`` envelope."""
    try:
        return str(json.loads(text).get("error", {}).get("message", ""))[:200]
    except Exception:  # noqa: BLE001 — a proxy may return non-JSON (HTML error page)
        return text[:200]


def _status_message(status: int, text: str, *, what: str) -> str:
    """Map an OpenRouter HTTP status to a message a user can act on.

    Keyed on the STATUS CODE only, never the body. Verified live: an
    unauthenticated ``POST /api/v1/images`` returns the actively misleading
    ``{"error":{"message":"No cookie auth credentials found","code":401}}`` — a
    body-matching implementation would tell the user to check their cookies.
    """
    if status in (401, 403):
        return (
            f"OpenRouter rejected the API key ({what}). Check the key in Settings → "
            f"Providers, or {_API_KEY_ENV}."
        )
    if status == 402:
        return (
            f"OpenRouter reports insufficient credits ({what}) — top up at "
            "openrouter.ai/credits."
        )
    if status == 413:
        return f"The input image is too large for OpenRouter's limit ({what})."
    if status == 429:
        return f"OpenRouter rate-limited this request ({what})."
    if status == 502:
        return (
            f"The upstream provider failed ({what}). Billing is all-or-nothing, so "
            "nothing partial was produced and a 502 is not charged."
        )
    if status in (524, 529):
        return f"OpenRouter timed out or is overloaded ({what}); retry shortly."
    return f"OpenRouter {what} failed (HTTP {status}): {_error_detail(text)}"


def _retry_after_seconds(headers: dict[str, str]) -> float:
    """``Retry-After`` as delta-seconds, clamped to [1, 30].

    Clamped so one honored retry can never outlive the enclosing request timeout —
    an upstream free to name any delay could otherwise stall a turn indefinitely.
    """
    raw = ""
    for k, v in (headers or {}).items():
        if k.lower() == "retry-after":
            raw = str(v)
            break
    try:
        return max(1.0, min(30.0, float(raw.strip())))
    except (TypeError, ValueError):
        return 1.0


async def _request_json(
    url: str,
    *,
    key: str,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    policy=None,
    error_cls: type[Exception] = ImageGenError,
    what: str = "request",
    ok_statuses: tuple[int, ...] = (200,),
    allow_retry: bool = True,
) -> dict[str, Any]:
    """One guarded JSON call, with the status→message mapping + a single 429 retry.

    Retries EXACTLY once on 429, honoring ``Retry-After``. A second 429 raises: an
    unbounded backoff chain inside a chat turn is worse for the user than a clear
    "rate-limited, try again" — they can see it and decide.
    """
    data = json.dumps(body).encode() if body is not None else None
    attempt = 0
    while True:
        try:
            resp = await fetch(
                url,
                policy=policy if policy is not None else _json_policy(),
                method=method,
                headers=_headers(key),
                data=data,
            )
        except EgressBlocked as e:
            raise error_cls(f"OpenRouter {what} was blocked by the egress guard: {e}") from e
        except error_cls:
            raise
        except asyncio.TimeoutError as e:
            raise error_cls(f"OpenRouter {what} timed out.") from e
        except Exception as e:  # noqa: BLE001 — surface any transport failure as typed
            raise error_cls(f"OpenRouter {what} request failed: {e}") from e

        if resp.status in ok_statuses:
            try:
                return json.loads(resp.text) if resp.text else {}
            except (json.JSONDecodeError, ValueError) as e:
                raise error_cls(f"OpenRouter {what} returned an unparseable response.") from e

        if resp.status == 429 and allow_retry and attempt == 0:
            attempt += 1
            await asyncio.sleep(_retry_after_seconds(resp.headers))
            continue

        raise error_cls(_status_message(resp.status, resp.text, what=what))


# ── Dynamic model discovery (TTL-cached, per key) ─────────────────────────────
#
# Separate caches per route: the three endpoints return DIFFERENT descriptor
# grammars, and only the media ones carry the per-model parameter caps the request
# builders need. Keyed by api_key so two accounts with different model access don't
# read each other's list.

_image_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_video_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


async def _discover(
    cache: dict[str, tuple[float, list[dict[str, Any]]]],
    *,
    url: str,
    api_key: str,
    label: str,
) -> list[dict[str, Any]]:
    """Fetch + cache a model list. Degrades to the last good list, then to [].

    A transient 5xx returning the STALE list keeps the picker populated through a
    blip; having never succeeded returns [] so an unconfigured provider shows an
    honestly empty picker instead of invented ids.
    """
    cached = cache.get(api_key)
    now = time.monotonic()
    if cached and (now - cached[0]) < _DISCOVERY_TTL_S:
        return cached[1]
    try:
        resp = await fetch(
            url, policy=_json_policy(), method="GET", headers=_headers(api_key),
        )
        if resp.status != 200:
            logger.debug("OpenRouter %s discovery HTTP %s", label, resp.status)
            return cached[1] if cached else []
        data = json.loads(resp.text)
    except Exception:  # noqa: BLE001 — discovery is fail-soft by contract
        logger.debug("OpenRouter %s discovery failed", label, exc_info=True)
        return cached[1] if cached else []

    models = [m for m in (data.get("data") or []) if isinstance(m, dict) and m.get("id")]
    cache[api_key] = (now, models)
    return models


async def _discover_image_models(api_key: str) -> list[dict[str, Any]]:
    """``GET /images/models`` — the dedicated route.

    NOT ``GET /models``: verified live that the default list is text-only (367
    models, zero with image output), so a bare listing would show an empty image
    picker while 38 image models are in fact available.
    """
    return await _discover(
        _image_cache, url=f"{_BASE}/images/models", api_key=api_key, label="image",
    )


async def _discover_video_models(api_key: str) -> list[dict[str, Any]]:
    """``GET /videos/models`` — the dedicated route (same reason as images)."""
    return await _discover(
        _video_cache, url=f"{_BASE}/videos/models", api_key=api_key, label="video",
    )


def _param(descriptor: dict[str, Any], key: str) -> dict[str, Any] | None:
    """A model's ``supported_parameters[key]`` descriptor, or None.

    An ABSENT key means the parameter is UNSUPPORTED by that model — the request
    builders must omit it rather than send a default. Verified: the union of keys
    across all 38 image models is exactly aspect_ratio/background/input_references/
    n/output_compression/output_format/quality/resolution/seed, and each model
    carries only its own subset.
    """
    sp = descriptor.get("supported_parameters")
    if not isinstance(sp, dict):
        return None
    d = sp.get(key)
    return d if isinstance(d, dict) else None


def _enum_values(descriptor: dict[str, Any], key: str) -> list[str]:
    d = _param(descriptor, key)
    if not d:
        return []
    return [str(v) for v in (d.get("values") or [])]


def _range_max(descriptor: dict[str, Any], key: str) -> int | None:
    d = _param(descriptor, key)
    if not d:
        return None
    try:
        return int(d.get("max"))
    except (TypeError, ValueError):
        return None


def _truncate(text: str, limit: int = 300) -> str:
    t = str(text or "").strip()
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


# ── Image Provider ────────────────────────────────────────────────────────────


class OpenRouterImageProvider(ImageGenProvider):
    """Image generation + editing via OpenRouter's ``POST /images``.

    The endpoint is SYNCHRONOUS (no queue, no polling) but bespoke: its body takes
    ``resolution``/``aspect_ratio``/``input_references`` rather than OpenAI-Images'
    ``size``+``image[]``, and the response is base64-only. That shape difference is
    exactly why ``openrouter`` must NOT join core's ``OPENAI_FAMILY_TYPES`` (which
    would auto-wire an OpenAI-Images adapter under our own config name) — see the
    scanner block at the bottom of this module.
    """

    def __init__(
        self, *, api_key: str = "", endpoint: str = "", name: str = "openrouter",
    ) -> None:
        self._api_key = api_key
        self._endpoint = endpoint
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @name.setter
    def name(self, value: str) -> None:
        # Settable, not a bare read-only property: the adapter is keyed by config
        # entry name in the image_gen registry, and ModelTypeHandler.create may
        # rename an instance. A read-only property makes that assignment raise.
        self._name = value

    @property
    def display_name(self) -> str:
        return "OpenRouter (image)"

    def _key(self) -> str:
        return self._api_key or os.environ.get(_API_KEY_ENV, "")

    def _base(self) -> str:
        return (self._endpoint or _BASE).rstrip("/")

    async def is_available(self) -> bool:
        return bool(self._key())

    async def list_models(self) -> list[ImageGenModel]:
        from personalclaw.sdk.image import active_image_gen

        resolved = active_image_gen()
        # Compare against OUR name (the config entry), not a literal "openrouter":
        # two instances share this class, and only the bound one marks a model active.
        active_model = resolved[1] if resolved and resolved[0].name == self._name else ""

        key = self._key()
        if not key:
            return []  # honest empty picker; no speculative unauthenticated call

        out: list[ImageGenModel] = []
        for m in await _discover_image_models(key):
            mid = str(m["id"])
            # OpenRouter expresses geometry as resolution tokens ("1K"/"2K"/"4K")
            # AND aspect-ratio tokens ("16:9"), never as pixel dimensions here. The
            # picker must offer what the API actually accepts, so both families are
            # surfaced — a ratio-only choice is a legitimate request.
            sizes = _enum_values(m, "resolution") + _enum_values(m, "aspect_ratio")
            refs_max = _range_max(m, "input_references")
            out.append(ImageGenModel(
                name=mid,
                description=_truncate(m.get("description", "")) or mid,
                sizes=sizes,
                supports_edit=bool(refs_max and refs_max >= 1),
                downloaded=True,  # hosted — nothing to download
                active=mid == active_model,
            ))
        return out

    async def _resolve_model(self, key: str, model: str) -> tuple[str, dict[str, Any]]:
        """Resolve the model id + its live descriptor, defaulting to the first listed."""
        discovered = await _discover_image_models(key)
        by_id = {str(m["id"]): m for m in discovered}
        model_id = str(model or "")
        if not model_id:
            if not discovered:
                raise ImageGenError(
                    "No OpenRouter image-generation model is available (discovery "
                    "returned nothing — check the API key and connectivity)."
                )
            model_id = str(discovered[0]["id"])
        return model_id, by_id.get(model_id, {})

    def _geometry(self, size: str, descriptor: dict[str, Any]) -> dict[str, Any]:
        """Map the ABC's single opaque ``size`` onto ONE of OpenRouter's three keys.

        Exactly one key is sent. Measured live, ``size`` + ``aspect_ratio`` together
        is a hard 400 (``size "1024x1024" conflicts with aspect_ratio "16:9"``);
        ``size`` + ``resolution`` happens to be accepted today with ``size`` winning,
        but sending a redundant pair means relying on which one upstream prefers, so
        the caller's single value maps to a single key either way.

        An unrecognized value yields {} and lets the model use its own default rather
        than guessing a token the model may not accept.
        """
        s = (size or "").strip()
        if not s:
            return {}
        if _PIXEL_SIZE_RE.match(s):
            return {"size": s}
        if s in _enum_values(descriptor, "resolution"):
            return {"resolution": s}
        if s in _enum_values(descriptor, "aspect_ratio"):
            return {"aspect_ratio": s}
        logger.debug("OpenRouter: dropping unrecognized image size %r", s)
        return {}

    @staticmethod
    def _size_hint(body: dict[str, Any], descriptor: dict[str, Any]) -> str:
        """" … Accepted: …" when the request carried a pixel ``size``, else ""."""
        if not body.get("size"):
            return ""
        accepted = (_enum_values(descriptor, "resolution")
                    + _enum_values(descriptor, "aspect_ratio"))
        if not accepted:
            return ""
        return (f" This model accepts these sizes: {', '.join(accepted)} "
                f"(or a smaller pixel size).")

    def _build_body(
        self, model_id: str, prompt: str, *, size: str, n: int, descriptor: dict[str, Any],
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"model": model_id, "prompt": prompt}
        body.update(self._geometry(size, descriptor))
        # Clamp to the model's LIVE cap, not the schema's. Verified the schema
        # maximum is 10 while per-model caps run 1/6/10 — google/gemini-3-pro-image
        # reports max 1. Absent key ⇒ the model doesn't take ``n`` at all ⇒ omit.
        n_max = _range_max(descriptor, "n")
        if n_max is not None:
            body["n"] = max(1, min(int(n or 1), n_max))
        # Only sent when advertised. PNG is the safe default and matches
        # ImageResult.mime's own default.
        if "png" in _enum_values(descriptor, "output_format"):
            body["output_format"] = "png"
        return body

    async def _post_images(
        self, body: dict[str, Any], key: str, *, descriptor: dict[str, Any] | None = None,
    ) -> list[ImageResult]:
        """POST the assembled body and decode the base64-only response.

        The route is ``/images`` — never the undocumented ``/images/generations``
        alias (which exists but carries no stability contract).

        A rejected pixel ``size`` is re-raised with the sizes this model DOES accept
        appended. Upstream's own text names only the tier it computed ("Image size 2K
        is not supported for this model"), which the caller cannot act on: it never
        asked for "2K", it asked for a WxH, and the tier mapping is not a published
        rule. Listing the model's real enum turns a dead end into a next step.
        """
        try:
            data = await _request_json(
                f"{self._base()}/images",
                key=key,
                method="POST",
                body=body,
                # A long call returning a large base64 body: CONNECTOR's 20s/10MB would
                # both time out and silently truncate.
                policy=_long_policy(timeout_s=_IMAGE_TIMEOUT_S, max_bytes=64_000_000),
                error_cls=ImageGenError,
                what="image generation",
            )
        except ImageGenError as e:
            hint = self._size_hint(body, descriptor or {})
            if not hint:
                raise
            raise ImageGenError(f"{e}{hint}") from e
        results: list[ImageResult] = []
        for item in data.get("data", []) or []:
            if not isinstance(item, dict):
                continue
            b64 = str(item.get("b64_json", "") or "")
            if not b64:
                continue
            # Return the bytes inline and write NOTHING to disk: core's
            # _materialize_image decodes b64 directly (no second egress hop, no
            # expiring URL) and persists through the native artifact store, which
            # owns naming and versioning.
            results.append(ImageResult(
                b64=b64,
                mime=str(item.get("media_type", "") or "image/png"),
                revised_prompt=str(item.get("revised_prompt", "") or ""),
            ))
        if not results:
            raise ImageGenError("OpenRouter returned no image data.")
        return results

    async def generate(
        self, prompt: str, *, model: str = "", size: str = "", n: int = 1, **opts: Any,
    ) -> list[ImageResult]:
        key = self._key()
        if not key:
            raise ImageGenError(f"No OpenRouter API key configured (set {_API_KEY_ENV}).")
        model_id, descriptor = await self._resolve_model(key, model)
        body = self._build_body(model_id, prompt, size=size, n=n, descriptor=descriptor)
        return await self._post_images(body, key, descriptor=descriptor)

    async def edit(
        self, prompt: str, *, source_image: str, mask: str = "", model: str = "",
        size: str = "", n: int = 1, **opts: Any,
    ) -> list[ImageResult]:
        key = self._key()
        if not key:
            raise ImageGenError(f"No OpenRouter API key configured (set {_API_KEY_ENV}).")
        if mask:
            # A typed, honest refusal rather than a silent drop: OpenRouter's
            # /images has no mask/inpainting parameter at all, so quietly ignoring
            # the mask would return a whole-image edit the user didn't ask for.
            raise ImageGenError(
                "OpenRouter's image API has no mask/inpainting parameter; omit the "
                "mask or use a mask-capable provider."
            )

        model_id, descriptor = await self._resolve_model(key, model)
        refs_max = _range_max(descriptor, "input_references")
        if not refs_max:
            # Checked BEFORE the request so an impossible edit costs nothing.
            raise ImageGenError(
                f"Model {model_id!r} does not accept input images "
                "(no input_references support)."
            )

        try:
            with open(source_image, "rb") as fh:
                raw = fh.read()
        except OSError as e:
            raise ImageGenError(f"Could not read source image: {e}") from e
        mime = mimetypes.guess_type(source_image)[0] or "image/png"
        data_uri = f"data:{mime};base64,{base64.b64encode(raw).decode()}"

        body = self._build_body(model_id, prompt, size=size, n=n, descriptor=descriptor)
        # Image-to-image is the TOP-LEVEL input_references array (not a nested
        # `image` field), each entry an OpenAI-style image_url content part.
        body["input_references"] = [
            {"type": "image_url", "image_url": {"url": data_uri}},
        ]
        return await self._post_images(body, key, descriptor=descriptor)


# ── Video Provider ────────────────────────────────────────────────────────────


class OpenRouterVideoProvider(VideoGenProvider):
    """Video generation via OpenRouter's async job API.

    ``generate()`` owns the whole cycle the ABC requires — submit → poll →
    download — so the caller never sees the async/sync difference. The bytes are
    downloaded HERE and returned as ``local_path``: core's ``_materialize_video``
    fetches a returned ``url`` with the bare CONNECTOR policy (10MB cap, silently
    truncating), which would corrupt any realistic MP4. A ``local_path`` takes
    core's uncapped read path instead.
    """

    def __init__(
        self, *, api_key: str = "", endpoint: str = "", name: str = "openrouter",
    ) -> None:
        self._api_key = api_key
        self._endpoint = endpoint
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @name.setter
    def name(self, value: str) -> None:
        self._name = value  # settable for the same reason as the image adapter

    @property
    def display_name(self) -> str:
        return "OpenRouter (video)"

    def _key(self) -> str:
        return self._api_key or os.environ.get(_API_KEY_ENV, "")

    def _base(self) -> str:
        return (self._endpoint or _BASE).rstrip("/")

    async def is_available(self) -> bool:
        return bool(self._key())

    async def list_models(self) -> list[VideoGenModel]:
        from personalclaw.sdk.video import active_video_gen

        resolved = active_video_gen()
        active_model = resolved[1] if resolved and resolved[0].name == self._name else ""

        key = self._key()
        if not key:
            return []

        out: list[VideoGenModel] = []
        for m in await _discover_video_models(key):
            mid = str(m["id"])
            # Every array can be null, and null means "the model doesn't express
            # this" — distinct from an empty list. Ratios come from the descriptor
            # rather than a hardcoded ["16:9","9:16"]: seedance-2.0 supports 7,
            # hailuo-2.3 supports exactly one, and grok-imagine-video-1.5 reports
            # null. A baked pair would offer ratios that 400 upstream.
            durations = _int_list(m.get("supported_durations"))
            out.append(VideoGenModel(
                name=mid,
                description=_truncate(m.get("description", "")) or mid,
                aspect_ratios=[str(r) for r in (m.get("supported_aspect_ratios") or [])],
                max_duration_s=max(durations) if durations else 10,
                downloaded=True,
                active=mid == active_model,
            ))
        return out

    async def _resolve_model(self, key: str, model: str) -> tuple[str, dict[str, Any]]:
        discovered = await _discover_video_models(key)
        by_id = {str(m["id"]): m for m in discovered}
        model_id = str(model or "")
        if not model_id:
            if not discovered:
                raise VideoGenError(
                    "No OpenRouter video-generation model is available (discovery "
                    "returned nothing — check the API key and connectivity)."
                )
            model_id = str(discovered[0]["id"])
        return model_id, by_id.get(model_id, {})

    def _build_body(
        self,
        model_id: str,
        prompt: str,
        *,
        duration_seconds: float,
        aspect_ratio: str,
        descriptor: dict[str, Any],
        opts: dict[str, Any],
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"model": model_id}
        if prompt:
            body["prompt"] = prompt

        # Duration is SNAPPED to a value the model actually offers, never sent raw:
        # these are explicit int arrays per model (veo-3.1-fast [4,6,8],
        # kling-video-o1 [5,10]), so an unlisted duration is a 400.
        durations = _int_list(descriptor.get("supported_durations"))
        if durations:
            want = round(float(duration_seconds or 0) or 0)
            body["duration"] = min(durations, key=lambda d: (abs(d - want), d))

        ratios = [str(r) for r in (descriptor.get("supported_aspect_ratios") or [])]
        if aspect_ratio and aspect_ratio in ratios:
            body["aspect_ratio"] = aspect_ratio

        # ALWAYS explicit when the model supports audio: the docs and the OpenAPI
        # schema disagree on the default, and a silently-muted clip is the worse
        # surprise. Omitted entirely when the descriptor reports null (unsupported).
        if descriptor.get("generate_audio") is not None:
            body["generate_audio"] = bool(opts.get("generate_audio", True))

        if descriptor.get("seed") is True and opts.get("seed") is not None:
            body["seed"] = int(opts["seed"])

        frames = self._frame_images(descriptor, opts)
        if frames:
            # frame_images WINS over input_references per the documented
            # precedence, so exactly one of the two is ever sent.
            body["frame_images"] = frames
        elif opts.get("input_references"):
            body["input_references"] = list(opts["input_references"])

        if not body.get("prompt") and not frames and not body.get("input_references"):
            raise VideoGenError(
                "OpenRouter video generation needs a prompt or a reference/frame image."
            )
        return body

    @staticmethod
    def _frame_images(
        descriptor: dict[str, Any], opts: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Validate caller-supplied first/last frames against the model's support.

        ``supported_frame_images`` is null for openai/sora-2-pro, ["first_frame"]
        for several, and both for most — so a last_frame request is validated
        BEFORE spending a submit rather than 400-ing upstream.
        """
        raw = opts.get("frame_images")
        if not raw:
            return []
        supported = [str(f) for f in (descriptor.get("supported_frame_images") or [])]
        if not supported:
            raise VideoGenError(
                f"Model {descriptor.get('id', '')!r} does not accept frame images."
            )
        out: list[dict[str, Any]] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            ftype = str(entry.get("frame_type", "") or "first_frame")
            if ftype not in supported:
                raise VideoGenError(
                    f"Model {descriptor.get('id', '')!r} does not support "
                    f"frame_type {ftype!r} (supports {supported})."
                )
            out.append({"frame_type": ftype, "image_url": entry.get("image_url", {})})
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
            raise VideoGenError(f"No OpenRouter API key configured (set {_API_KEY_ENV}).")

        model_id, descriptor = await self._resolve_model(key, model)
        body = self._build_body(
            model_id, prompt, duration_seconds=duration_seconds,
            aspect_ratio=aspect_ratio, descriptor=descriptor, opts=opts,
        )
        job_id = await self._submit(body, key)
        await self._poll(job_id, key)
        return await self._download(job_id, key, duration_s=float(body.get("duration", 0) or 0))

    async def _submit(self, body: dict[str, Any], key: str) -> str:
        """Submit the job; returns its id. 202 is the documented success status."""
        data = await _request_json(
            f"{self._base()}/videos",
            key=key,
            method="POST",
            body=body,
            error_cls=VideoGenError,
            what="video submit",
            ok_statuses=(200, 201, 202),
        )
        job_id = str(data.get("id", "") or "")
        if not job_id:
            raise VideoGenError("OpenRouter video submit returned no job id.")
        # ``polling_url`` is present in the response but deliberately NOT used: we
        # construct the canonical GET /videos/{id} ourselves. A vendor-returned
        # status URL that omits part of the path is a real bug class this repo has
        # already been bitten by (see fal-image's queue-URL note).
        return job_id

    async def _poll(self, job_id: str, key: str) -> dict[str, Any]:
        """Poll to a terminal state, bounded by ``_VIDEO_TIMEOUT_S``.

        A transient non-200 on a poll is swallowed and retried — one 5xx must not
        abandon (and waste) a job the user is already paying for. Every terminal
        state raises its own message so "cancelled" never reads as "timed out".
        """
        url = f"{self._base()}/videos/{job_id}"
        elapsed = 0.0
        while elapsed < _VIDEO_TIMEOUT_S:
            doc: dict[str, Any] = {}
            try:
                resp = await fetch(
                    url, policy=_json_policy(), method="GET", headers=_headers(key),
                )
                if resp.status == 200:
                    doc = json.loads(resp.text)
                elif resp.status in (401, 403, 402):
                    # An auth/credit failure is not transient — retrying until the
                    # timeout would just hide it behind a misleading message.
                    raise VideoGenError(
                        _status_message(resp.status, resp.text, what="video polling")
                    )
            except VideoGenError:
                raise
            except Exception:  # noqa: BLE001 — a transient poll error is retried
                logger.debug("OpenRouter video poll error", exc_info=True)

            status = str(doc.get("status", "") or "").lower()
            if status == _VIDEO_DONE:
                return doc
            if status == "failed":
                detail = _truncate(doc.get("error") or doc.get("failure_reason") or "", 200)
                raise VideoGenError(
                    f"OpenRouter video job failed: {detail}" if detail
                    else "OpenRouter video job failed."
                )
            if status == "cancelled":
                raise VideoGenError("OpenRouter video job was cancelled.")
            if status == "expired":
                raise VideoGenError(
                    "OpenRouter video job expired before its output could be downloaded."
                )
            if status and status not in _VIDEO_PENDING:
                # Forward-compat: an unrecognized status must not crash the turn.
                # Treated as pending and governed by the outer timeout.
                logger.debug("OpenRouter video job %s: unknown status %r", job_id, status)

            await asyncio.sleep(_VIDEO_POLL_INTERVAL_S)
            elapsed += _VIDEO_POLL_INTERVAL_S
        raise VideoGenError(
            f"OpenRouter video generation timed out after {_VIDEO_TIMEOUT_S:.0f}s."
        )

    async def _download(
        self, job_id: str, key: str, *, duration_s: float = 0.0,
    ) -> list[VideoResult]:
        """Download the rendered MP4 and hand core a local path."""
        url = f"{self._base()}/videos/{job_id}/content?index=0"
        try:
            resp = await fetch(
                url,
                # 256MB: a 4K/15s clip is far past CONNECTOR's 10MB cap.
                policy=_long_policy(timeout_s=_IMAGE_TIMEOUT_S, max_bytes=256_000_000),
                method="GET",
                headers=_headers(key),
            )
        except EgressBlocked as e:
            raise VideoGenError(f"OpenRouter video download was blocked: {e}") from e
        except Exception as e:  # noqa: BLE001
            raise VideoGenError(f"OpenRouter video download failed: {e}") from e

        if resp.status != 200:
            raise VideoGenError(
                _status_message(resp.status, resp.text, what="video download")
            )
        if resp.truncated:
            # ``_read_capped`` truncates SILENTLY, so without this check a clip over
            # the cap would be saved as a corrupt file that plays back broken. Fail
            # loudly instead.
            raise VideoGenError(
                "The generated video was truncated at the egress byte cap; it was "
                "not saved. Generate a shorter or lower-resolution clip."
            )
        if not resp.body:
            raise VideoGenError("OpenRouter returned an empty video body.")

        mime = (resp.headers.get("Content-Type", "") or "").split(";")[0].strip()
        # delete=False, and deliberately NOT unlinked here: core reads the file
        # AFTER generate() returns, then persists the bytes into the artifact store.
        # OS temp cleanup reclaims it (the contract bedrock-models already relies on).
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as fh:
            fh.write(resp.body)
            path = fh.name
        return [VideoResult(
            local_path=path, mime=mime or "video/mp4", duration_s=duration_s,
        )]


def _int_list(raw: Any) -> list[int]:
    """Coerce a possibly-null descriptor array to a list of ints."""
    out: list[int] = []
    for v in raw or []:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out


# ── Media-capability config scanners ─────────────────────────────────────────
# The image/video capabilities resolve through their own registries, which build a
# per-config adapter. Core knows the OpenAI-family built-in; OpenRouter contributes
# its adapters through the app-owned ``media_scanners`` extension point.
#
# ``openrouter`` is deliberately NOT added to core's OPENAI_FAMILY_TYPES. That
# membership would register a core OpenAIImageProvider under OUR config name — the
# same dict key our scanner owns — and the core adapter speaks OpenAI-Images
# (``/images/generations`` with ``size`` + ``image[]``), which OpenRouter's
# ``/images`` verifiably is not. Any window where the core adapter won would 400.
# The cost is that this app must contribute its own adapter, which it wants to
# anyway: only the app knows the ``/images`` body shape.


def _openrouter_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The config.json provider entries this app owns (type ``openrouter``)."""
    out = []
    for e in entries:
        ptype = str(e.get("type", ""))
        # ``_original_type`` tolerance: canonical_provider_type is an identity map
        # today, so nothing stamps it for us — but it is documented as the single
        # hook a future alias would use, and an alias must not orphan our adapters.
        original = str((e.get("options") or {}).get("_original_type", ""))
        if ptype == "openrouter" or original == "openrouter":
            out.append(e)
    return out


def _entry_key(e: dict[str, Any]) -> str:
    return str((e.get("options") or {}).get("api_key", "") or "")


def _entry_endpoint(e: dict[str, Any]) -> str:
    return str((e.get("options") or {}).get("endpoint", "") or "")


def _scan_image(entries: list[dict[str, Any]]) -> list:
    # Keyed by the CONFIG ENTRY's name, not the literal "openrouter": that is what
    # makes an ``<instance>:<model>`` binding resolve to the same account that backs
    # that instance's chat, and what lets two accounts coexist.
    return [
        OpenRouterImageProvider(
            api_key=_entry_key(e), endpoint=_entry_endpoint(e), name=str(e["name"]),
        )
        for e in _openrouter_entries(entries)
    ]


def _scan_video(entries: list[dict[str, Any]]) -> list:
    return [
        OpenRouterVideoProvider(
            api_key=_entry_key(e), endpoint=_entry_endpoint(e), name=str(e["name"]),
        )
        for e in _openrouter_entries(entries)
    ]


try:
    from personalclaw.sdk.model import register_scanner as _reg_scanner

    _reg_scanner("image_gen", _scan_image)
    _reg_scanner("video_gen", _scan_video)
except Exception:  # noqa: BLE001 — older core without the extension point
    logger.debug("media_scanners extension point unavailable", exc_info=True)
