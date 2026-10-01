"""Every model Image · Generation offers for Bedrock makes an image from a prompt, each called its
own way, and only where the instance's region serves it.

The adapter listed Amazon Nova Canvas whatever the region. AWS serves Nova Canvas in some regions
only, and an instance in a region that does not serve it was offered it, bound it, and was refused
at its first image, after the user had approved the call, with "ValidationException: The provided
model identifier is invalid" and a traceback at WARNING in the gateway log. Nothing else it offered
made an image from a prompt: Stability AI's text-to-image models were never listed, and their
request is not Nova Canvas's.

The endpoint below is a Bedrock on ``127.0.0.1`` that the app's real ``boto3`` reaches. Its listings
have the shape ListFoundationModels and ListInferenceProfiles answer with, for the models AWS's
"Regional availability by models" page names in each region. It holds each InvokeModel request to
the body AWS documents for its model (the Amazon Nova User Guide's "Request and response structure
for image generation" for Nova Canvas; the Amazon Bedrock User Guide's "Stability AI models" for
Stable Diffusion 3.5 Large, Stable Image Core and Stable Image Ultra), refuses a model the region
does not serve as Bedrock does ("The provided model identifier is invalid."), refuses a
profile-only model called by its own id, and answers in the documented shape. No AWS account is
reached.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlsplit

import pytest

import provider as prov  # app-local (loaded from the app dir)
from personalclaw.sdk.image import ImageGenError

PROMPT = "a heron at dawn on a misty lake"

#: A 1×1 PNG, the image every model here answers with.
PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5E"
    "rkJggg=="
)

NOVA_CANVAS = "amazon.nova-canvas-v1:0"
SD35_LARGE = "stability.sd3-5-large-v1:0"
IMAGE_CORE = "stability.stable-image-core-v1:1"
IMAGE_ULTRA = "stability.stable-image-ultra-v1:1"

#: Stability AI's Image Services: each changes an image it is given, and is called through a US
#: inference profile.
IMAGE_SERVICES = [
    ("stability.stable-conservative-upscale-v1:0", "Stable Image Conservative Upscale"),
    ("stability.stable-creative-upscale-v1:0", "Stable Image Creative Upscale"),
    ("stability.stable-fast-upscale-v1:0", "Stable Image Fast Upscale"),
    ("stability.stable-image-control-sketch-v1:0", "Stable Image Control Sketch"),
    ("stability.stable-image-control-structure-v1:0", "Stable Image Control Structure"),
    ("stability.stable-image-erase-object-v1:0", "Stable Image Erase Object"),
    ("stability.stable-image-inpaint-v1:0", "Stable Image Inpaint"),
    ("stability.stable-image-remove-background-v1:0", "Stable Image Remove Background"),
    ("stability.stable-image-search-recolor-v1:0", "Stable Image Search and Recolor"),
    ("stability.stable-image-search-replace-v1:0", "Stable Image Search and Replace"),
    ("stability.stable-image-style-guide-v1:0", "Stable Image Style Guide"),
    ("stability.stable-style-transfer-v1:0", "Stable Image Style Transfer"),
    ("stability.stable-outpaint-v1:0", "Stable Image Outpaint"),
]


def _record(model_id, name, maker, inputs, outputs, *, invoked, streams=False, status="ACTIVE"):
    """One ListFoundationModels ``modelSummaries`` entry."""
    return {
        "modelArn": f"arn:aws:bedrock:us-west-2::foundation-model/{model_id}",
        "modelId": model_id,
        "modelName": name,
        "providerName": maker,
        "inputModalities": list(inputs),
        "outputModalities": list(outputs),
        "responseStreamingSupported": streams,
        "customizationsSupported": [],
        "inferenceTypesSupported": list(invoked),
        "modelLifecycle": {"status": status},
    }


def _profile(profile_id, name, model_id, regions=("us-east-1", "us-east-2", "us-west-2")):
    """One ListInferenceProfiles ``inferenceProfileSummaries`` entry."""
    return {
        "inferenceProfileName": name,
        "inferenceProfileArn": (
            f"arn:aws:bedrock:us-west-2:111122223333:inference-profile/{profile_id}"
        ),
        "inferenceProfileId": profile_id,
        "models": [
            {"modelArn": f"arn:aws:bedrock:{region}::foundation-model/{model_id}"}
            for region in regions
        ],
        "status": "ACTIVE",
        "type": "SYSTEM_DEFINED",
    }


CLAUDE = "anthropic.claude-sonnet-4-5-20250929-v1:0"

_SERVICES = [
    _record(model_id, name, "Stability AI", ["TEXT", "IMAGE"], ["IMAGE"],
            invoked=["INFERENCE_PROFILE"])
    for model_id, name in IMAGE_SERVICES
]
_SERVICE_PROFILES = [
    _profile(f"us.{model_id}", f"US {name}", model_id) for model_id, name in IMAGE_SERVICES
]
_CHAT = [
    _record(CLAUDE, "Claude Sonnet 4.5", "Anthropic", ["TEXT", "IMAGE"], ["TEXT"],
            invoked=["INFERENCE_PROFILE"], streams=True),
    _record("amazon.nova-pro-v1:0", "Nova Pro", "Amazon", ["TEXT", "IMAGE", "VIDEO"], ["TEXT"],
            invoked=["ON_DEMAND", "INFERENCE_PROFILE"], streams=True),
    _record("amazon.titan-embed-text-v2:0", "Titan Text Embeddings V2", "Amazon", ["TEXT"],
            ["EMBEDDING"], invoked=["ON_DEMAND"]),
]
_CHAT_PROFILES = [_profile(f"us.{CLAUDE}", "US Anthropic Claude Sonnet 4.5", CLAUDE)]

#: What each region lists, as ``(modelSummaries, inferenceProfileSummaries)``. us-west-2 serves
#: Stability AI's three text-to-image models in the region and not Nova Canvas; us-east-2 serves
#: only the Image Services; the third region serves Nova Canvas and Nova Reel in the region.
LISTINGS: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {
    "us-west-2": (
        [
            *_CHAT,
            _record(SD35_LARGE, "Stable Diffusion 3.5 Large", "Stability AI", ["TEXT", "IMAGE"],
                    ["IMAGE"], invoked=["ON_DEMAND"]),
            _record(IMAGE_CORE, "Stable Image Core v1.1", "Stability AI", ["TEXT"], ["IMAGE"],
                    invoked=["ON_DEMAND"]),
            _record(IMAGE_ULTRA, "Stable Image Ultra v1.1", "Stability AI", ["TEXT", "IMAGE"],
                    ["IMAGE"], invoked=["ON_DEMAND"]),
            *_SERVICES,
        ],
        [*_CHAT_PROFILES, *_SERVICE_PROFILES],
    ),
    "us-east-2": ([*_CHAT, *_SERVICES], [*_CHAT_PROFILES, *_SERVICE_PROFILES]),
    "eu-west-1": (
        [
            *_CHAT,
            _record(NOVA_CANVAS, "Nova Canvas", "Amazon", ["TEXT", "IMAGE"], ["IMAGE"],
                    invoked=["ON_DEMAND"]),
            _record("amazon.nova-reel-v1:0", "Nova Reel", "Amazon", ["TEXT", "IMAGE"], ["VIDEO"],
                    invoked=["ON_DEMAND"]),
        ],
        list(_CHAT_PROFILES),
    ),
}

_STABILITY_RATIOS = {"16:9", "1:1", "21:9", "2:3", "3:2", "4:5", "5:4", "9:16", "9:21"}


def _base(model: str) -> str:
    """The foundation model an id names: an inference profile's is the model after its geography."""
    head, _, rest = model.partition(".")
    return rest if head in {"us", "eu", "apac", "global"} else model


def _extra(body: dict[str, Any], allowed: set[str]) -> int:
    return len(set(body) - allowed)


def _nova_canvas_violations(body: dict[str, Any]) -> int:
    """The ``TEXT_IMAGE`` request: a 1-1024 character prompt, one to five images, each side 320 to
    4,096 pixels and a multiple of 16, an aspect ratio between 1:4 and 4:1, at most 4,194,304
    pixels in all."""
    params = body.get("textToImageParams")
    config = body.get("imageGenerationConfig", {})
    if not isinstance(params, dict) or not isinstance(config, dict):
        return 1
    text = params.get("text")
    width, height = config.get("width", 1024), config.get("height", 1024)
    return (
        _extra(body, {"taskType", "textToImageParams", "imageGenerationConfig"})
        + (body.get("taskType") != "TEXT_IMAGE")
        + _extra(params, {"text", "negativeText", "style", "conditionImage", "controlMode",
                          "controlStrength"})
        + (not isinstance(text, str) or not 1 <= len(text) <= 1024)
        + _extra(config, {"width", "height", "quality", "cfgScale", "seed", "numberOfImages"})
        + (config.get("numberOfImages", 1) not in range(1, 6))
        + sum(
            not isinstance(side, int) or not 320 <= side <= 4096 or side % 16
            for side in (width, height)
        )
        + (not 0.25 <= width / height <= 4 or width * height > 4_194_304)
    )


def _stability_violations(model: str, body: dict[str, Any]) -> int:
    """Stability AI's text-to-image request: a prompt of at most 10,000 characters, one of the nine
    aspect ratios, an output format the model writes; Stable Diffusion 3.5 Large's ``mode`` too."""
    allowed = {"prompt", "aspect_ratio", "output_format", "seed", "negative_prompt"}
    formats = {"jpeg", "png", "webp"}
    if model == SD35_LARGE:
        allowed |= {"mode", "image", "strength"}
    if model == IMAGE_CORE:
        formats = {"jpeg", "png"}
    prompt = body.get("prompt")
    return (
        _extra(body, allowed)
        + (not isinstance(prompt, str) or len(prompt) > 10_000)
        + (body.get("aspect_ratio", "1:1") not in _STABILITY_RATIOS)
        + (body.get("output_format", "png") not in formats)
        + (body.get("mode", "text-to-image") != "text-to-image")
    )


def _violations(model: str, body: Any) -> int | str:
    """How ``body`` breaks the request AWS documents for ``model``: a count of schema violations,
    or, for an Image Service, the missing key it names."""
    if not isinstance(body, dict):
        return 1
    base = _base(model)
    if base == NOVA_CANVAS:
        return _nova_canvas_violations(body)
    if base in {SD35_LARGE, IMAGE_CORE, IMAGE_ULTRA}:
        return _stability_violations(base, body)
    return "" if "image" in body else "#: required key [image] not found"


class _Bedrock:
    """Bedrock's runtime and control plane on ``127.0.0.1``: lists :data:`LISTINGS` for the region
    a request is signed for, and answers each ``InvokeModel`` as its model does. ``invoked`` is
    ``(region, model, body)`` for each call, ``refused`` each refusal's message."""

    def __init__(self) -> None:
        self.listings = dict(LISTINGS)
        self.invoked: list[tuple[str, str, Any]] = []
        self.refused: list[str] = []
        #: Every request path sent to the runtime, whatever it asked for.
        self.posted: list[str] = []
        #: An answer to send in place of the documented one, for the next call.
        self.next_answer: dict[str, Any] | None = None
        #: An error to answer the next call with instead, as ``(status, type, message)``.
        self.next_error: tuple[int, str, str] | None = None
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(self))
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def callable_ids(self, region: str) -> dict[str, dict[str, Any]]:
        """Every id a call in ``region`` can name, with the record of the model that answers it."""
        records, profiles = self.listings.get(region, ([], []))
        by_id = {r["modelId"]: r for r in records}
        ids = {r["modelId"]: r for r in records if "ON_DEMAND" in r["inferenceTypesSupported"]}
        for p in profiles:
            routed = p["models"][0]["modelArn"].rpartition("/")[2]
            if routed in by_id:
                ids[p["inferenceProfileId"]] = by_id[routed]
        return ids

    def __enter__(self) -> "_Bedrock":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


_SIGNED_FOR = re.compile(r"Credential=[^/]+/\d{8}/(?P<region>[a-z0-9-]+)/")


def _handler_for(bedrock: _Bedrock) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:
            pass

        def _region(self) -> str:
            found = _SIGNED_FOR.search(self.headers.get("Authorization") or "")
            return found["region"] if found else ""

        def _reply(self, status: int, payload: dict[str, Any], error: str = "") -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if error:
                self.send_header("x-amzn-ErrorType", error)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)

        def _refuse(self, message: str) -> None:
            bedrock.refused.append(message)
            self._reply(400, {"message": message}, "ValidationException")

        def do_GET(self) -> None:  # noqa: N802 — the http.server hook name
            records, profiles = bedrock.listings.get(self._region(), ([], []))
            path = urlsplit(self.path).path.rstrip("/")
            if path.endswith("/foundation-models"):
                self._reply(200, {"modelSummaries": records})
            elif path.endswith("/inference-profiles"):
                self._reply(200, {"inferenceProfileSummaries": profiles})
            else:
                self._reply(404, {"message": "not found"}, "ResourceNotFoundException")

        def do_POST(self) -> None:  # noqa: N802
            parts = urlsplit(self.path).path.split("/")
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            bedrock.posted.append(urlsplit(self.path).path)
            if len(parts) != 4 or parts[1] != "model" or parts[3] != "invoke":
                self._reply(404, {"message": "not found"}, "ResourceNotFoundException")
                return
            region, model = self._region(), unquote(parts[2])
            try:
                body: Any = json.loads(raw or b"{}")
            except ValueError:
                body = None
            bedrock.invoked.append((region, model, body))
            error, bedrock.next_error = bedrock.next_error, None
            if error is not None:
                self._reply(error[0], {"message": error[2]}, error[1])
                return
            callable_ids = bedrock.callable_ids(region)
            records = {r["modelId"]: r for r in bedrock.listings.get(region, ([], []))[0]}
            if model not in callable_ids:
                if model in records:
                    self._refuse(
                        f"Invocation of model ID {model} with on-demand throughput isn’t "
                        "supported. Retry your request with the ID or ARN of an inference profile "
                        "that contains this model."
                    )
                else:
                    self._refuse("The provided model identifier is invalid.")
                return
            broken = _violations(model, body)
            if isinstance(broken, str) and broken:
                self._refuse(
                    f"Malformed input request: {broken}, please reformat your input and try again."
                )
                return
            if broken:
                self._refuse(
                    f"Malformed input request: {broken} schema "
                    f"violation{'s' if broken > 1 else ''} found, please reformat your input and "
                    "try again."
                )
                return
            answer, bedrock.next_answer = bedrock.next_answer, None
            if answer is None:
                if _base(model) == NOVA_CANVAS:
                    count = body.get("imageGenerationConfig", {}).get("numberOfImages", 1)
                    answer = {"images": [PNG] * count}
                else:
                    answer = {"seeds": [2130420379], "finish_reasons": [None], "images": [PNG]}
            self._reply(200, answer)

    return _Handler


#: The AWS SDK's own packages. A test here imports them, so it takes them out again: importing
#: the provider module must not import boto3 (``test_provider``), and a later test proves it.
_AWS_SDK = {"boto3", "botocore", "s3transfer"}


@pytest.fixture
def bedrock(monkeypatch):
    """The app's real boto3 reaches the loopback Bedrock and nothing else: both endpoints are
    overridden, the credentials are dummies, and no AWS file on this machine is read."""
    loaded = {name for name in sys.modules if name.split(".")[0] in _AWS_SDK}
    with _Bedrock() as endpoint:
        monkeypatch.setenv("AWS_ENDPOINT_URL_BEDROCK_RUNTIME", endpoint.url)
        monkeypatch.setenv("AWS_ENDPOINT_URL_BEDROCK", endpoint.url)
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "image-test")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "image-test")
        for name in ("AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
        monkeypatch.setattr(prov, "_WARNED_AT", {})
        monkeypatch.setattr(prov, "_cred_cache", {})
        prov._BEDROCK_CACHE.clear()
        yield endpoint
        prov._BEDROCK_CACHE.clear()
    for name in [n for n in sys.modules if n.split(".")[0] in _AWS_SDK and n not in loaded]:
        del sys.modules[name]


def _run(coro):
    return asyncio.run(coro)


def _adapter(region: str = "us-west-2") -> prov.BedrockImageProvider:
    return prov.BedrockImageProvider(region=region, name="my-bedrock")


def _offered(region: str = "us-west-2") -> list[str]:
    """The models Image · Generation offers for an instance in ``region``."""
    return [m.name for m in _run(_adapter(region).list_models())]


def _core_through_a_profile_only(bedrock: _Bedrock) -> None:
    """us-west-2 as a listing in which Stable Image Core is served only through an inference
    profile."""
    core = _record(IMAGE_CORE, "Stable Image Core v1.1", "Stability AI", ["TEXT"], ["IMAGE"],
                   invoked=["INFERENCE_PROFILE"])
    bedrock.listings["us-west-2"] = (
        [core], [_profile(f"us.{IMAGE_CORE}", "US Stable Image Core v1.1", IMAGE_CORE)]
    )


# ── only models that make an image from a prompt, and only where the region serves them ─────


def test_each_region_offers_the_models_it_serves_that_make_an_image_from_a_prompt(bedrock):
    """🔴 Red before: Nova Canvas in every region, us-west-2 and us-east-2 among them, which do not
    serve it. No model that only edits or upscales an image is offered, by its own id or its
    profile's: no binding here edits one."""
    services = {model_id for model_id, _ in IMAGE_SERVICES}
    services |= {f"us.{model_id}" for model_id in services}

    assert sorted(_offered("us-west-2")) == sorted([SD35_LARGE, IMAGE_CORE, IMAGE_ULTRA])
    assert _offered("us-east-2") == []
    assert _offered("eu-west-1") == [NOVA_CANVAS]
    assert not services & {m for region in LISTINGS for m in _offered(region)}


def test_every_model_offered_makes_an_image(bedrock):
    """🔴 Red before: the one model offered in us-west-2 was refused as an invalid identifier."""
    made = {}
    for region in LISTINGS:
        for model in _offered(region):
            images = _run(_adapter(region).generate(PROMPT, model=model))
            made[model] = [(image.b64, image.mime) for image in images]

    assert bedrock.refused == [], bedrock.refused
    assert made == dict.fromkeys(
        [SD35_LARGE, IMAGE_CORE, IMAGE_ULTRA, NOVA_CANVAS], [(PNG, "image/png")]
    )


def test_a_model_served_only_through_an_inference_profile_is_called_through_it(bedrock):
    """A model whose record offers no on-demand call is offered, and called, by the id of the
    profile that routes to it; its own id is never offered, since Bedrock refuses a call to it."""
    _core_through_a_profile_only(bedrock)

    assert _offered("us-west-2") == [f"us.{IMAGE_CORE}"]
    images = _run(_adapter().generate(PROMPT, model=f"us.{IMAGE_CORE}"))

    assert [image.b64 for image in images] == [PNG]
    assert [model for _region, model, _body in bedrock.invoked] == [f"us.{IMAGE_CORE}"]


def test_a_retired_model_is_not_offered(bedrock):
    """A model the region lists as Legacy is not offered, as the chat and embedding lists leave
    it out."""
    records, profiles = bedrock.listings["eu-west-1"]
    bedrock.listings["eu-west-1"] = (
        [{**r, "modelLifecycle": {"status": "LEGACY"}} if r["modelId"] == NOVA_CANVAS else r
         for r in records],
        profiles,
    )

    assert _offered("eu-west-1") == []


# ── each model's own request ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("size", "ratio"),
    [("", "1:1"), ("1024x1024", "1:1"), ("1280x720", "16:9"), ("720x1280", "9:16"),
     ("2:3", "2:3"), ("a square", "1:1")],
)
def test_a_stability_model_is_asked_for_the_aspect_ratio_nearest_the_size(bedrock, size, ratio):
    """Stability AI's models take an aspect ratio, not a width and height: a size names the
    nearest one they make, and one they cannot read their default. 🔴 Red before: they were sent
    Nova Canvas's request."""
    _run(_adapter().generate(PROMPT, model=IMAGE_ULTRA, size=size))

    assert bedrock.refused == []
    assert bedrock.invoked[-1][2] == {
        "prompt": PROMPT, "aspect_ratio": ratio, "output_format": "png"
    }


def test_each_model_makes_as_many_images_as_asked_its_own_way(bedrock):
    """Nova Canvas makes up to five images in one request, at the size asked for; each Stability AI
    request makes one, so two images are two requests."""
    nova = _run(_adapter("eu-west-1").generate(PROMPT, model=NOVA_CANVAS, size="1280x720", n=2))
    stability = _run(_adapter().generate(PROMPT, model=SD35_LARGE, n=2))

    assert bedrock.refused == []
    assert [image.b64 for image in nova + stability] == [PNG] * 4
    assert [(model, body) for _region, model, body in bedrock.invoked] == [
        (
            NOVA_CANVAS,
            {
                "taskType": "TEXT_IMAGE",
                "textToImageParams": {"text": PROMPT},
                "imageGenerationConfig": {"numberOfImages": 2, "width": 1280, "height": 720},
            },
        ),
        (SD35_LARGE, {"prompt": PROMPT, "aspect_ratio": "1:1", "output_format": "png"}),
        (SD35_LARGE, {"prompt": PROMPT, "aspect_ratio": "1:1", "output_format": "png"}),
    ]


def test_a_filtered_image_says_so(bedrock):
    """A Stability AI answer whose finish reason is a filter holds no image."""
    bedrock.next_answer = {"finish_reasons": ["Filter reason: prompt"]}

    with pytest.raises(ImageGenError) as refused:
        _run(_adapter().generate(PROMPT, model=IMAGE_CORE))

    assert str(refused.value) == (
        f"{IMAGE_CORE}'s content filter stopped this image. Reword the prompt, then try again. "
        "Details: Filter reason: prompt"
    )


# ── a model that cannot make one, and a region with none ────────────────────────────────────


def test_a_model_that_only_edits_an_image_is_refused_before_anything_is_sent(bedrock):
    """A binding to an editing model (made before, or through the API) is refused with a sentence.
    🔴 Red before: it was sent Nova Canvas's request, and Bedrock refused it."""
    with pytest.raises(ImageGenError) as refused:
        _run(_adapter().generate(PROMPT, model="us.stability.stable-image-inpaint-v1:0"))

    assert bedrock.posted == [], "nothing was sent"
    assert str(refused.value) == (
        "Bedrock's us.stability.stable-image-inpaint-v1:0 is not a model this app can make an "
        "image from a prompt with, so nothing was sent. Choose one of the models Settings → "
        "Models lists under Image · Generation for this Amazon Bedrock instance."
    )


def test_a_region_with_no_image_model_says_so(bedrock):
    """What Image · Generation shows for the instance, under its name. 🔴 Red before: us-east-2
    offered Nova Canvas, which it does not serve, and said nothing."""
    adapter = _adapter("us-east-2")

    assert _run(adapter.list_models()) == []
    assert _run(adapter.is_available()) is False
    assert _run(adapter.unavailable_reason()) == (
        "No image generation model is available in us-east-2: Amazon Bedrock lists none there "
        "that this app can make an image from a prompt with (Amazon Nova Canvas, Stable Diffusion "
        "3.5 Large, Stable Image Core, Stable Image Ultra). Models that only edit or upscale an "
        "image are not offered. Set AWS Region on this Amazon Bedrock instance in Settings → "
        "Providers to a region that lists one of them, or add an instance for that region."
    )
    # A region that serves one has nothing to say.
    assert _run(_adapter("us-west-2").unavailable_reason()) == ""
    assert _run(_adapter("us-west-2").is_available()) is True


def test_a_listing_that_fails_is_the_rows_failure(bedrock, monkeypatch):
    """The listing's failure is what Image · Generation says, as the chat catalog says it; it is
    not read as a region that serves no image model. 🔴 Red before: the list named Nova Canvas
    whatever the listing said."""

    def _unanswered(region, profile):
        raise RuntimeError("the control plane did not answer")

    monkeypatch.setattr(prov, "_list_bedrock_models_sync", _unanswered)
    adapter = _adapter()

    assert _run(adapter.unavailable_reason()) == ""
    with pytest.raises(prov.ModelDiscoveryError) as failed:
        _run(adapter.list_models())
    assert str(failed.value).startswith("No model list came back from Amazon Bedrock in us-west-2.")


def test_an_edit_says_none_is_made(bedrock, tmp_path):
    """🔴 Red before: "Image editing is not supported by Amazon Bedrock Nova Canvas.", for
    whichever model was bound."""
    source = tmp_path / "heron.png"
    source.write_bytes(b"png")

    with pytest.raises(ImageGenError) as refused:
        _run(_adapter().edit(PROMPT, source_image=str(source), model=IMAGE_CORE))

    assert bedrock.posted == []
    assert str(refused.value) == (
        "This Amazon Bedrock instance makes new images from a prompt and edits none, so nothing "
        "was sent. Ask for a new image instead of an edit."
    )


# ── a failure at use time ────────────────────────────────────────────────────────────────────


def test_a_model_the_region_does_not_serve_says_so_in_one_line(bedrock, caplog):
    """The binding a us-west-2 instance was offered before: Nova Canvas. 🔴 Red before: "Bedrock
    image generation failed. Try again; if it keeps failing, check the gateway log", and a
    traceback under the WARNING."""
    caplog.set_level(logging.DEBUG, logger="bedrock_models")

    with pytest.raises(ImageGenError) as refused:
        _run(_adapter().generate(PROMPT, model=NOVA_CANVAS))

    said = str(refused.value)
    assert said.startswith(
        "Amazon Bedrock in us-west-2 has no model 'amazon.nova-canvas-v1:0' this AWS account can "
        "call. Choose one of the models Settings → Models lists for this Amazon Bedrock instance, "
        "or set AWS Region on this Amazon Bedrock instance in Settings → Providers to a region "
        "that serves it. Details: "
    ), said
    assert said.endswith("The provided model identifier is invalid."), "AWS's own words follow"
    logged = [r for r in caplog.records if r.name == "bedrock_models"]
    assert [(r.levelno, r.getMessage()) for r in logged] == [
        (logging.WARNING, f"Bedrock image generation on 'my-bedrock' failed: {said}")
    ]
    assert logged[0].exc_info is None


def test_a_model_called_by_its_own_id_that_takes_a_profile_says_so(bedrock):
    """🔴 Red before: "Bedrock image generation failed. Try again; …", with AWS's words after."""
    _core_through_a_profile_only(bedrock)

    with pytest.raises(ImageGenError) as refused:
        _run(_adapter().generate(PROMPT, model=IMAGE_CORE))

    assert str(refused.value).startswith(
        "Amazon Bedrock in us-west-2 serves 'stability.stable-image-core-v1:1' only through an "
        "inference profile. Choose the inference profile Settings → Models lists for it (its id "
        "starts with a geography, such as us. or global.). Details: "
    ), str(refused.value)


def test_a_failure_nothing_here_recognises_keeps_its_traceback_at_debug(bedrock, caplog):
    """The warning is the sentence alone, AWS's words included; the traceback the sentence sends
    the reader to the log for is at DEBUG. 🔴 Red before: the traceback was in the warning."""
    caplog.set_level(logging.DEBUG, logger="bedrock_models")
    bedrock.next_error = (424, "ModelErrorException", "The model could not process the request.")

    with pytest.raises(ImageGenError) as refused:
        _run(_adapter("eu-west-1").generate(PROMPT, model=NOVA_CANVAS))

    said = str(refused.value)
    assert said.startswith(
        "Bedrock image generation failed. Try again; if it keeps failing, check the gateway log. "
        "Details: An error occurred (ModelErrorException)"
    ), said
    warned = [
        r for r in caplog.records if r.name == "bedrock_models" and r.levelno == logging.WARNING
    ]
    assert [r.getMessage() for r in warned] == [
        f"Bedrock image generation on 'my-bedrock' failed: {said}"
    ]
    traced = [
        r.getMessage() for r in caplog.records
        if r.name == "bedrock_models" and r.levelno == logging.DEBUG
        and "Traceback" in r.getMessage()
    ]
    assert len(traced) == 1 and "ModelErrorException" in traced[0]


# ── video generation, the same way ───────────────────────────────────────────────────────────


def _video(region: str) -> prov.BedrockVideoProvider:
    return prov.BedrockVideoProvider(region=region, s3_bucket="clips", name="my-bedrock")


def test_video_generation_offers_nova_reel_only_where_the_region_serves_it(bedrock):
    """🔴 Red before: Nova Reel v1:1 whatever the region, and nothing said a region had none."""
    assert [m.name for m in _run(_video("eu-west-1").list_models())] == ["amazon.nova-reel-v1:0"]
    assert _run(_video("us-west-2").list_models()) == []
    assert _run(_video("us-west-2").is_available()) is False
    assert _run(_video("us-west-2").unavailable_reason()) == (
        "No video generation model is available in us-west-2: Amazon Bedrock lists no Amazon Nova "
        "Reel model there, the one this app makes videos with. Set AWS Region on this Amazon "
        "Bedrock instance in Settings → Providers to a region that lists it, or add an instance "
        "for that region."
    )


def test_a_video_on_a_model_that_makes_none_is_refused_before_anything_is_sent(bedrock):
    """🔴 Red before: the job was submitted for an image model."""
    from personalclaw.sdk.video import VideoGenError

    with pytest.raises(VideoGenError) as refused:
        _run(_video("us-west-2").generate(PROMPT, model=IMAGE_CORE))

    assert bedrock.posted == [], "nothing was sent"
    assert str(refused.value) == (
        "Bedrock's stability.stable-image-core-v1:1 is not a model this app can make a video with, "
        "so nothing was sent. Choose one of the models Settings → Models lists under Video · "
        "Generation for this Amazon Bedrock instance."
    )
