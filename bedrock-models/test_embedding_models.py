"""Every embedding model Settings → Models offers for Bedrock embeds, each called its own way.

Each Bedrock embedding model takes a request body of its own through ``InvokeModel`` and answers
in a shape of its own. The app sent one body for every id that was not Cohere's (Titan Text
Embeddings V2's ``dimensions`` and ``normalize``, which only that model takes) and read every
Cohere answer as Embed v4's, so four of the six models a region offered failed on their first
embedding: three with "Malformed input request: 2 schema violations found", and Embed English
with "'list' object has no attribute 'get'". The re-index card said so, honestly, and the model
stayed bound.

The endpoint below is a Bedrock on ``127.0.0.1`` that the app's real ``boto3`` reaches. It holds
each model's request to the schema AWS documents for it (Amazon Bedrock User Guide, "Inference
request parameters and response fields for foundation models": Amazon Titan Embeddings G1 - Text
and Titan Text Embeddings V2, Amazon Titan Multimodal Embeddings G1, Cohere Embed v3 and v4; the
Amazon Nova User Guide's "Complete embeddings request and response schema"), refuses a body that
breaks it the way Bedrock does, and answers in the documented shape. No AWS account is reached.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # the repo root: apps_testkit

import provider as prov  # app-local (loaded from the app dir)  # noqa: E402
from apps_testkit.egress import HOST, owner_egress  # noqa: E402

TEXT = "a heron waits by the lake"

#: The embedding models ``ListFoundationModels`` answers with, as ``(id, name, maker, reads,
#: on demand)``: the six us-west-2 lists for on-demand use, Amazon Nova Multimodal Embeddings, which
#: us-east-1 lists too, and two that are invoked only through an inference profile.
_LISTED = [
    ("cohere.embed-english-v3", "Embed English", "Cohere", ["TEXT"], True),
    ("cohere.embed-multilingual-v3", "Embed Multilingual", "Cohere", ["TEXT"], True),
    ("amazon.titan-embed-text-v1", "Titan Embeddings G1 - Text", "Amazon", ["TEXT"], True),
    (
        "amazon.titan-embed-image-v1",
        "Titan Multimodal Embeddings G1",
        "Amazon",
        ["TEXT", "IMAGE"],
        True,
    ),
    ("amazon.titan-embed-g1-text-02", "Titan Text Embeddings v2", "Amazon", ["TEXT"], True),
    ("amazon.titan-embed-text-v2:0", "Titan Text Embeddings V2", "Amazon", ["TEXT"], True),
    (
        "amazon.nova-2-multimodal-embeddings-v1:0",
        "Nova Multimodal Embeddings",
        "Amazon",
        ["TEXT", "IMAGE", "AUDIO", "VIDEO"],
        True,
    ),
    ("cohere.embed-v4:0", "Embed v4", "Cohere", ["TEXT", "IMAGE"], False),
    (
        "twelvelabs.marengo-embed-2-7-v1:0",
        "Marengo Embed 2.7",
        "TwelveLabs",
        ["TEXT", "IMAGE", "VIDEO", "AUDIO"],
        False,
    ),
]

#: The inference profiles ``ListInferenceProfiles`` answers with, as ``(id, name, model)``.
_PROFILES = [
    ("us.cohere.embed-v4:0", "US Cohere Embed v4", "cohere.embed-v4:0"),
    ("global.cohere.embed-v4:0", "Global Cohere Embed v4", "cohere.embed-v4:0"),
    (
        "us.twelvelabs.marengo-embed-2-7-v1:0",
        "US TwelveLabs Marengo Embed 2.7",
        "twelvelabs.marengo-embed-2-7-v1:0",
    ),
]

#: The models whose request and answer AWS documents, so the app can call them: Titan Text
#: Embeddings ``g1-text-02`` has no documented request body, and Marengo is not called through
#: ``InvokeModel`` with a text.
_CALLABLE = {
    "cohere.embed-english-v3",
    "cohere.embed-multilingual-v3",
    "amazon.titan-embed-text-v1",
    "amazon.titan-embed-image-v1",
    "amazon.titan-embed-text-v2:0",
    "amazon.nova-2-multimodal-embeddings-v1:0",
    "us.cohere.embed-v4:0",
    "global.cohere.embed-v4:0",
}

_COHERE_INPUT_TYPES = {"search_document", "search_query", "classification", "clustering"}


def _base(model: str) -> str:
    """The foundation model an id names: an inference profile's (``us.cohere.embed-v4:0``) is
    the model after its geography."""
    bare = model.split(":", 1)[0]
    head, _, rest = bare.partition(".")
    return rest if head in {"us", "eu", "apac", "global"} else bare


def _extra(body: dict[str, Any], allowed: set[str]) -> int:
    return len(set(body) - allowed)


def _violations(model: str, body: Any) -> int:
    """How many ways ``body`` breaks the request schema AWS documents for ``model``."""
    if not isinstance(body, dict):
        return 1
    base = _base(model)
    if base == "amazon.titan-embed-text-v1":
        return _extra(body, {"inputText"}) + (not isinstance(body.get("inputText"), str))
    if base == "amazon.titan-embed-text-v2":
        text = body.get("inputText")
        return (
            _extra(body, {"inputText", "dimensions", "normalize", "embeddingTypes"})
            + (not isinstance(text, str) or not 0 < len(text) <= 50_000)
            + (body.get("dimensions", 1024) not in (256, 512, 1024))
        )
    if base == "amazon.titan-embed-image-v1":
        config = body.get("embeddingConfig", {})
        return (
            _extra(body, {"inputText", "inputImage", "embeddingConfig"})
            + ("inputText" not in body and "inputImage" not in body)
            + (not isinstance(config, dict) or _extra(config, {"outputEmbeddingLength"}))
            + (config.get("outputEmbeddingLength", 1024) not in (256, 384, 1024))
        )
    if base in {"cohere.embed-english-v3", "cohere.embed-multilingual-v3"}:
        texts = body.get("texts")
        return (
            _extra(body, {"input_type", "texts", "images", "truncate", "embedding_types"})
            + (body.get("input_type") not in _COHERE_INPUT_TYPES)
            + (not isinstance(texts, list))
            + sum(len(t) > 2048 for t in texts or [])
        )
    if base == "cohere.embed-v4":
        return _extra(
            body,
            {"input_type", "texts", "images", "inputs", "embedding_types", "output_dimension",
             "max_tokens", "truncate"},
        ) + (body.get("input_type") not in _COHERE_INPUT_TYPES)
    if base == "amazon.nova-2-multimodal-embeddings-v1":
        params = body.get("singleEmbeddingParams")
        if not isinstance(params, dict):
            return 1 + _extra(body, {"schemaVersion", "taskType", "singleEmbeddingParams"})
        text = params.get("text") or {}
        return (
            _extra(body, {"schemaVersion", "taskType", "singleEmbeddingParams"})
            + (body.get("taskType") != "SINGLE_EMBEDDING")
            + ("embeddingPurpose" not in params)
            + (params.get("embeddingDimension", 3072) not in (256, 384, 1024, 3072))
            + (text.get("truncationMode") not in ("START", "END", "NONE"))
            + (not isinstance(text.get("value"), str) or len(text["value"]) > 8192)
        )
    return 1  # a model this endpoint does not serve


def _vector(width: int) -> list[float]:
    return [round(0.001 * i, 3) for i in range(width)]


def _answer(model: str, body: dict[str, Any], *, cohere_by_type: bool) -> dict[str, Any]:
    """What ``model`` answers ``body`` with, in the documented shape."""
    base = _base(model)
    if base == "amazon.titan-embed-text-v1":
        return {"embedding": _vector(1536), "inputTextTokenCount": 6}
    if base == "amazon.titan-embed-text-v2":
        vector = _vector(body.get("dimensions", 1024))
        return {"embedding": vector, "inputTextTokenCount": 6, "embeddingsByType": {"float": vector}}
    if base == "amazon.titan-embed-image-v1":
        width = body.get("embeddingConfig", {}).get("outputEmbeddingLength", 1024)
        return {"embedding": _vector(width), "inputTextTokenCount": 6, "message": None}
    if base.startswith("cohere."):
        vector = _vector(1536 if base == "cohere.embed-v4" else 1024)
        by_type = cohere_by_type or "embedding_types" in body
        return {
            "id": "a5e6f1c0-embed",
            "embeddings": {"float": [vector]} if by_type else [vector],
            "response_type": "embeddings_by_type" if by_type else "embeddings_floats",
            "texts": body["texts"],
        }
    width = body["singleEmbeddingParams"].get("embeddingDimension", 3072)
    return {"embeddings": [{"embeddingType": "TEXT", "embedding": _vector(width)}]}


class _Bedrock:
    """Bedrock's runtime and control plane on ``127.0.0.1``: lists :data:`_LISTED` and
    :data:`_PROFILES`, and answers each ``InvokeModel`` as its model does. ``invoked`` is
    ``(model, body)`` for each call; ``answered`` the vector each accepted call answered."""

    def __init__(self) -> None:
        self.invoked: list[tuple[str, Any]] = []
        self.answered: list[list[float]] = []
        self.refused: list[str] = []
        self.cohere_by_type = False
        #: An answer to send in place of the documented one, for the next call.
        self.next_answer: dict[str, Any] | None = None
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(self))
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "_Bedrock":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def _handler_for(bedrock: _Bedrock) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:
            pass

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

        def do_GET(self) -> None:  # noqa: N802 — the http.server hook name
            url = urlsplit(self.path)
            if url.path.rstrip("/").endswith("/foundation-models"):
                modality = parse_qs(url.query).get("byOutputModality", [""])[0]
                rows = [
                    {
                        "modelId": model,
                        "modelName": name,
                        "providerName": maker,
                        "inputModalities": inputs,
                        "outputModalities": ["EMBEDDING"],
                        "responseStreamingSupported": False,
                        "inferenceTypesSupported": [
                            "ON_DEMAND" if on_demand else "INFERENCE_PROFILE"
                        ],
                        "modelLifecycle": {"status": "ACTIVE"},
                    }
                    for model, name, maker, inputs, on_demand in _LISTED
                ] if modality in ("", "EMBEDDING") else []
                self._reply(200, {"modelSummaries": rows})
            elif url.path.rstrip("/").endswith("/inference-profiles"):
                profiles = [
                    {
                        "inferenceProfileId": profile,
                        "inferenceProfileName": name,
                        "status": "ACTIVE",
                        "type": "SYSTEM_DEFINED",
                        "models": [
                            {"modelArn": f"arn:aws:bedrock:{region}::foundation-model/{model}"}
                            for region in ("us-west-2", "us-east-1")
                        ],
                    }
                    for profile, name, model in _PROFILES
                ]
                self._reply(200, {"inferenceProfileSummaries": profiles})
            else:
                self._reply(404, {"message": "not found"}, "ResourceNotFoundException")

        def do_POST(self) -> None:  # noqa: N802
            parts = urlsplit(self.path).path.split("/")
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if len(parts) != 4 or parts[1] != "model" or parts[3] != "invoke":
                self._reply(404, {"message": "not found"}, "ResourceNotFoundException")
                return
            model = unquote(parts[2])
            try:
                body: Any = json.loads(raw or b"{}")
            except ValueError:
                body = None
            bedrock.invoked.append((model, body))
            broken = _violations(model, body)
            if broken:
                bedrock.refused.append(model)
                self._reply(
                    400,
                    {
                        "message": f"Malformed input request: {broken} schema violation"
                        f"{'s' if broken > 1 else ''} found, please reformat your input and try "
                        "again."
                    },
                    "ValidationException",
                )
                return
            answer, bedrock.next_answer = bedrock.next_answer, None
            if answer is None:
                answer = _answer(model, body, cohere_by_type=bedrock.cohere_by_type)
                vectors = answer.get("embedding") or answer.get("embeddings")
                if isinstance(vectors, dict):
                    vectors = vectors["float"]
                if vectors and isinstance(vectors[0], dict):
                    vectors = [vectors[0]["embedding"]]
                bedrock.answered.append(vectors if isinstance(vectors[0], float) else vectors[0])
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
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "embedding-test")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "embedding-test")
        # The endpoint is on this computer, so its owner allows its host in Settings → Security →
        # Network egress, as she would for her own; every request is asked of the guard.
        owner_egress(allow_hosts=[HOST])
        for name in ("AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
        monkeypatch.setattr(prov, "_WARNED_AT", {})
        prov._BEDROCK_CACHE.clear()
        yield endpoint
        prov._BEDROCK_CACHE.clear()
    for name in [n for n in sys.modules if n.split(".")[0] in _AWS_SDK and n not in loaded]:
        del sys.modules[name]


def _run(coro):
    return asyncio.run(coro)


def _offered(region: str = "us-west-2") -> list[Any]:
    """The models Settings → Models offers for Embedding on an instance in ``region``."""
    models = _run(prov.create_catalog({"region": region}).list_models())
    return [m for m in models if "embedding" in m.capabilities]


def _adapter() -> prov.BedrockEmbeddingProvider:
    return prov.BedrockEmbeddingProvider(region="us-west-2", name="my-bedrock")


# ── every model offered embeds ──────────────────────────────────────────────────────────────


def test_every_embedding_model_offered_embeds(bedrock):
    """🔴 Red before: Titan Embeddings G1 - Text, Titan Multimodal Embeddings G1 and Nova were
    sent Titan V2's body and refused; Embed English and Multilingual were read as Embed v4."""
    offered = [m.id for m in _offered()]
    assert offered, "the region's embedding models are listed"

    vectors = {model: _run(_adapter().embed(TEXT, model=model)) for model in offered}

    assert bedrock.refused == [], f"Bedrock refused the request for {bedrock.refused}"
    assert {model: bool(vector) for model, vector in vectors.items()} == dict.fromkeys(
        offered, True
    )
    assert list(vectors.values()) == bedrock.answered, "each vector is the one its model answered"


def test_a_model_whose_request_aws_does_not_document_is_not_offered(bedrock):
    """🔴 Red before: every on-demand embedding model was offered, whatever its body."""
    assert {m.id for m in _offered()} == _CALLABLE


def test_a_model_this_app_cannot_call_is_refused_before_anything_is_sent(bedrock):
    """A binding to a model the app has no request for (one saved through the API, or offered
    before) is refused with a sentence, and nothing reaches Bedrock. 🔴 Red before: it was sent
    Titan V2's body and Bedrock refused it."""
    adapter = _adapter()

    assert _run(adapter.embed(TEXT, model="amazon.titan-embed-g1-text-02")) is None
    assert _run(adapter.embed_batch([TEXT, TEXT], model="amazon.titan-embed-g1-text-02")) == [
        None,
        None,
    ]

    assert bedrock.invoked == [], "nothing was sent"
    reason = _run(adapter.unavailable_reason())
    assert "amazon.titan-embed-g1-text-02" in reason
    assert "Settings → Models" in reason


# ── each model's own request and answer ─────────────────────────────────────────────────────


@pytest.mark.parametrize("model", ["us.cohere.embed-v4:0", "global.cohere.embed-v4:0"])
@pytest.mark.parametrize("by_type", [False, True], ids=["embeddings_floats", "embeddings_by_type"])
def test_cohere_embed_v4_embeds_through_its_inference_profiles(bedrock, model, by_type):
    """Embed v4 is invoked through an inference profile, and answers in either of the two shapes
    Cohere documents. 🔴 Red before: a profile id is not ``cohere.``, so it was sent Titan's
    body."""
    bedrock.cohere_by_type = by_type

    vector = _run(_adapter().embed(TEXT, model=model))

    assert bedrock.refused == []
    assert vector and vector == bedrock.answered[-1]


@pytest.mark.parametrize("model", ["cohere.embed-english-v3", "cohere.embed-multilingual-v3"])
def test_cohere_embed_v3_reads_its_list_of_embeddings(bedrock, model):
    """🔴 Red before: "'list' object has no attribute 'get'"."""
    vector = _run(_adapter().embed(TEXT, model=model))

    assert vector and vector == bedrock.answered[-1]
    sent = bedrock.invoked[-1][1]
    assert sent["texts"] == [TEXT] and sent["input_type"] == "search_document"


@pytest.mark.parametrize(
    ("model", "limit"),
    [
        ("cohere.embed-english-v3", 2048),
        ("cohere.embed-multilingual-v3", 2048),
        ("amazon.nova-2-multimodal-embeddings-v1:0", 8192),
    ],
)
def test_a_text_longer_than_its_model_takes_is_embedded_from_its_start(bedrock, model, limit):
    """A model that refuses a text past a documented number of characters, and cuts one past its
    token limit at the end, is sent the text's first that many characters. 🔴 Red before:
    refused."""
    long_text = "the heron waited by the lake. " * (limit // 30 + 50)
    assert len(long_text) > limit

    vector = _run(_adapter().embed(long_text, model=model))

    assert bedrock.refused == []
    assert vector
    sent = json.dumps(bedrock.invoked[-1][1])
    assert long_text[:limit] in sent and long_text[: limit + 1] not in sent


def test_an_answer_with_no_embedding_says_why(bedrock):
    """Titan Multimodal Embeddings G1 reports a failure in its answer's ``message``. 🔴 Red
    before: answered None, and nothing said why."""
    bedrock.next_answer = {"inputTextTokenCount": 6, "message": "Unable to embed the input."}
    adapter = _adapter()

    assert _run(adapter.embed(TEXT, model="amazon.titan-embed-image-v1")) is None
    reason = _run(adapter.unavailable_reason())
    assert "Bedrock embedding failed" in reason
    assert "Unable to embed the input." in reason


# ── two models AWS names alike are told apart ───────────────────────────────────────────────


def test_a_name_is_told_apart_only_from_the_models_offered(bedrock):
    """Titan Text Embeddings V2 shares its name, case aside, with ``g1-text-02``, which is not
    offered, so it is shown as AWS names it."""
    names = {m.id: m.name for m in _offered()}

    assert names["amazon.titan-embed-text-v2:0"] == "Titan Text Embeddings V2 (Amazon)"
    assert len({name.casefold() for name in names.values()}) == len(names)


def test_two_models_aws_names_alike_are_told_apart(monkeypatch):
    """AWS named two embedding models "Titan Text Embeddings v2" and "Titan Text Embeddings V2",
    and Settings → Models showed both names. 🔴 Red before: the names were shown as AWS wrote
    them."""
    rows = [
        {"id": "amazon.titan-embed-text-v2:0", "name": "Titan Text Embeddings V2 (Amazon)",
         "capabilities": ["embedding"]},
        {"id": "amazon.titan-embed-text-v2:1", "name": "Titan Text Embeddings v2 (Amazon)",
         "capabilities": ["embedding"]},
        {"id": "cohere.embed-english-v3", "name": "Embed English (Cohere)",
         "capabilities": ["embedding"]},
    ]
    monkeypatch.setattr(prov, "_list_bedrock_models_sync", lambda region, profile: [*rows])
    prov._BEDROCK_CACHE.clear()
    try:
        names = {m.id: m.name for m in _offered("us-east-1")}
    finally:
        prov._BEDROCK_CACHE.clear()

    assert names == {
        "amazon.titan-embed-text-v2:0": "Titan Text Embeddings V2 (Amazon) · amazon.titan-embed-text-v2:0",
        "amazon.titan-embed-text-v2:1": "Titan Text Embeddings v2 (Amazon) · amazon.titan-embed-text-v2:1",
        "cohere.embed-english-v3": "Embed English (Cohere)",
    }
