"""Unit tests for the google model provider app.

The OpenAI SDK is stubbed (construction triggers its lazy import); these tests assert
the app's spec wiring — the registered TYPE, the default base URL, api-key env
fallback, and that both the config-path and registry-path factories build a provider
pinned to the right endpoint — and what a failed image or video call tells the user,
against a fake HTTP session.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types

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


import provider as prov  # app-local; registers type + catalog on import

from personalclaw.llm.registry import ProviderEntry, get_default_registry
from personalclaw.llm.capabilities import Capability


def test_type_and_catalog_registered():
    reg = get_default_registry()
    assert reg.capability_of("google").type == "google"
    assert reg.catalog_of("google") is not None


def test_spec_defaults():
    assert prov.SPEC.type == "google"
    assert prov.SPEC.default_base_url == "https://generativelanguage.googleapis.com/v1beta/openai/"
    assert prov.SPEC.api_key_env == "GEMINI_API_KEY"
    assert prov.SPEC.default_model == ""  # de-hardcoded: discovery-resolved, no baked id
    assert Capability.CHAT in prov.SPEC.capabilities


def test_create_provider_uses_default_endpoint(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    p = prov.create_provider({})
    assert p._base_url == "https://generativelanguage.googleapis.com/v1beta/openai/"
    assert p._model == ""  # unpinned → empty at construction; resolved from /v1/models at start()


def test_create_provider_config_overrides(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    p = prov.create_provider({"api_key": "k", "model": "custom-model", "endpoint": "https://proxy/v1"})
    assert p._base_url == "https://proxy/v1"
    assert p._model == "custom-model"


def test_registry_build(monkeypatch):
    reg = get_default_registry()
    if not any(e.name == "google-inst" for e in reg.list_entries()):
        reg.register_entry(ProviderEntry(
            name="google-inst", type="google", model="m",
            options={"api_key": "k"},
            declared_capabilities=frozenset({Capability.CHAT}),
        ))
    p = reg.build("google-inst")
    assert p._base_url == "https://generativelanguage.googleapis.com/v1beta/openai/"


# ── What a failed media call says ─────────────────────────────────────────────

_KEY = "fake-gemini-key-test"


def _fake_gemini(monkeypatch, *answers):
    """Answer each request with the next of ``answers``, the last one repeating.

    A ``(status, payload)`` pair is Gemini's answer (a str payload is sent as it is); an
    exception is raised as the HTTP library would raise it.
    """
    import aiohttp

    queue = list(answers)

    class _Response:
        def __init__(self, status, payload):
            self.status = status
            self._text = payload if isinstance(payload, str) else json.dumps(payload)

        async def text(self):
            return self._text

    class _Request:
        def __init__(self):
            self._answer = queue.pop(0) if len(queue) > 1 else queue[0]

        async def __aenter__(self):
            if isinstance(self._answer, BaseException):
                raise self._answer
            return _Response(*self._answer)

        async def __aexit__(self, *_exc):
            return False

    class _Session:
        def __init__(self, *_a, **_k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        def post(self, _url, **_k):
            return _Request()

        def get(self, _url, **_k):
            return _Request()

    monkeypatch.setattr(aiohttp, "ClientSession", _Session)


def _discovers(monkeypatch, *models):
    async def _discover(_key):
        return list(models)

    monkeypatch.setattr(prov, "_discover_models", _discover)


def _image(model="gemini-image-test"):
    return asyncio.run(prov.GeminiImageProvider(api_key=_KEY).generate("a heron", model=model))


def _video():
    return asyncio.run(prov.GeminiVideoProvider(api_key=_KEY).generate("a heron", model="veo-test"))


@pytest.mark.parametrize(("status", "sentence"), [
    (400, "Gemini refused the image request as invalid (HTTP 400). It answers an API key it "
          "does not recognize this way too, so check Google Gemini API Key on this Google Gemini "
          "instance in Settings → Providers (or GEMINI_API_KEY); if the key is right, choose "
          "another model in Settings → Models or change what you asked for."),
    (403, "Gemini refused the API key for the image request (HTTP 403). Check Google Gemini API "
          "Key on this Google Gemini instance in Settings → Providers (or GEMINI_API_KEY), and "
          "that the key's Google Cloud project may use this model."),
    (404, "Gemini has no such model for the image request (HTTP 404). Choose another model in "
          "Settings → Models."),
    (429, "Gemini's quota or rate limit stopped the image request (HTTP 429). Wait a minute and "
          "try again, or check the quota of the key's Google Cloud project."),
    (503, "Gemini failed on its side (image request, HTTP 503). Try again in a few minutes."),
    (418, "Gemini's image request failed (HTTP 418). Try again; if it fails the same way, choose "
          "another model in Settings → Models."),
])
def test_a_failed_image_request_says_what_to_do_then_geminis_words(monkeypatch, status, sentence):
    """These read "failed (HTTP n):" and Gemini's words, with no next step."""
    from personalclaw.sdk.image import ImageGenError

    _discovers(monkeypatch)
    _fake_gemini(monkeypatch, (status, {"error": {"message": "upstream detail"}}))
    with pytest.raises(ImageGenError) as ei:
        _image()
    assert str(ei.value) == f"{sentence} Details: upstream detail"


def test_an_imagen_request_that_cannot_connect_says_what_to_check(monkeypatch):
    from personalclaw.sdk.image import ImageGenError

    _discovers(
        monkeypatch, {"name": "models/imagen-test", "supportedGenerationMethods": ["predict"]},
    )
    _fake_gemini(monkeypatch, ConnectionResetError(54, "Connection reset by peer"))
    with pytest.raises(ImageGenError) as ei:
        _image(model="imagen-test")
    assert str(ei.value) == (
        "The connection to Gemini failed during the Imagen request. Check this computer's "
        "internet connection, then try again. Details: [Errno 54] Connection reset by peer"
    )


def test_an_answer_that_is_not_json_says_it_could_not_be_read(monkeypatch):
    # A 200 whose body is not JSON (a proxy's page) read "request failed:" and the parser's words.
    from personalclaw.sdk.image import ImageGenError

    _discovers(monkeypatch)
    _fake_gemini(monkeypatch, (200, "<html>proxy page</html>"))
    with pytest.raises(ImageGenError) as ei:
        _image()
    assert str(ei.value) == (
        "Gemini's answer to the image request could not be read. Try again in a moment. "
        "Details: Expecting value: line 1 column 1 (char 0)"
    )


def test_the_api_key_in_a_native_url_never_reaches_the_details(monkeypatch):
    """The native calls carry the key in the URL, and an HTTP library's error can quote that
    URL. The key is taken out of the details; the rest of the error's words are kept."""
    from personalclaw.sdk.image import ImageGenError

    url = f"{prov._NATIVE_BASE}models/gemini-image-test:generateContent?key={_KEY}"
    _discovers(monkeypatch)
    _fake_gemini(monkeypatch, RuntimeError(f"too many redirects for {url}"))
    with pytest.raises(ImageGenError) as ei:
        _image()
    assert _KEY not in str(ei.value)
    assert str(ei.value) == (
        "The image request to Gemini failed unexpectedly. Try again in a moment. Details: too "
        "many redirects for https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-image-test:generateContent?key=[REDACTED: credential]"
    )


@pytest.mark.parametrize(("answer", "sentence"), [
    (asyncio.TimeoutError(), "The Veo video request to Gemini timed out. Try again in a moment."),
    ((400, {"error": {"message": "API key not valid. Please pass a valid API key."}}),
     "Gemini refused the Veo video request as invalid (HTTP 400). It answers an API key it does "
     "not recognize this way too, so check Google Gemini API Key on this Google Gemini instance "
     "in Settings → Providers (or GEMINI_API_KEY); if the key is right, choose another model in "
     "Settings → Models or change what you asked for. Details: API key not valid. Please pass a "
     "valid API key."),
])
def test_a_failed_veo_submit_says_what_to_do(monkeypatch, answer, sentence):
    # A timed-out submit read "Veo submit request failed: " with nothing after it.
    from personalclaw.sdk.video import VideoGenError

    _fake_gemini(monkeypatch, answer)
    with pytest.raises(VideoGenError) as ei:
        _video()
    assert str(ei.value) == sentence


@pytest.mark.parametrize("error", [
    {"code": 3, "message": "The prompt was blocked."},
    "The prompt was blocked.",
])
def test_a_failed_veo_job_says_what_to_try_then_geminis_reason(monkeypatch, error):
    """The reason used to be the whole message after "Veo generation failed:", and a reason that
    was not an object raised AttributeError instead of any message at all."""
    from personalclaw.sdk.video import VideoGenError

    _fake_gemini(
        monkeypatch,
        (200, {"name": "operations/op-test"}),
        (200, {"done": True, "error": error}),
    )
    with pytest.raises(VideoGenError) as ei:
        _video()
    assert str(ei.value) == (
        "Veo's video job failed, so no video was made. Try again; if it fails again, change the "
        "prompt or choose another model in Settings → Models. Details: The prompt was blocked."
    )
