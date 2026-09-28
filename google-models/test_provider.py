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
    """Answer each request with the next of ``answers``, the last one repeating, and return the
    requests sent, as ``(method, url, headers, follows_redirects)``.

    A ``(status, payload)`` or ``(status, payload, headers)`` tuple is Gemini's answer (a str or
    bytes payload is sent as it is, anything else as JSON); an exception is raised as the HTTP
    library would raise it, and a callable is given the requested URL and returns the exception.
    """
    import aiohttp

    queue = list(answers)
    sent: list[tuple[str, str, dict, bool]] = []

    class _Content:
        def __init__(self, body: bytes):
            self._body = body

        async def iter_chunked(self, size):
            for start in range(0, len(self._body), size):
                yield self._body[start:start + size]

    class _Response:
        def __init__(self, status, payload, headers=None):
            self.status = status
            self.headers = dict(headers or {})
            if isinstance(payload, bytes):
                self._body = payload
            else:
                self._body = (payload if isinstance(payload, str) else json.dumps(payload)).encode()
            self.content = _Content(self._body)

        async def text(self):
            return self._body.decode()

    class _Request:
        def __init__(self, url):
            self._url = url
            self._answer = queue.pop(0) if len(queue) > 1 else queue[0]

        async def __aenter__(self):
            answer = self._answer(self._url) if callable(self._answer) else self._answer
            if isinstance(answer, BaseException):
                raise answer
            return _Response(*answer)

        async def __aexit__(self, *_exc):
            return False

    class _Session:
        def __init__(self, *_a, **_k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        def _send(self, method, url, kw):
            sent.append((method, url, dict(kw.get("headers") or {}), kw.get("allow_redirects", True)))
            return _Request(url)

        def post(self, url, **kw):
            return self._send("POST", url, kw)

        def get(self, url, **kw):
            return self._send("GET", url, kw)

    monkeypatch.setattr(aiohttp, "ClientSession", _Session)
    return sent


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


def test_an_error_that_quotes_the_api_key_anyway_has_it_taken_out_of_the_details(monkeypatch):
    """No URL carries the key, but an answer can still quote the request back (a proxy's page
    echoing it). The key is taken out of the details; the rest of the error's words are kept."""
    from personalclaw.sdk.image import ImageGenError

    _discovers(monkeypatch)
    _fake_gemini(monkeypatch, RuntimeError(f"proxy refused the request carrying {_KEY}"))
    with pytest.raises(ImageGenError) as ei:
        _image()
    assert _KEY not in str(ei.value)
    assert str(ei.value) == (
        "The image request to Gemini failed unexpectedly. Try again in a moment. Details: proxy "
        "refused the request carrying [REDACTED: credential]"
    )


# ── The key goes in the header, never a URL ──────────────────────────────────

_VIDEO_URI = f"{prov._NATIVE_BASE}files/vid-test:download?alt=media"
_IMAGE_MODEL = {"name": "models/gemini-image-test", "supportedGenerationMethods": ["generateContent"]}
_AN_IMAGE = {"candidates": [{"content": {"parts": [
    {"inlineData": {"mimeType": "image/png", "data": "aW1hZ2U="}},
]}}]}


def _finished(*uris):
    """A finished Veo operation whose samples are at ``uris``."""
    return {"done": True, "response": {"generateVideoResponse": {"generatedSamples": [
        {"video": {"uri": uri}} for uri in uris
    ]}}}


@pytest.fixture
def no_waiting(monkeypatch, tmp_path):
    """Polls don't sleep, discovery is not remembered between tests, and a fetched video is
    written under ``tmp_path``."""
    import tempfile

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(prov.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(prov, "_discovery_cache", {})
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return tmp_path


def test_every_native_call_sends_the_key_in_its_header_and_no_url_carries_it(no_waiting, monkeypatch):
    """Discovery, an image, and a video's submit, poll and download: row 464. Each used to put
    the key in its URL (``?key=…``), which an HTTP library's error, a traceback and a log line
    can each quote. None may follow a redirect with the key: that is the download's own call."""
    sent_for_image = _fake_gemini(monkeypatch, (200, {"models": [_IMAGE_MODEL]}), (200, _AN_IMAGE))
    [image] = _image()
    assert image.b64 == "aW1hZ2U="
    sent_for_video = _fake_gemini(
        monkeypatch,
        (200, {"name": "operations/op-test"}),
        (200, _finished(_VIDEO_URI)),
        (200, b"a fake mp4", {"Content-Type": "video/mp4"}),
    )
    [video] = _video()
    assert Path(video.local_path).read_bytes() == b"a fake mp4"
    assert (video.url, video.mime) == ("", "video/mp4")

    sent = sent_for_image + sent_for_video
    assert [(method, url) for method, url, _h, _r in sent] == [
        ("GET", f"{prov._NATIVE_BASE}models?pageSize=200"),
        ("POST", f"{prov._NATIVE_BASE}models/gemini-image-test:generateContent"),
        ("POST", f"{prov._NATIVE_BASE}models/veo-test:predictLongRunning"),
        ("GET", f"{prov._NATIVE_BASE}operations/op-test"),
        ("GET", _VIDEO_URI),
    ]
    for _method, url, headers, follows_redirects in sent:
        assert _KEY not in url
        assert headers["x-goog-api-key"] == _KEY
        assert follows_redirects is False


def test_no_log_line_carries_the_key(no_waiting, monkeypatch, caplog):
    """A failed discovery and a dropped poll are logged with their traceback, and an HTTP
    library's error quotes the URL it was given. With the key in that URL, it was in the log."""
    import logging

    def _quoting(url):
        return RuntimeError(f"Cannot connect to {url}")

    _fake_gemini(
        monkeypatch,
        _quoting,  # discovery
        (200, {"name": "operations/op-test"}),
        _quoting,  # a dropped poll, retried
        (200, _finished("https://video-store.example/vid.mp4")),
    )
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(prov.GeminiImageProvider(api_key=_KEY).list_models()) == []
        [video] = _video()
    assert "Cannot connect to https://generativelanguage.googleapis.com/v1beta/models" in caplog.text
    assert "Veo poll error" in caplog.text
    assert _KEY not in caplog.text
    assert video.url == "https://video-store.example/vid.mp4"


def test_a_redirect_on_geminis_host_is_followed_with_the_key(no_waiting, monkeypatch):
    sent = _fake_gemini(
        monkeypatch,
        (200, {"name": "operations/op-test"}),
        (200, _finished(_VIDEO_URI)),
        (302, b"", {"Location": "/v1beta/files/vid-test:download?alt=media&hop=2"}),
        (200, b"a fake mp4", {"Content-Type": "video/mp4"}),
    )
    [video] = _video()
    assert Path(video.local_path).read_bytes() == b"a fake mp4"
    assert sent[-1][1] == f"{prov._NATIVE_BASE}files/vid-test:download?alt=media&hop=2"
    assert sent[-1][2]["x-goog-api-key"] == _KEY


def test_a_redirect_off_geminis_host_is_handed_to_core_without_the_key(no_waiting, monkeypatch):
    """Gemini's download redirects (its own instructions follow one). The key is sent only to
    Gemini's host; where it redirects to is core's to fetch, through its egress guard."""
    signed = "https://video-store.example/signed/vid-test.mp4?sig=placeholder"
    sent = _fake_gemini(
        monkeypatch,
        (200, {"name": "operations/op-test"}),
        (200, _finished(_VIDEO_URI)),
        (302, b"", {"Location": signed}),
    )
    [video] = _video()
    assert (video.url, video.local_path) == (signed, "")
    assert [url for _m, url, _h, _r in sent][-1] == _VIDEO_URI  # nothing was sent to the other host


@pytest.mark.parametrize("uri", [
    "https://video-store.example/vid-test.mp4",
    # Named in the address, not its host: the old check searched the string for it.
    "https://video-store.example/?next=generativelanguage.googleapis.com",
    "http://generativelanguage.googleapis.com/v1beta/files/vid-test:download",
])
def test_a_video_elsewhere_is_handed_to_core_and_sent_no_key(no_waiting, monkeypatch, uri):
    sent = _fake_gemini(
        monkeypatch, (200, {"name": "operations/op-test"}), (200, _finished(uri)),
    )
    [video] = _video()
    assert (video.url, video.local_path) == (uri, "")
    assert _KEY not in video.url
    assert len(sent) == 2  # the submit and the poll: the app fetched nothing from ``uri``


def test_a_video_answered_inline_is_saved_as_a_file_core_can_read(no_waiting, monkeypatch):
    """It was handed over as a ``data:`` URL, which core's egress guard refuses to fetch, so the
    video never reached the user."""
    import base64

    inline = {"done": True, "response": {"predictions": [
        {"bytesBase64Encoded": base64.b64encode(b"a fake mp4").decode()},
    ]}}
    _fake_gemini(monkeypatch, (200, {"name": "operations/op-test"}), (200, inline))
    [video] = _video()
    assert video.url == ""
    assert Path(video.local_path).read_bytes() == b"a fake mp4"


@pytest.mark.parametrize(("status", "sentence"), [
    (403, "Gemini refused the API key for the Veo video download request (HTTP 403). Check "
          "Google Gemini API Key on this Google Gemini instance in Settings → Providers (or "
          "GEMINI_API_KEY), and that the key's Google Cloud project may use this model."),
    (404, "Gemini no longer has the video Veo made (HTTP 404), so it could not be saved. Try "
          "again."),
])
def test_a_failed_video_download_says_what_to_do(no_waiting, monkeypatch, status, sentence):
    from personalclaw.sdk.video import VideoGenError

    _fake_gemini(
        monkeypatch,
        (200, {"name": "operations/op-test"}),
        (200, _finished(_VIDEO_URI)),
        (status, {"error": {"message": "upstream detail"}}),
    )
    with pytest.raises(VideoGenError) as ei:
        _video()
    assert str(ei.value) == f"{sentence} Details: upstream detail"


def test_a_video_larger_than_the_cap_is_refused_and_leaves_no_file(no_waiting, monkeypatch):
    from personalclaw.sdk.video import VideoGenError

    monkeypatch.setattr(prov, "_VIDEO_MAX_BYTES", 8)
    _fake_gemini(
        monkeypatch,
        (200, {"name": "operations/op-test"}),
        (200, _finished(_VIDEO_URI)),
        (200, b"more than eight bytes", {"Content-Type": "video/mp4"}),
    )
    with pytest.raises(VideoGenError, match="so PersonalClaw did not save it"):
        _video()
    assert list(no_waiting.glob("gemini-video-*")) == []


# ── A poll that cannot succeed says so at once ───────────────────────────────


@pytest.mark.parametrize(("status", "sentence"), [
    (403, "Gemini refused the API key for the Veo video request (HTTP 403). Check Google Gemini "
          "API Key on this Google Gemini instance in Settings → Providers (or GEMINI_API_KEY), "
          "and that the key's Google Cloud project may use this model."),
    (404, "Gemini no longer knows the Veo video job PersonalClaw was waiting on (HTTP 404), so "
          "its video can't be fetched. Try again."),
])
def test_a_poll_that_cannot_succeed_says_so_at_once(no_waiting, monkeypatch, status, sentence):
    """It was retried for ten minutes and then reported as a timeout."""
    from personalclaw.sdk.video import VideoGenError

    sent = _fake_gemini(
        monkeypatch,
        (200, {"name": "operations/op-test"}),
        (status, {"error": {"message": "upstream detail"}}),
    )
    with pytest.raises(VideoGenError) as ei:
        _video()
    assert str(ei.value) == f"{sentence} Details: upstream detail"
    assert len(sent) == 2  # one poll


def test_a_poll_that_another_can_outlast_is_retried(no_waiting, monkeypatch):
    _fake_gemini(
        monkeypatch,
        (200, {"name": "operations/op-test"}),
        (503, {"error": {"message": "busy"}}),
        (429, {"error": {"message": "slow down"}}),
        (200, {"done": False}),
        (200, _finished(_VIDEO_URI)),
        (200, b"a fake mp4", {"Content-Type": "video/mp4"}),
    )
    [video] = _video()
    assert Path(video.local_path).read_bytes() == b"a fake mp4"


def test_a_job_that_never_finishes_says_so_with_the_last_polls_words(no_waiting, monkeypatch):
    from personalclaw.sdk.video import VideoGenError

    _fake_gemini(
        monkeypatch, (200, {"name": "operations/op-test"}), (503, {"error": {"message": "busy"}}),
    )
    with pytest.raises(VideoGenError) as ei:
        _video()
    assert str(ei.value) == (
        "Veo's video job did not finish within 10 minutes, so PersonalClaw stopped waiting and "
        "no video was saved. Try again later; if it keeps happening, choose another model in "
        "Settings → Models. Details: HTTP 503: busy"
    )


# ── A 200 that is not the JSON object Gemini sends (row 468) ─────────────────

_NOT_OBJECTS = [([], "a list"), ("null", "null"), ('"ok"', "a string"), ("7", "a number")]


@pytest.mark.parametrize(("body", "kind"), _NOT_OBJECTS)
def test_an_image_answer_that_is_not_an_object_says_it_could_not_be_read(monkeypatch, body, kind):
    """``data.get`` raised AttributeError, which reached the user as no sentence at all."""
    from personalclaw.sdk.image import ImageGenError

    _discovers(monkeypatch)
    _fake_gemini(monkeypatch, (200, body))
    with pytest.raises(ImageGenError) as ei:
        _image()
    assert str(ei.value) == (
        "Gemini's answer to the image request could not be read. Try again in a moment. "
        f"Details: the answer is JSON but {kind}, not an object"
    )


@pytest.mark.parametrize(("body", "kind"), _NOT_OBJECTS)
def test_an_imagen_answer_that_is_not_an_object_says_it_could_not_be_read(monkeypatch, body, kind):
    from personalclaw.sdk.image import ImageGenError

    _discovers(
        monkeypatch, {"name": "models/imagen-test", "supportedGenerationMethods": ["predict"]},
    )
    _fake_gemini(monkeypatch, (200, body))
    with pytest.raises(ImageGenError) as ei:
        _image(model="imagen-test")
    assert str(ei.value) == (
        "Gemini's answer to the Imagen request could not be read. Try again in a moment. "
        f"Details: the answer is JSON but {kind}, not an object"
    )


@pytest.mark.parametrize(("body", "kind"), _NOT_OBJECTS)
@pytest.mark.parametrize("stage", ["submit", "poll"])
def test_a_video_answer_that_is_not_an_object_says_it_could_not_be_read(
    no_waiting, monkeypatch, stage, body, kind,
):
    """The submit raised AttributeError; the poll swallowed it, polled for ten minutes and then
    said the job timed out."""
    from personalclaw.sdk.video import VideoGenError

    answers = [(200, body)] if stage == "submit" else [(200, {"name": "operations/op-test"}),
                                                        (200, body)]
    _fake_gemini(monkeypatch, *answers)
    with pytest.raises(VideoGenError) as ei:
        _video()
    assert str(ei.value) == (
        "Gemini's answer to the Veo video request could not be read. Try again in a moment. "
        f"Details: the answer is JSON but {kind}, not an object"
    )


@pytest.mark.parametrize("answer", [
    {"candidates": "none"},
    {"candidates": [{"content": {"parts": "none"}}]},
    {"candidates": [{"content": {"parts": [{"inlineData": "none"}]}}]},
    {"candidates": [7]},
])
def test_an_image_answer_whose_fields_are_the_wrong_shape_says_it_had_no_image(monkeypatch, answer):
    from personalclaw.sdk.image import ImageGenError

    _discovers(monkeypatch)
    _fake_gemini(monkeypatch, (200, answer))
    with pytest.raises(ImageGenError) as ei:
        _image()
    assert str(ei.value) == (
        "Gemini's answer to the image request had no image in it. Try again; if it happens "
        "again, change the prompt or choose another model in Settings → Models."
    )


@pytest.mark.parametrize("response", [
    "none",
    {"generateVideoResponse": "none"},
    {"generateVideoResponse": {"generatedSamples": [{"video": "none"}]}},
    {"predictions": [7, {"video": "none"}]},
])
def test_a_finished_job_whose_fields_are_the_wrong_shape_says_it_had_no_video(
    no_waiting, monkeypatch, response,
):
    from personalclaw.sdk.video import VideoGenError

    _fake_gemini(
        monkeypatch,
        (200, {"name": "operations/op-test"}),
        (200, {"done": True, "response": response}),
    )
    with pytest.raises(VideoGenError) as ei:
        _video()
    assert str(ei.value) == (
        "Veo's video job finished, but Gemini's answer had no video in it. Try again; if it "
        "happens again, change the prompt or choose another model in Settings → Models."
    )


@pytest.mark.parametrize("body", [[], {"models": "none"}, {"models": [7, "x"]}, "null"])
def test_a_model_list_that_is_not_one_lists_none(no_waiting, monkeypatch, body):
    _fake_gemini(monkeypatch, (200, body))
    assert asyncio.run(prov.GeminiImageProvider(api_key=_KEY).list_models()) == []
    assert asyncio.run(prov.GeminiVideoProvider(api_key=_KEY).list_models()) == []


def test_the_base_url_help_says_what_it_reaches(monkeypatch):
    """The Base URL moves chat, embeddings and their model list. Image, video and speech go to
    Google's own address whatever it says, and its help said nothing of that."""
    manifest = json.loads((Path(__file__).parent / "app.json").read_text())
    help_text = manifest["provider"]["settingsSchema"]["properties"]["endpoint"]["x-meta"]["help"]
    assert "Image, video and speech always use Google's own address." in help_text
    assert f"Empty uses {prov.SPEC.default_base_url}." in help_text

    entry = {"name": "google-inst", "type": "google",
             "options": {"api_key": _KEY, "endpoint": "https://gemini-proxy.example/v1"}}
    [image] = prov._scan_image([entry])
    _discovers(monkeypatch)
    sent = _fake_gemini(monkeypatch, (200, _AN_IMAGE))
    asyncio.run(image.generate("a heron", model="gemini-image-test"))
    assert sent[0][1].startswith(prov._NATIVE_BASE)
    chat = prov.create_provider({"api_key": "k", "endpoint": "https://gemini-proxy.example/v1"})
    assert chat._base_url == "https://gemini-proxy.example/v1"


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


_NO_KEY = (
    "No Gemini API key is set. Add it as Google Gemini API Key on this Google Gemini instance in "
    "Settings → Providers (or GEMINI_API_KEY)."
)


@pytest.mark.parametrize("adapter", [prov.GeminiImageProvider, prov.GeminiVideoProvider])
def test_an_adapter_without_a_key_says_why_it_is_unavailable(monkeypatch, adapter):
    """🔴 Red before: the SDK's default "" — Settings → Models left it out with nothing saying
    why."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert asyncio.run(adapter(api_key="").unavailable_reason()) == _NO_KEY
    assert asyncio.run(adapter(api_key=_KEY).unavailable_reason()) == ""


def test_a_call_without_a_key_names_where_to_set_it(monkeypatch):
    """It said only "set GEMINI_API_KEY", never the instance's own field."""
    from personalclaw.sdk.image import ImageGenError
    from personalclaw.sdk.video import VideoGenError

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ImageGenError, match=r"^No Gemini API key is set\."):
        asyncio.run(prov.GeminiImageProvider(api_key="").generate("a heron", model="gemini-image-test"))
    with pytest.raises(VideoGenError) as ei:
        asyncio.run(prov.GeminiVideoProvider(api_key="").generate("a heron", model="veo-test"))
    assert str(ei.value) == _NO_KEY
