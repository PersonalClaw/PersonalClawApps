"""Unit tests for the OpenRouter model provider app.

The OpenAI SDK is stubbed (constructing the chat provider triggers its lazy
import), and every HTTP call is served by a queued fake ``fetch`` so the whole
suite runs offline with no vendor SDK and no API key.

The assertions are the app's real contracts: the registered TYPE + catalog, the
manifest↔code parity that keeps "Add instance" buildable, the config-entry keying
that lets two accounts coexist, and — for the media adapters — the exact request
shape (which upstream rejects with a 400 when it's wrong) plus every terminal job
state.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _stub_openai(monkeypatch):
    fake = types.ModuleType("openai")

    class _AsyncOpenAI:
        def __init__(self, **kw):
            self.kw = kw

    fake.AsyncOpenAI = _AsyncOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake)
    yield


import provider as prov  # app-local; registers type + catalog + scanners on import

from personalclaw.llm.capabilities import Capability
from personalclaw.llm.registry import ProviderEntry, get_default_registry
from personalclaw.providers import media_scanners
from personalclaw.providers.use_cases import CAPABILITIES, CHAT_SUBCATEGORIES, OPENAI_FAMILY_TYPES
from personalclaw.sdk.image import ImageGenError, ImageGenProvider
from personalclaw.sdk.video import VideoGenError, VideoGenProvider

_MANIFEST = json.loads((Path(__file__).parent / "app.json").read_text(encoding="utf-8"))


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    """No ambient key: the key-absent assertions must not read the dev machine's."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    yield


@pytest.fixture(autouse=True)
def _clear_discovery_caches():
    """The TTL discovery caches are module-level, so they leak across tests."""
    prov._image_cache.clear()
    prov._video_cache.clear()
    yield
    prov._image_cache.clear()
    prov._video_cache.clear()


class _FakeResponse:
    def __init__(self, status, payload=None, *, headers=None, body=None, truncated=False):
        self.status = status
        self.headers = headers or {}
        self.truncated = truncated
        if body is not None:
            self.body = body
            self.text = ""
        else:
            self.body = b""
            self.text = "" if payload is None else json.dumps(payload)


def _fake_fetch(monkeypatch, responses):
    """Serve a queued list of responses; record every (method, url, body, headers).

    The last queued response repeats, so a poll loop can be driven to a terminal
    state without enumerating each identical hop.
    """
    calls: list[dict] = []
    queue = list(responses)

    async def _fetch(url, *, policy=None, method="GET", headers=None, data=None, **kw):
        calls.append({
            "url": url,
            "method": method,
            "headers": dict(headers or {}),
            "body": json.loads(data.decode()) if data else None,
            "policy": policy,
        })
        return queue.pop(0) if len(queue) > 1 else queue[0]

    for target in ("personalclaw.net.client.fetch", "personalclaw.sdk.net.fetch",
                   "personalclaw.net.fetch", "provider.fetch"):
        monkeypatch.setattr(target, _fetch, raising=False)
    return calls


@pytest.fixture
def _no_sleep(monkeypatch):
    """Patch asyncio.sleep so poll loops and Retry-After don't spend wall time."""
    slept: list[float] = []

    async def _sleep(seconds, *a, **kw):
        slept.append(seconds)

    monkeypatch.setattr(prov.asyncio, "sleep", _sleep)
    return slept


# ── Live-shaped fixtures (taken verbatim from the real endpoints) ─────────────

_IMAGE_MODEL = {
    "id": "google/gemini-3-pro-image",
    "name": "Google: Gemini 3 Pro Image",
    "description": "Gemini 3 Pro Image generation.",
    "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["image"]},
    "supported_parameters": {
        "resolution": {"type": "enum", "values": ["1K", "2K", "4K"]},
        "aspect_ratio": {"type": "enum", "values": ["1:1", "16:9", "9:16"]},
        "n": {"type": "range", "min": 1, "max": 1},
        "input_references": {"type": "range", "min": 0, "max": 14},
        "output_format": {"type": "enum", "values": ["png", "jpeg", "webp"]},
    },
}
# A model with NO input_references and no output_format/seed — the "absent key means
# unsupported" case the request builder must honor.
_IMAGE_MODEL_NO_EDIT = {
    "id": "vendor/text-only-image",
    "description": "No reference-image support.",
    "supported_parameters": {"n": {"type": "range", "min": 1, "max": 10}},
}
_VIDEO_MODEL = {
    "id": "google/veo-3.1-fast",
    "description": "Veo 3.1 Fast.",
    "supported_resolutions": ["720p", "1080p", "4K"],
    "supported_aspect_ratios": ["16:9", "9:16"],
    "supported_sizes": None,
    "supported_durations": [4, 6, 8],
    "supported_frame_images": ["first_frame", "last_frame"],
    "generate_audio": True,
    "seed": True,
}


def _image_models_payload(*models):
    return {"data": list(models) or [_IMAGE_MODEL]}


def _video_models_payload(*models):
    return {"data": list(models) or [_VIDEO_MODEL]}


def _image_ok(b64="aGk=", media_type="image/png"):
    return {"created": 1, "data": [{"b64_json": b64, "media_type": media_type}]}


def _image_provider(**kw):
    kw.setdefault("api_key", "k")
    kw.setdefault("name", "or-test")
    return prov.OpenRouterImageProvider(**kw)


def _video_provider(**kw):
    kw.setdefault("api_key", "k")
    kw.setdefault("name", "or-test")
    return prov.OpenRouterVideoProvider(**kw)


# ── Chat spec + registration ──────────────────────────────────────────────────


def test_type_and_catalog_registered():
    reg = get_default_registry()
    assert reg.capability_of("openrouter").type == "openrouter"
    assert reg.catalog_of("openrouter") is not None


def test_spec_defaults():
    assert prov.SPEC.type == "openrouter"
    assert prov.SPEC.default_base_url == "https://openrouter.ai/api/v1"
    assert prov.SPEC.api_key_env == "OPENROUTER_API_KEY"
    assert prov.SPEC.default_model == ""  # de-hardcoded: resolved from discovery
    assert prov.SPEC.fallback_models == ()  # no fake ids when discovery fails
    assert prov.SPEC.max_tokens is None  # openai-wire leaves it unset
    assert {
        Capability.CHAT, Capability.CODE_TOOLS, Capability.STREAMING,
        Capability.VISION, Capability.EMBEDDING,
    } <= prov.SPEC.capabilities


def test_embedding_capability_reaches_the_registered_type():
    # Capability.EMBEDDING on the spec is what flips supports_embeddings on the
    # registered type — the claim the manifest's "embedding" capability makes.
    assert get_default_registry().capability_of("openrouter").supports_embeddings is True


def test_create_provider_uses_default_endpoint(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    p = prov.create_provider({})
    assert p._base_url == "https://openrouter.ai/api/v1"
    assert p._model == ""  # unpinned → resolved from /v1/models at start()


def test_create_provider_config_overrides():
    p = prov.create_provider(
        {"api_key": "k", "model": "anthropic/claude-sonnet-4.5", "endpoint": "https://proxy/v1"}
    )
    assert p._base_url == "https://proxy/v1"
    assert p._model == "anthropic/claude-sonnet-4.5"


def test_create_provider_returns_single_chat_provider():
    # ONE provider, not a list: the media adapters come from the scanners (keyed by
    # config entry), never from this factory. A fal-style list-returning factory
    # cannot key its adapters per account.
    p = prov.create_provider({"api_key": "k"})
    assert not isinstance(p, list)
    assert not isinstance(p, (ImageGenProvider, VideoGenProvider))


def test_registry_build():
    reg = get_default_registry()
    if not any(e.name == "openrouter-inst" for e in reg.list_entries()):
        reg.register_entry(ProviderEntry(
            name="openrouter-inst", type="openrouter", model="m",
            options={"api_key": "k"},
            declared_capabilities=frozenset({Capability.CHAT}),
        ))
    p = reg.build("openrouter-inst")
    assert p._base_url == "https://openrouter.ai/api/v1"


# ── Manifest ↔ code parity ────────────────────────────────────────────────────


def test_manifest_capabilities_are_known_use_cases():
    known = set(CAPABILITIES) | set(CHAT_SUBCATEGORIES) | {c.value for c in Capability}
    declared = set(_MANIFEST["provider"]["capabilities"])
    assert declared <= known, f"unknown capability strings: {sorted(declared - known)}"


def test_manifest_provider_type_matches_spec():
    # A mismatch makes "Add instance" persist an entry no registered type can build.
    assert _MANIFEST["provider"]["providerType"] == prov.SPEC.type


def test_manifest_provider_type_is_model():
    from personalclaw.apps.manifest import PROVIDER_TYPES

    assert _MANIFEST["provider"]["type"] == "model"
    assert _MANIFEST["provider"]["type"] in PROVIDER_TYPES


def test_manifest_declares_network_permission_only():
    # The install-consent surface: this app needs outbound HTTP and nothing else.
    assert _MANIFEST["permissions"] == {"network": True}


def test_manifest_declares_media_capabilities():
    caps = _MANIFEST["provider"]["capabilities"]
    for c in ("chat", "code_tools", "streaming", "vision", "embedding",
              "image_gen", "video_gen"):
        assert c in caps


def test_manifest_says_vision_once_not_twice():
    # Core aliases the Settings→Models capability ``image_modality`` onto the
    # provider-type ``vision`` enum (provider_bridge._CAPABILITY_TO_ENUM), so the two
    # name ONE capability. The manifest list is rendered verbatim as chips by
    # ProviderCard and ModelBackends, so declaring both would print image input twice
    # under two names. ``image_modality`` stays the right vocabulary for PER-MODEL
    # catalog tags (see _capabilities_for) — it is only wrong in this manifest list,
    # where nothing in registry.register() branches on it either.
    caps = _MANIFEST["provider"]["capabilities"]
    assert "vision" in caps
    assert "image_modality" not in caps


def test_manifest_settings_keys_match_the_code_paths():
    props = _MANIFEST["provider"]["settingsSchema"]["properties"]
    assert set(props) == {"api_key", "default_model", "endpoint"}
    assert props["api_key"]["x-meta"]["sensitive"] is True


# ── Scanner / registration wiring ─────────────────────────────────────────────


def test_openrouter_entries_matches_type_and_original_type():
    entries = [
        {"name": "a", "type": "openrouter"},
        {"name": "b", "type": "openai", "options": {"_original_type": "openrouter"}},
        {"name": "c", "type": "google"},
    ]
    assert [e["name"] for e in prov._openrouter_entries(entries)] == ["a", "b"]


def test_scanners_key_adapters_by_entry_name():
    # The structural prune-safety guarantee: an adapter named after its config entry
    # survives _prune_removed_providers because that name is a configured provider.
    entries = [
        {"name": "acct-a", "type": "openrouter", "options": {"api_key": "ka"}},
        {"name": "acct-b", "type": "openrouter", "options": {"api_key": "kb"}},
    ]
    assert [p.name for p in prov._scan_image(entries)] == ["acct-a", "acct-b"]
    assert [p.name for p in prov._scan_video(entries)] == ["acct-a", "acct-b"]


def test_scanners_thread_per_entry_api_key_and_endpoint():
    entries = [
        {"name": "acct-a", "type": "openrouter",
         "options": {"api_key": "ka", "endpoint": "https://proxy-a/v1"}},
        {"name": "acct-b", "type": "openrouter", "options": {"api_key": "kb"}},
    ]
    imgs = prov._scan_image(entries)
    assert [p._api_key for p in imgs] == ["ka", "kb"]
    assert imgs[0]._base() == "https://proxy-a/v1"
    assert imgs[1]._base() == "https://openrouter.ai/api/v1"


def test_adapter_name_is_settable():
    # ModelTypeHandler may assign .name; a read-only property would raise there.
    for p in (_image_provider(), _video_provider()):
        p.name = "renamed"
        assert p.name == "renamed"


def test_scanners_registered_for_both_capabilities():
    for cap, fn in (("image_gen", prov._scan_image), ("video_gen", prov._scan_video)):
        assert fn in media_scanners._scanners.get(cap, [])


def test_openrouter_not_in_openai_family_types():
    # Landmine, asserted as a standing invariant: membership would register a core
    # OpenAI-Images adapter under our own config name, racing our scanner on the same
    # dict key and speaking a body shape OpenRouter's /images rejects.
    assert "openrouter" not in OPENAI_FAMILY_TYPES


# ── Chat/embedding catalog ────────────────────────────────────────────────────


def test_catalog_uses_modality_filter_not_bare_models(monkeypatch):
    # The bare route is TEXT-ONLY upstream (verified: 0 embedding models), so an
    # unfiltered GET would advertise embedding support with an empty picker.
    calls = _fake_fetch(monkeypatch, [_FakeResponse(200, {"data": []})])
    _run(prov.create_catalog({"api_key": "k"}).list_models())
    assert calls[0]["url"] == (
        "https://openrouter.ai/api/v1/models?output_modalities=text,embeddings"
    )


def test_catalog_discovery_url_has_no_double_v1(monkeypatch):
    calls = _fake_fetch(monkeypatch, [_FakeResponse(200, {"data": []})])
    _run(prov.create_catalog({"api_key": "k"}).list_models())
    assert "/v1/v1/" not in calls[0]["url"]


def test_catalog_tags_capabilities_from_declared_modalities(monkeypatch):
    # Declared modalities beat core's id heuristic, which misses 105 of the live
    # image-input models and mis-tags image-output chat models as image_gen.
    _fake_fetch(monkeypatch, [_FakeResponse(200, {"data": [
        {"id": "qwen/qwen3.7-flash",
         "architecture": {"input_modalities": ["text", "image"],
                          "output_modalities": ["text"]}},
        {"id": "google/gemini-3-pro-image",
         "architecture": {"input_modalities": ["text", "image"],
                          "output_modalities": ["image", "text"]}},
        {"id": "baai/bge-m3",
         "architecture": {"input_modalities": ["text"],
                          "output_modalities": ["embeddings"]}},
        {"id": "vendor/omni",
         "architecture": {"input_modalities": ["text", "audio", "video"],
                          "output_modalities": ["text"]}},
    ]})])
    models = _run(prov.create_catalog({"api_key": "k"}).list_models())
    by_id = {m.id: m.capabilities for m in models}
    assert by_id["qwen/qwen3.7-flash"] == ["chat", "image_modality"]
    # image OUTPUT still converses → keeps chat (core's heuristic would say image_gen)
    assert by_id["google/gemini-3-pro-image"] == ["chat", "image_modality"]
    assert by_id["baai/bge-m3"] == ["embedding"]  # exclusive with chat
    assert by_id["vendor/omni"] == ["chat", "audio_modality", "video_modality"]


# ── Image provider ────────────────────────────────────────────────────────────


def test_image_is_available_requires_key(monkeypatch):
    assert _run(_image_provider(api_key="").is_available()) is False
    assert _run(_image_provider(api_key="k").is_available()) is True
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
    assert _run(_image_provider(api_key="").is_available()) is True


def test_image_list_models_uses_dedicated_route(monkeypatch):
    calls = _fake_fetch(monkeypatch, [_FakeResponse(200, _image_models_payload())])
    _run(_image_provider().list_models())
    assert calls[0]["url"] == "https://openrouter.ai/api/v1/images/models"


def test_image_list_models_maps_supported_parameters(monkeypatch):
    _fake_fetch(monkeypatch, [
        _FakeResponse(200, _image_models_payload(_IMAGE_MODEL, _IMAGE_MODEL_NO_EDIT)),
    ])
    models = {m.name: m for m in _run(_image_provider().list_models())}
    m = models["google/gemini-3-pro-image"]
    # resolution tokens then aspect-ratio tokens: both are what /images accepts.
    assert m.sizes == ["1K", "2K", "4K", "1:1", "16:9", "9:16"]
    assert m.supports_edit is True
    assert m.downloaded is True
    assert models["vendor/text-only-image"].supports_edit is False


def test_image_list_models_empty_without_key(monkeypatch):
    calls = _fake_fetch(monkeypatch, [_FakeResponse(200, _image_models_payload())])
    assert _run(_image_provider(api_key="").list_models()) == []
    assert calls == []  # no speculative unauthenticated call


def test_image_list_models_uses_ttl_cache(monkeypatch):
    calls = _fake_fetch(monkeypatch, [_FakeResponse(200, _image_models_payload())])
    p = _image_provider()
    _run(p.list_models())
    _run(p.list_models())
    assert len(calls) == 1


def test_image_list_models_returns_stale_on_error(monkeypatch):
    _fake_fetch(monkeypatch, [_FakeResponse(200, _image_models_payload())])
    p = _image_provider()
    assert len(_run(p.list_models())) == 1
    prov._image_cache["k"] = (0.0, prov._image_cache["k"][1])  # force expiry
    _fake_fetch(monkeypatch, [_FakeResponse(500, {})])
    # A transient 5xx degrades to the last good list, not to an empty picker.
    assert [m.name for m in _run(p.list_models())] == ["google/gemini-3-pro-image"]


def test_image_generate_posts_to_images_endpoint(monkeypatch):
    calls = _fake_fetch(monkeypatch, [
        _FakeResponse(200, _image_models_payload()),
        _FakeResponse(200, _image_ok()),
    ])
    _run(_image_provider().generate("a bike", model="google/gemini-3-pro-image"))
    post = [c for c in calls if c["method"] == "POST"][0]
    assert post["url"] == "https://openrouter.ai/api/v1/images"
    assert "/images/generations" not in post["url"]  # the undocumented alias


def test_image_generate_decodes_b64_to_image_result(monkeypatch):
    _fake_fetch(monkeypatch, [
        _FakeResponse(200, _image_models_payload()),
        _FakeResponse(200, _image_ok(b64="Zm9v", media_type="image/webp")),
    ])
    out = _run(_image_provider().generate("x", model="google/gemini-3-pro-image"))
    assert len(out) == 1
    assert out[0].b64 == "Zm9v"
    assert out[0].mime == "image/webp"
    assert out[0].local_path == ""  # core's _materialize_image persists the bytes


def test_image_generate_never_sends_size_with_resolution(monkeypatch):
    # Exactly one geometry key, ever. Verified live: size + aspect_ratio is a hard
    # 400 (`size "1024x1024" conflicts with aspect_ratio "16:9"`), while
    # size + resolution is currently ACCEPTED with size winning — sending a
    # redundant pair would mean depending on which one upstream happens to prefer.
    calls = _fake_fetch(monkeypatch, [
        _FakeResponse(200, _image_models_payload()),
        _FakeResponse(200, _image_ok()),
    ])
    _run(_image_provider().generate("x", model="google/gemini-3-pro-image", size="2K"))
    body = [c for c in calls if c["method"] == "POST"][0]["body"]
    assert body["resolution"] == "2K"
    assert "size" not in body and "aspect_ratio" not in body

    calls2 = _fake_fetch(monkeypatch, [_FakeResponse(200, _image_ok())])
    _run(_image_provider().generate("x", model="google/gemini-3-pro-image", size="1024x1024"))
    body2 = calls2[0]["body"]
    assert body2["size"] == "1024x1024"
    assert "resolution" not in body2 and "aspect_ratio" not in body2


def test_image_generate_maps_aspect_ratio_token(monkeypatch):
    calls = _fake_fetch(monkeypatch, [
        _FakeResponse(200, _image_models_payload()),
        _FakeResponse(200, _image_ok()),
    ])
    _run(_image_provider().generate("x", model="google/gemini-3-pro-image", size="16:9"))
    body = [c for c in calls if c["method"] == "POST"][0]["body"]
    assert body["aspect_ratio"] == "16:9"
    assert "size" not in body and "resolution" not in body


def test_image_generate_drops_unknown_size_token(monkeypatch):
    calls = _fake_fetch(monkeypatch, [
        _FakeResponse(200, _image_models_payload()),
        _FakeResponse(200, _image_ok()),
    ])
    _run(_image_provider().generate("x", model="google/gemini-3-pro-image", size="gigantic"))
    body = [c for c in calls if c["method"] == "POST"][0]["body"]
    for k in ("size", "resolution", "aspect_ratio"):
        assert k not in body  # let the model default rather than guess a token


def test_rejected_pixel_size_error_names_the_sizes_the_model_accepts(monkeypatch):
    """A 400 on a pixel ``size`` must tell the caller what IS accepted.

    Upstream names only the tier it computed ("Image size 2K is not supported for
    this model"), which is a dead end: the caller asked for a WxH, not for "2K", and
    the pixels→tier mapping is not a published rule (measured live, 1024x1024 ⇒ 1K
    but 1408x768 ⇒ 2K). Without the model's own enum appended there is no next step.
    """
    _fake_fetch(monkeypatch, [
        _FakeResponse(200, _image_models_payload()),
        _FakeResponse(400, {"error": {
            "message": "Image size 2K is not supported for this model", "code": 400}}),
    ])
    with pytest.raises(ImageGenError) as ei:
        _run(_image_provider().generate(
            "x", model="google/gemini-3-pro-image", size="4096x4096"))
    msg = str(ei.value)
    assert "2K" in msg  # upstream's own text is preserved
    assert "This model accepts these sizes:" in msg
    # The enum comes from the live descriptor, not a hardcoded list.
    assert "16:9" in msg


def test_non_size_failures_are_not_decorated_with_a_size_hint(monkeypatch):
    # A 402/429/500 has nothing to do with geometry; appending a size list there
    # would misdirect the user away from the real cause.
    _fake_fetch(monkeypatch, [
        _FakeResponse(200, _image_models_payload()),
        _FakeResponse(402, {"error": {"message": "Insufficient credits", "code": 402}}),
    ])
    with pytest.raises(ImageGenError) as ei:
        _run(_image_provider().generate("x", model="google/gemini-3-pro-image"))
    assert "accepts these sizes" not in str(ei.value)


def test_image_generate_clamps_n_to_model_cap(monkeypatch):
    calls = _fake_fetch(monkeypatch, [
        _FakeResponse(200, _image_models_payload()),
        _FakeResponse(200, _image_ok()),
    ])
    _run(_image_provider().generate("x", model="google/gemini-3-pro-image", n=5))
    assert [c for c in calls if c["method"] == "POST"][0]["body"]["n"] == 1  # live max, not 10


def test_image_generate_omits_unsupported_params(monkeypatch):
    calls = _fake_fetch(monkeypatch, [
        _FakeResponse(200, _image_models_payload(_IMAGE_MODEL_NO_EDIT)),
        _FakeResponse(200, _image_ok()),
    ])
    _run(_image_provider().generate("x", model="vendor/text-only-image"))
    body = [c for c in calls if c["method"] == "POST"][0]["body"]
    assert "output_format" not in body  # absent descriptor key ⇒ unsupported
    assert "seed" not in body
    assert body["n"] == 1


def test_image_generate_sends_bearer_and_title_headers(monkeypatch):
    calls = _fake_fetch(monkeypatch, [
        _FakeResponse(200, _image_models_payload()),
        _FakeResponse(200, _image_ok()),
    ])
    _run(_image_provider().generate("x", model="google/gemini-3-pro-image"))
    headers = [c for c in calls if c["method"] == "POST"][0]["headers"]
    assert headers["Authorization"] == "Bearer k"
    assert headers["X-OpenRouter-Title"] == "PersonalClaw"
    assert "X-Title" not in headers  # the legacy alias


def test_image_generate_raises_without_key(monkeypatch):
    calls = _fake_fetch(monkeypatch, [_FakeResponse(200, _image_ok())])
    with pytest.raises(ImageGenError, match="OPENROUTER_API_KEY"):
        _run(_image_provider(api_key="").generate("x"))
    assert calls == []


def test_image_generate_raises_when_discovery_empty(monkeypatch):
    _fake_fetch(monkeypatch, [_FakeResponse(200, {"data": []})])
    with pytest.raises(ImageGenError, match="No OpenRouter image-generation model"):
        _run(_image_provider().generate("x"))


@pytest.mark.parametrize(("status", "needle"), [
    (401, "rejected the API key"),
    (403, "rejected the API key"),
    (402, "insufficient credits"),
    (413, "too large"),
    (502, "upstream provider failed"),
    (524, "timed out or is overloaded"),
    (529, "timed out or is overloaded"),
    (418, "HTTP 418"),
])
def test_image_generate_error_mapping_by_status(monkeypatch, status, needle):
    _fake_fetch(monkeypatch, [
        _FakeResponse(200, _image_models_payload()),
        _FakeResponse(status, {"error": {"message": "upstream detail", "code": status}}),
    ])
    with pytest.raises(ImageGenError, match=needle):
        _run(_image_provider().generate("x", model="google/gemini-3-pro-image"))


def test_image_generate_ignores_cookie_auth_message(monkeypatch):
    # Verified live: an unauthenticated POST /images returns the misleading
    # "No cookie auth credentials found". Keying off the STATUS keeps the advice right.
    _fake_fetch(monkeypatch, [
        _FakeResponse(200, _image_models_payload()),
        _FakeResponse(401, {"error": {"message": "No cookie auth credentials found",
                                      "code": 401}}),
    ])
    with pytest.raises(ImageGenError) as exc:
        _run(_image_provider().generate("x", model="google/gemini-3-pro-image"))
    assert "rejected the API key" in str(exc.value)
    assert "cookie" not in str(exc.value)


def test_image_generate_honors_retry_after_once(monkeypatch, _no_sleep):
    _fake_fetch(monkeypatch, [
        _FakeResponse(200, _image_models_payload()),
        _FakeResponse(429, {}, headers={"Retry-After": "2"}),
        _FakeResponse(200, _image_ok()),
    ])
    out = _run(_image_provider().generate("x", model="google/gemini-3-pro-image"))
    assert len(out) == 1
    assert _no_sleep == [2.0]


def test_image_generate_retry_after_is_clamped(monkeypatch, _no_sleep):
    # Clamped to [1,30] so an upstream can't stall a turn indefinitely.
    _fake_fetch(monkeypatch, [
        _FakeResponse(200, _image_models_payload()),
        _FakeResponse(429, {}, headers={"Retry-After": "9999"}),
        _FakeResponse(200, _image_ok()),
    ])
    _run(_image_provider().generate("x", model="google/gemini-3-pro-image"))
    assert _no_sleep == [30.0]


def test_image_generate_second_429_raises(monkeypatch, _no_sleep):
    _fake_fetch(monkeypatch, [
        _FakeResponse(200, _image_models_payload()),
        _FakeResponse(429, {}, headers={"Retry-After": "1"}),
    ])
    with pytest.raises(ImageGenError, match="rate-limited"):
        _run(_image_provider().generate("x", model="google/gemini-3-pro-image"))


def test_image_generate_raises_when_no_data(monkeypatch):
    _fake_fetch(monkeypatch, [
        _FakeResponse(200, _image_models_payload()),
        _FakeResponse(200, {"created": 1, "data": []}),
    ])
    with pytest.raises(ImageGenError, match="no image data"):
        _run(_image_provider().generate("x", model="google/gemini-3-pro-image"))


def test_image_generate_uses_a_policy_that_wont_truncate(monkeypatch):
    # CONNECTOR's 10MB/20s would silently truncate a large base64 body and time out
    # a 120s generation, so the POST must carry raised caps.
    calls = _fake_fetch(monkeypatch, [
        _FakeResponse(200, _image_models_payload()),
        _FakeResponse(200, _image_ok()),
    ])
    _run(_image_provider().generate("x", model="google/gemini-3-pro-image"))
    policy = [c for c in calls if c["method"] == "POST"][0]["policy"]
    assert policy.max_bytes >= 64_000_000
    assert policy.timeout_s >= prov._IMAGE_TIMEOUT_S


def test_image_edit_sends_input_references_data_uri(monkeypatch, tmp_path):
    src = tmp_path / "src.png"
    src.write_bytes(b"\x89PNG-bytes")
    calls = _fake_fetch(monkeypatch, [
        _FakeResponse(200, _image_models_payload()),
        _FakeResponse(200, _image_ok()),
    ])
    _run(_image_provider().edit(
        "make it blue", source_image=str(src), model="google/gemini-3-pro-image",
    ))
    body = [c for c in calls if c["method"] == "POST"][0]["body"]
    ref = body["input_references"][0]
    assert ref["type"] == "image_url"
    assert ref["image_url"]["url"].startswith("data:image/png;base64,")


def test_image_edit_rejects_mask(monkeypatch, tmp_path):
    src = tmp_path / "src.png"
    src.write_bytes(b"x")
    calls = _fake_fetch(monkeypatch, [_FakeResponse(200, _image_models_payload())])
    with pytest.raises(ImageGenError, match="mask/inpainting"):
        _run(_image_provider().edit("x", source_image=str(src), mask=str(src)))
    assert calls == []  # refused before any request


def test_image_edit_rejects_model_without_input_references(monkeypatch, tmp_path):
    src = tmp_path / "src.png"
    src.write_bytes(b"x")
    calls = _fake_fetch(monkeypatch, [
        _FakeResponse(200, _image_models_payload(_IMAGE_MODEL_NO_EDIT)),
    ])
    with pytest.raises(ImageGenError, match="does not accept input images"):
        _run(_image_provider().edit(
            "x", source_image=str(src), model="vendor/text-only-image",
        ))
    # Discovery happened, but no POST — an impossible edit costs nothing.
    assert [c["method"] for c in calls] == ["GET"]


def test_image_edit_reports_unreadable_source(monkeypatch, tmp_path):
    _fake_fetch(monkeypatch, [_FakeResponse(200, _image_models_payload())])
    with pytest.raises(ImageGenError, match="Could not read source image"):
        _run(_image_provider().edit(
            "x", source_image=str(tmp_path / "missing.png"),
            model="google/gemini-3-pro-image",
        ))


def test_module_uses_guarded_fetch_not_raw_aiohttp():
    # Locks the sdk.net.fetch chokepoint (host classification, redirect re-check,
    # byte cap, SEL audit) instead of google-models' raw aiohttp.
    source = (Path(__file__).parent / "provider.py").read_text(encoding="utf-8")
    assert "aiohttp" not in source
    assert "ClientSession" not in source


# ── Video provider ────────────────────────────────────────────────────────────


def test_video_is_available_requires_key(monkeypatch):
    assert _run(_video_provider(api_key="").is_available()) is False
    assert _run(_video_provider(api_key="k").is_available()) is True
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
    assert _run(_video_provider(api_key="").is_available()) is True


def test_video_list_models_uses_dedicated_route(monkeypatch):
    calls = _fake_fetch(monkeypatch, [_FakeResponse(200, _video_models_payload())])
    _run(_video_provider().list_models())
    assert calls[0]["url"] == "https://openrouter.ai/api/v1/videos/models"


def test_video_list_models_maps_explicit_arrays(monkeypatch):
    _fake_fetch(monkeypatch, [_FakeResponse(200, _video_models_payload())])
    m = _run(_video_provider().list_models())[0]
    assert m.name == "google/veo-3.1-fast"
    assert m.aspect_ratios == ["16:9", "9:16"]
    assert m.max_duration_s == 8
    assert m.downloaded is True


def test_video_list_models_tolerates_null_arrays(monkeypatch):
    _fake_fetch(monkeypatch, [_FakeResponse(200, _video_models_payload({
        "id": "x-ai/grok-imagine-video-1.5",
        "supported_aspect_ratios": None,
        "supported_durations": None,
        "generate_audio": None,
        "seed": None,
    }))])
    m = _run(_video_provider().list_models())[0]
    assert m.aspect_ratios == []  # null ⇒ [], not a TypeError
    assert m.max_duration_s == 10  # the dataclass default


def test_video_list_models_does_not_hardcode_ratios(monkeypatch):
    seven = ["1:1", "3:4", "9:16", "4:3", "16:9", "21:9", "9:21"]
    _fake_fetch(monkeypatch, [_FakeResponse(200, _video_models_payload({
        "id": "bytedance/seedance-2.0", "supported_aspect_ratios": seven,
        "supported_durations": [4, 15],
    }))])
    m = _run(_video_provider().list_models())[0]
    assert m.aspect_ratios == seven  # not the baked ["16:9","9:16"]
    assert m.max_duration_s == 15


def test_video_list_models_empty_without_key(monkeypatch):
    calls = _fake_fetch(monkeypatch, [_FakeResponse(200, _video_models_payload())])
    assert _run(_video_provider(api_key="").list_models()) == []
    assert calls == []


def _video_happy(monkeypatch, mp4=b"\x00\x00\x00 ftypmp42"):
    return _fake_fetch(monkeypatch, [
        _FakeResponse(200, _video_models_payload()),                 # discovery
        _FakeResponse(202, {"id": "job-1", "status": "pending"}),    # submit
        _FakeResponse(200, {"id": "job-1", "status": "in_progress"}),
        _FakeResponse(200, {"id": "job-1", "status": "completed"}),
        _FakeResponse(200, body=mp4, headers={"Content-Type": "video/mp4"}),
    ])


def test_video_generate_accepts_202_and_polls_to_completed(monkeypatch, _no_sleep):
    calls = _video_happy(monkeypatch)
    out = _run(_video_provider().generate("waves", model="google/veo-3.1-fast"))
    assert len(out) == 1
    urls = [c["url"] for c in calls]
    assert urls[1] == "https://openrouter.ai/api/v1/videos"
    # The canonical poll URL we construct — never the vendor's returned polling_url.
    assert urls[2] == "https://openrouter.ai/api/v1/videos/job-1"
    assert urls[-1] == "https://openrouter.ai/api/v1/videos/job-1/content?index=0"


def test_video_generate_sleeps_between_polls(monkeypatch, _no_sleep):
    _video_happy(monkeypatch)
    _run(_video_provider().generate("waves", model="google/veo-3.1-fast"))
    assert _no_sleep and all(s == prov._VIDEO_POLL_INTERVAL_S for s in _no_sleep)


def test_video_generate_returns_local_path_not_url(monkeypatch, _no_sleep):
    # local_path takes core's uncapped read path; a returned url would be fetched
    # with bare CONNECTOR (10MB) and silently truncated.
    _video_happy(monkeypatch, mp4=b"MP4-BYTES")
    out = _run(_video_provider().generate("waves", model="google/veo-3.1-fast"))
    assert out[0].url == ""
    assert Path(out[0].local_path).read_bytes() == b"MP4-BYTES"
    assert out[0].mime == "video/mp4"
    assert out[0].duration_s == 4  # the snapped duration


def test_video_generate_raises_on_truncated_download(monkeypatch, _no_sleep):
    _fake_fetch(monkeypatch, [
        _FakeResponse(200, _video_models_payload()),
        _FakeResponse(202, {"id": "job-1", "status": "pending"}),
        _FakeResponse(200, {"id": "job-1", "status": "completed"}),
        _FakeResponse(200, body=b"partial", truncated=True),
    ])
    with pytest.raises(VideoGenError, match="truncated"):
        _run(_video_provider().generate("waves", model="google/veo-3.1-fast"))


def test_video_generate_raises_on_empty_body(monkeypatch, _no_sleep):
    _fake_fetch(monkeypatch, [
        _FakeResponse(200, _video_models_payload()),
        _FakeResponse(202, {"id": "job-1", "status": "pending"}),
        _FakeResponse(200, {"id": "job-1", "status": "completed"}),
        _FakeResponse(200, body=b""),
    ])
    with pytest.raises(VideoGenError, match="empty video body"):
        _run(_video_provider().generate("waves", model="google/veo-3.1-fast"))


@pytest.mark.parametrize(("status", "needle"), [
    ("failed", "failed"),
    ("cancelled", "was cancelled"),
    ("expired", "expired before its output"),
])
def test_video_generate_handles_each_terminal_bad_status(
    monkeypatch, _no_sleep, status, needle,
):
    # All three, explicitly: the docs' table lists only four of the six states.
    _fake_fetch(monkeypatch, [
        _FakeResponse(200, _video_models_payload()),
        _FakeResponse(202, {"id": "job-1", "status": "pending"}),
        _FakeResponse(200, {"id": "job-1", "status": status, "error": "boom"}),
    ])
    with pytest.raises(VideoGenError, match=needle):
        _run(_video_provider().generate("waves", model="google/veo-3.1-fast"))


def test_video_generate_treats_unknown_status_as_pending(monkeypatch, _no_sleep):
    # Forward-compat: a status OpenRouter adds later must not crash the turn.
    monkeypatch.setattr(prov, "_VIDEO_TIMEOUT_S", 10.0)
    _fake_fetch(monkeypatch, [
        _FakeResponse(200, _video_models_payload()),
        _FakeResponse(202, {"id": "job-1", "status": "pending"}),
        _FakeResponse(200, {"id": "job-1", "status": "queued"}),
    ])
    with pytest.raises(VideoGenError, match="timed out"):
        _run(_video_provider().generate("waves", model="google/veo-3.1-fast"))
    assert _no_sleep  # kept polling rather than raising on the unknown status


def test_video_generate_times_out(monkeypatch, _no_sleep):
    monkeypatch.setattr(prov, "_VIDEO_TIMEOUT_S", 10.0)
    _fake_fetch(monkeypatch, [
        _FakeResponse(200, _video_models_payload()),
        _FakeResponse(202, {"id": "job-1", "status": "pending"}),
        _FakeResponse(200, {"id": "job-1", "status": "in_progress"}),
    ])
    with pytest.raises(VideoGenError, match="timed out after 10s"):
        _run(_video_provider().generate("waves", model="google/veo-3.1-fast"))


def test_video_timeout_default_is_600s():
    # OpenRouter serves 20s clips and 4K/15s jobs; 300s times out a working job.
    assert prov._VIDEO_TIMEOUT_S == 600.0


def test_video_poll_stops_on_auth_failure(monkeypatch, _no_sleep):
    # A 401 mid-poll is not transient; retrying to the timeout would hide it.
    _fake_fetch(monkeypatch, [
        _FakeResponse(200, _video_models_payload()),
        _FakeResponse(202, {"id": "job-1", "status": "pending"}),
        _FakeResponse(401, {"error": {"message": "No cookie auth credentials found"}}),
    ])
    with pytest.raises(VideoGenError, match="rejected the API key"):
        _run(_video_provider().generate("waves", model="google/veo-3.1-fast"))


def test_video_poll_retries_transient_error(monkeypatch, _no_sleep):
    # One 5xx must not abandon a job the user is already paying for.
    _fake_fetch(monkeypatch, [
        _FakeResponse(200, _video_models_payload()),
        _FakeResponse(202, {"id": "job-1", "status": "pending"}),
        _FakeResponse(503, {}),
        _FakeResponse(200, {"id": "job-1", "status": "completed"}),
        _FakeResponse(200, body=b"MP4", headers={"Content-Type": "video/mp4"}),
    ])
    out = _run(_video_provider().generate("waves", model="google/veo-3.1-fast"))
    assert Path(out[0].local_path).read_bytes() == b"MP4"


@pytest.mark.parametrize(("want", "durations", "expected"), [
    (7.0, [4, 6, 8], 6),      # nearest
    (5.0, [5, 10], 5),        # exact
    (100.0, [4, 6, 8], 8),    # clamped up
    (1.0, [4, 6, 8], 4),      # clamped down
])
def test_video_generate_snaps_duration_to_supported(
    monkeypatch, _no_sleep, want, durations, expected,
):
    calls = _fake_fetch(monkeypatch, [
        _FakeResponse(200, _video_models_payload(
            {**_VIDEO_MODEL, "supported_durations": durations})),
        _FakeResponse(202, {"id": "job-1", "status": "pending"}),
        _FakeResponse(200, {"id": "job-1", "status": "completed"}),
        _FakeResponse(200, body=b"MP4"),
    ])
    _run(_video_provider().generate(
        "x", model="google/veo-3.1-fast", duration_seconds=want,
    ))
    assert [c for c in calls if c["method"] == "POST"][0]["body"]["duration"] == expected


def test_video_generate_always_sends_generate_audio(monkeypatch, _no_sleep):
    calls = _fake_fetch(monkeypatch, [
        _FakeResponse(200, _video_models_payload()),
        _FakeResponse(202, {"id": "job-1", "status": "pending"}),
        _FakeResponse(200, {"id": "job-1", "status": "completed"}),
        _FakeResponse(200, body=b"MP4"),
    ])
    _run(_video_provider().generate("x", model="google/veo-3.1-fast"))
    # Explicit because the docs and the OpenAPI schema disagree on the default.
    assert [c for c in calls if c["method"] == "POST"][0]["body"]["generate_audio"] is True


def test_video_generate_omits_generate_audio_when_unsupported(monkeypatch, _no_sleep):
    calls = _fake_fetch(monkeypatch, [
        _FakeResponse(200, _video_models_payload(
            {**_VIDEO_MODEL, "generate_audio": None})),
        _FakeResponse(202, {"id": "job-1", "status": "pending"}),
        _FakeResponse(200, {"id": "job-1", "status": "completed"}),
        _FakeResponse(200, body=b"MP4"),
    ])
    _run(_video_provider().generate("x", model="google/veo-3.1-fast"))
    assert "generate_audio" not in [c for c in calls if c["method"] == "POST"][0]["body"]


def test_video_generate_honors_generate_audio_opt(monkeypatch, _no_sleep):
    calls = _fake_fetch(monkeypatch, [
        _FakeResponse(200, _video_models_payload()),
        _FakeResponse(202, {"id": "job-1", "status": "pending"}),
        _FakeResponse(200, {"id": "job-1", "status": "completed"}),
        _FakeResponse(200, body=b"MP4"),
    ])
    _run(_video_provider().generate(
        "x", model="google/veo-3.1-fast", generate_audio=False,
    ))
    assert [c for c in calls if c["method"] == "POST"][0]["body"]["generate_audio"] is False


def test_video_generate_omits_unsupported_aspect_ratio(monkeypatch, _no_sleep):
    calls = _fake_fetch(monkeypatch, [
        _FakeResponse(200, _video_models_payload()),  # supports 16:9, 9:16
        _FakeResponse(202, {"id": "job-1", "status": "pending"}),
        _FakeResponse(200, {"id": "job-1", "status": "completed"}),
        _FakeResponse(200, body=b"MP4"),
    ])
    _run(_video_provider().generate(
        "x", model="google/veo-3.1-fast", aspect_ratio="21:9",
    ))
    assert "aspect_ratio" not in [c for c in calls if c["method"] == "POST"][0]["body"]


def test_video_generate_sends_supported_aspect_ratio(monkeypatch, _no_sleep):
    calls = _fake_fetch(monkeypatch, [
        _FakeResponse(200, _video_models_payload()),
        _FakeResponse(202, {"id": "job-1", "status": "pending"}),
        _FakeResponse(200, {"id": "job-1", "status": "completed"}),
        _FakeResponse(200, body=b"MP4"),
    ])
    _run(_video_provider().generate("x", model="google/veo-3.1-fast", aspect_ratio="9:16"))
    assert [c for c in calls if c["method"] == "POST"][0]["body"]["aspect_ratio"] == "9:16"


def test_video_generate_prefers_frame_images_over_input_references(monkeypatch, _no_sleep):
    calls = _fake_fetch(monkeypatch, [
        _FakeResponse(200, _video_models_payload()),
        _FakeResponse(202, {"id": "job-1", "status": "pending"}),
        _FakeResponse(200, {"id": "job-1", "status": "completed"}),
        _FakeResponse(200, body=b"MP4"),
    ])
    _run(_video_provider().generate(
        "x", model="google/veo-3.1-fast",
        frame_images=[{"frame_type": "first_frame", "image_url": {"url": "data:,x"}}],
        input_references=[{"type": "image_url", "image_url": {"url": "data:,y"}}],
    ))
    body = [c for c in calls if c["method"] == "POST"][0]["body"]
    assert body["frame_images"][0]["frame_type"] == "first_frame"
    assert "input_references" not in body  # documented precedence; never both


def test_video_generate_sends_input_references_when_no_frames(monkeypatch, _no_sleep):
    calls = _fake_fetch(monkeypatch, [
        _FakeResponse(200, _video_models_payload()),
        _FakeResponse(202, {"id": "job-1", "status": "pending"}),
        _FakeResponse(200, {"id": "job-1", "status": "completed"}),
        _FakeResponse(200, body=b"MP4"),
    ])
    _run(_video_provider().generate(
        "x", model="google/veo-3.1-fast",
        input_references=[{"type": "image_url", "image_url": {"url": "data:,y"}}],
    ))
    body = [c for c in calls if c["method"] == "POST"][0]["body"]
    assert body["input_references"] and "frame_images" not in body


def test_video_generate_rejects_unsupported_frame_type(monkeypatch, _no_sleep):
    calls = _fake_fetch(monkeypatch, [
        _FakeResponse(200, _video_models_payload(
            {**_VIDEO_MODEL, "supported_frame_images": ["first_frame"]})),
    ])
    with pytest.raises(VideoGenError, match="frame_type"):
        _run(_video_provider().generate(
            "x", model="google/veo-3.1-fast",
            frame_images=[{"frame_type": "last_frame", "image_url": {"url": "data:,x"}}],
        ))
    assert [c["method"] for c in calls] == ["GET"]  # rejected before the submit


def test_video_generate_rejects_frames_when_model_has_none(monkeypatch, _no_sleep):
    _fake_fetch(monkeypatch, [
        _FakeResponse(200, _video_models_payload(
            {**_VIDEO_MODEL, "supported_frame_images": None})),  # sora-2-pro's shape
    ])
    with pytest.raises(VideoGenError, match="does not accept frame images"):
        _run(_video_provider().generate(
            "x", model="google/veo-3.1-fast",
            frame_images=[{"frame_type": "first_frame", "image_url": {"url": "data:,x"}}],
        ))


def test_video_generate_allows_empty_prompt_with_frame_image(monkeypatch, _no_sleep):
    # prompt is NOT required upstream — image-only generation is legal.
    calls = _fake_fetch(monkeypatch, [
        _FakeResponse(200, _video_models_payload()),
        _FakeResponse(202, {"id": "job-1", "status": "pending"}),
        _FakeResponse(200, {"id": "job-1", "status": "completed"}),
        _FakeResponse(200, body=b"MP4"),
    ])
    _run(_video_provider().generate(
        "", model="google/veo-3.1-fast",
        frame_images=[{"frame_type": "first_frame", "image_url": {"url": "data:,x"}}],
    ))
    body = [c for c in calls if c["method"] == "POST"][0]["body"]
    assert "prompt" not in body and body["frame_images"]


def test_video_generate_rejects_empty_prompt_without_references(monkeypatch, _no_sleep):
    _fake_fetch(monkeypatch, [_FakeResponse(200, _video_models_payload())])
    with pytest.raises(VideoGenError, match="needs a prompt or a reference"):
        _run(_video_provider().generate("", model="google/veo-3.1-fast"))


def test_video_generate_raises_without_key(monkeypatch):
    calls = _fake_fetch(monkeypatch, [_FakeResponse(200, {})])
    with pytest.raises(VideoGenError, match="OPENROUTER_API_KEY"):
        _run(_video_provider(api_key="").generate("x"))
    assert calls == []


def test_video_generate_raises_when_submit_returns_no_id(monkeypatch, _no_sleep):
    _fake_fetch(monkeypatch, [
        _FakeResponse(200, _video_models_payload()),
        _FakeResponse(202, {"status": "pending"}),
    ])
    with pytest.raises(VideoGenError, match="no job id"):
        _run(_video_provider().generate("x", model="google/veo-3.1-fast"))


@pytest.mark.parametrize(("status", "needle"), [
    (401, "rejected the API key"),
    (402, "insufficient credits"),
    (429, "rate-limited"),
    (502, "upstream provider failed"),
    (529, "timed out or is overloaded"),
])
def test_video_submit_error_mapping_by_status(monkeypatch, _no_sleep, status, needle):
    _fake_fetch(monkeypatch, [
        _FakeResponse(200, _video_models_payload()),
        _FakeResponse(status, {"error": {"message": "detail", "code": status}}),
    ])
    with pytest.raises(VideoGenError, match=needle):
        _run(_video_provider().generate("x", model="google/veo-3.1-fast"))


def test_video_download_uses_a_policy_that_wont_truncate(monkeypatch, _no_sleep):
    calls = _video_happy(monkeypatch)
    _run(_video_provider().generate("x", model="google/veo-3.1-fast"))
    policy = calls[-1]["policy"]
    assert policy.max_bytes >= 256_000_000  # a 4K/15s clip dwarfs CONNECTOR's 10MB
