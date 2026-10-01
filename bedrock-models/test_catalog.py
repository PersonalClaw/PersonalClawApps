"""BedrockCatalog model discovery is dynamic via the AWS control plane.

``BedrockCatalog.list_models`` queries ``bedrock.list_foundation_models`` (every
model's record) + ``list_inference_profiles`` (the cross-region ``us.*`` ids), using
the entry's region/profile, and offers each model for what its record says it does. There is no fallback catalog: when nothing
could be listed it raises ``ModelDiscoveryError`` naming why, which the connection
test reports as written. This logic moved out of core into the app during the
model-catalog-isolation slice; the test moved with it.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import provider as prov  # app-local (loaded from the app dir), registers on import


@pytest.fixture(autouse=True)
def _clear_bedrock_cache():
    """The discovery TTL cache is process-wide — clear it around each test."""
    prov._BEDROCK_CACHE.clear()
    yield
    prov._BEDROCK_CACHE.clear()


def _run(coro):
    return asyncio.run(coro)


async def _list(region="", profile=""):
    """Build a BedrockCatalog and return its models as plain dicts (id/name/caps)."""
    cat = prov.create_catalog({"region": region, "profile": profile})
    return [{"id": m.id, "name": m.name, "capabilities": m.capabilities} for m in await cat.list_models()]


def _fake_boto3(foundation, profiles, *, calls=None):
    """Build a fake ``boto3`` module whose bedrock client returns the given pages."""
    client = MagicMock()
    client.list_foundation_models.return_value = {"modelSummaries": foundation}
    client.list_inference_profiles.return_value = {"inferenceProfileSummaries": profiles}

    def _client(name, region_name=None):
        if calls is not None:
            calls["service"] = name
            calls["region"] = region_name
        return client

    session = MagicMock()
    session.client.side_effect = _client

    def _Session(profile_name=None):
        if calls is not None:
            calls["profile"] = profile_name
        return session

    return SimpleNamespace(Session=_Session)


# The records below have the shape Bedrock's ListFoundationModels and ListInferenceProfiles
# answer with. An inference profile's record names no modality: it names the model it routes to,
# by ARN, once per Region it routes to.


def _record(model_id, name, provider, inputs, outputs, *, streams, invoked, status="ACTIVE"):
    return {
        "modelArn": f"arn:aws:bedrock:us-east-1::foundation-model/{model_id}",
        "modelId": model_id,
        "modelName": name,
        "providerName": provider,
        "inputModalities": list(inputs),
        "outputModalities": list(outputs),
        "responseStreamingSupported": streams,
        "customizationsSupported": [],
        "inferenceTypesSupported": list(invoked),
        "modelLifecycle": {"status": status},
    }


def _routes(profile_id, name, model_id, regions=("us-east-1", "us-east-2", "us-west-2")):
    return {
        "inferenceProfileName": name,
        "inferenceProfileArn": f"arn:aws:bedrock:us-east-1:111122223333:inference-profile/{profile_id}",
        "inferenceProfileId": profile_id,
        "models": [
            {"modelArn": f"arn:aws:bedrock:{region}::foundation-model/{model_id}"} for region in regions
        ],
        "status": "ACTIVE",
        "type": "SYSTEM_DEFINED",
    }


CLAUDE = "anthropic.claude-sonnet-4-5-20250929-v1:0"
FOUNDATION_MODELS = [
    _record("amazon.nova-pro-v1:0", "Nova Pro", "Amazon", ["TEXT", "IMAGE", "VIDEO"], ["TEXT"],
            streams=True, invoked=["ON_DEMAND", "INFERENCE_PROFILE"]),
    _record(CLAUDE, "Claude Sonnet 4.5", "Anthropic", ["TEXT", "IMAGE"], ["TEXT"],
            streams=True, invoked=["INFERENCE_PROFILE"]),
    _record("mistral.voxtral-small-24b-2507", "Voxtral Small 24B 2507", "Mistral AI",
            ["SPEECH", "TEXT"], ["TEXT"], streams=True, invoked=["ON_DEMAND"]),
    _record("amazon.nova-sonic-v1:0", "Nova Sonic", "Amazon", ["SPEECH"], ["SPEECH", "TEXT"],
            streams=True, invoked=["ON_DEMAND"]),
    _record("amazon.rerank-v1:0", "Rerank 1.0", "Amazon", ["TEXT"], ["TEXT"],
            streams=False, invoked=["ON_DEMAND"]),
    _record("cohere.rerank-v3-5:0", "Rerank 3.5", "Cohere", ["TEXT"], ["TEXT"],
            streams=False, invoked=["ON_DEMAND"]),
    _record("amazon.titan-embed-text-v2:0", "Titan Text Embeddings V2", "Amazon", ["TEXT"],
            ["EMBEDDING"], streams=False, invoked=["ON_DEMAND"]),
    _record("cohere.embed-v4:0", "Embed v4", "Cohere", ["TEXT", "IMAGE"], ["EMBEDDING"],
            streams=False, invoked=["INFERENCE_PROFILE"]),
    _record("amazon.nova-canvas-v1:0", "Nova Canvas", "Amazon", ["TEXT", "IMAGE"], ["IMAGE"],
            streams=False, invoked=["ON_DEMAND"]),
    _record("stability.stable-image-inpaint-v1:0", "Stable Image Inpaint", "Stability AI",
            ["TEXT", "IMAGE"], ["IMAGE"], streams=False, invoked=["INFERENCE_PROFILE"]),
    _record("twelvelabs.pegasus-1-2-v1:0", "Pegasus v1.2", "TwelveLabs", ["TEXT", "VIDEO"],
            ["TEXT"], streams=True, invoked=["INFERENCE_PROFILE"]),
]
PROFILES = [
    _routes(f"us.{CLAUDE}", "US Anthropic Claude Sonnet 4.5", CLAUDE),
    _routes(f"global.{CLAUDE}", "Global Anthropic Claude Sonnet 4.5", CLAUDE),
    _routes("us.amazon.nova-pro-v1:0", "US Nova Pro", "amazon.nova-pro-v1:0"),
    _routes("us.cohere.embed-v4:0", "US Cohere Embed v4", "cohere.embed-v4:0"),
    _routes("global.cohere.embed-v4:0", "Global Cohere Embed v4", "cohere.embed-v4:0"),
    _routes("us.stability.stable-image-inpaint-v1:0", "US Stable Image Inpaint",
            "stability.stable-image-inpaint-v1:0"),
    _routes("us.twelvelabs.pegasus-1-2-v1:0", "US TwelveLabs Pegasus v1.2",
            "twelvelabs.pegasus-1-2-v1:0"),
    _routes("global.twelvelabs.pegasus-1-2-v1:0", "Global TwelveLabs Pegasus v1.2",
            "twelvelabs.pegasus-1-2-v1:0"),
]


def test_discovery_combines_foundation_and_profiles(monkeypatch):
    calls: dict = {}
    foundation = [
        _record("amazon.nova-pro-v1:0", "Nova Pro", "Amazon", ["TEXT", "IMAGE"], ["TEXT"],
                streams=True, invoked=["ON_DEMAND"]),
        # No ON_DEMAND → must NOT appear as a foundation model (only via profile).
        _record("anthropic.claude-sonnet-4-20250514-v1:0", "Claude Sonnet 4", "Anthropic",
                ["TEXT"], ["TEXT"], streams=True, invoked=["INFERENCE_PROFILE"]),
        # Legacy/withdrawn → skipped.
        _record("old.model-v1:0", "Old", "", ["TEXT"], ["TEXT"], streams=True,
                invoked=["ON_DEMAND"], status="LEGACY"),
    ]
    profiles = [
        _routes("us.anthropic.claude-sonnet-4-20250514-v1:0", "Claude Sonnet 4 (US)",
                "anthropic.claude-sonnet-4-20250514-v1:0"),
    ]
    monkeypatch.setitem(sys.modules, "boto3", _fake_boto3(foundation, profiles, calls=calls))

    models = _run(_list(region="us-west-2", profile="work"))
    ids = {m["id"] for m in models}

    assert "amazon.nova-pro-v1:0" in ids
    assert "us.anthropic.claude-sonnet-4-20250514-v1:0" in ids  # via inference profile
    assert "anthropic.claude-sonnet-4-20250514-v1:0" not in ids  # no ON_DEMAND, no base id
    assert "old.model-v1:0" not in ids  # LEGACY skipped
    # region/profile threaded through to boto3
    assert calls["region"] == "us-west-2"
    assert calls["profile"] == "work"
    assert calls["service"] == "bedrock"  # control plane, not bedrock-runtime
    # nova-pro has IMAGE input → image_modality capability
    nova = next(m for m in models if m["id"] == "amazon.nova-pro-v1:0")
    assert "image_modality" in nova["capabilities"]


def test_discovery_paginates_profiles(monkeypatch):
    client = MagicMock()
    client.list_foundation_models.return_value = {"modelSummaries": [
        _record(f"example.{name}", name, "Example", ["TEXT"], ["TEXT"], streams=True,
                invoked=["INFERENCE_PROFILE"])
        for name in ("a", "b")
    ]}
    client.list_inference_profiles.side_effect = [
        {"inferenceProfileSummaries": [_routes("us.a", "A", "example.a")], "nextToken": "t1"},
        {"inferenceProfileSummaries": [_routes("us.b", "B", "example.b")]},
    ]
    session = MagicMock()
    session.client.return_value = client
    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(Session=lambda **k: session))

    models = _run(_list(region="us-east-1"))
    assert {m["id"] for m in models if "chat" in m["capabilities"]} == {"us.a", "us.b"}


def test_discovery_that_cannot_run_raises_its_cause_not_an_empty_list(monkeypatch):
    """🔴 Red on main: ``[]``, which reads as an account that serves no models, and a connection
    test claiming a fallback catalog that does not exist. No fake ids either way."""
    from personalclaw.sdk.model import ModelDiscoveryError

    def _boom(*a, **k):
        raise ImportError("No module named 'boto3'")
    monkeypatch.setattr(prov, "_list_bedrock_models_sync", _boom)
    said = (
        "No model list came back from Amazon Bedrock in us-east-1. Try again; if it keeps "
        "failing, check the gateway log. Details: No module named 'boto3'"
    )

    with pytest.raises(ModelDiscoveryError) as failed:
        _run(_list())
    assert str(failed.value) == said
    result = _run(prov.create_catalog({}).test_connection())
    assert (result.ok, result.detail, result.rejected_credential) == (False, said, False)


def test_a_listing_that_fails_leaves_out_only_its_own_models(monkeypatch):
    """The inference-profile listing refused, the model listings answered: what they listed is
    the catalog, and nothing is raised for the part that failed."""
    client = MagicMock()
    client.list_foundation_models.return_value = {"modelSummaries": [
        _record("amazon.nova-pro-v1:0", "Nova Pro", "Amazon", ["TEXT"], ["TEXT"], streams=True,
                invoked=["ON_DEMAND"]),
    ]}
    client.list_inference_profiles.side_effect = RuntimeError("listing refused")
    session = MagicMock()
    session.client.return_value = client
    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(Session=lambda **k: session))

    assert "amazon.nova-pro-v1:0" in {m["id"] for m in _run(_list(region="us-east-1"))}


def test_every_listing_failing_raises_the_first_failure(monkeypatch):
    """Nothing listed and every listing failed: the first failure is the answer."""
    client = MagicMock()
    client.list_foundation_models.side_effect = RuntimeError("models refused")
    client.list_inference_profiles.side_effect = RuntimeError("profiles refused")
    session = MagicMock()
    session.client.return_value = client
    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(Session=lambda **k: session))

    with pytest.raises(RuntimeError, match="models refused"):
        prov._list_bedrock_models_sync("us-east-1", "")


def test_discovery_empty_when_no_models(monkeypatch):
    # AWS reachable but returns nothing → empty list (no hardcoded floor).
    monkeypatch.setattr(prov, "_list_bedrock_models_sync", lambda region, profile: [])
    assert _run(_list(region="eu-west-1")) == []


# ── the gateway log ─────────────────────────────────────────────────────────────────────────


def test_the_apps_log_reaches_the_gateway_log():
    """🔴 Red before: the module logged under its own name, core loads it under a private one, and
    no handler reached either, so a failed listing said "check the gateway log" and that log held
    nothing. Core adds the gateway log's handler to each root an installed app's manifest
    declares."""
    import json
    from pathlib import Path

    manifest = json.loads((Path(__file__).parent / "app.json").read_text(encoding="utf-8"))

    assert prov.logger.name in manifest["loggerRoots"]


def test_a_listing_that_fails_is_logged_once_and_its_traceback_at_debug(monkeypatch, caplog):
    """The catalog is asked on every Models page read, so the log says a failure once every few
    minutes, not once per read. The warning is the sentence alone: its traceback is at DEBUG."""
    import logging

    def _unanswered(region, profile):
        raise RuntimeError("the control plane did not answer AKIAIOSFODNN7EXAMPLE")

    monkeypatch.setattr(prov, "_list_bedrock_models_sync", _unanswered)
    monkeypatch.setattr(prov, "_WARNED_AT", {})
    caplog.set_level(logging.DEBUG, logger="bedrock_models")

    for _ in range(2):
        with pytest.raises(prov.ModelDiscoveryError):
            _run(_list(region="us-east-1"))

    warned = [
        r.getMessage()
        for r in caplog.records
        if r.name == "bedrock_models" and r.levelno == logging.WARNING
    ]
    assert warned == [
        "Listing Amazon Bedrock's models failed: No model list came back from Amazon Bedrock in "
        "us-east-1. Try again; if it keeps failing, check the gateway log. Details: the control "
        "plane did not answer [REDACTED: credential]"
    ]
    traced = [
        r.getMessage()
        for r in caplog.records
        if r.name == "bedrock_models" and r.levelno == logging.DEBUG
        and "\nTraceback" in r.getMessage()
    ]
    assert len(traced) == 2, "each failure's traceback is in the log, at DEBUG"
    head, trace = traced[0].split("\n", 1)
    assert head == "Listing Amazon Bedrock's models failed with this traceback:"
    assert trace.startswith("Traceback (most recent call last):") and "RuntimeError" in trace
    assert "AKIA" not in trace, "the traceback is redacted as the detail is"


def test_a_listing_with_no_aws_credentials_is_logged_as_one_line(monkeypatch, caplog):
    """No AWS credentials is a condition this app recognises, and its sentence names the fix. The
    log says that sentence and nothing after it: botocore's traceback of a missing credential said
    nothing the sentence did not, in sixty lines under it."""
    import logging

    from botocore import exceptions as aws_errors

    def _unsigned(region, profile):
        raise aws_errors.NoCredentialsError()

    monkeypatch.setattr(prov, "_list_bedrock_models_sync", _unsigned)
    monkeypatch.setattr(prov, "_WARNED_AT", {})
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
    caplog.set_level(logging.DEBUG, logger="bedrock_models")

    with pytest.raises(prov.ModelDiscoveryError):
        _run(_list(region="us-east-1"))

    said = [
        r for r in caplog.records if r.name == "bedrock_models" and r.levelno >= logging.WARNING
    ]
    assert [r.getMessage() for r in said] == [
        "Listing Amazon Bedrock's models failed: No AWS credentials were found: this Amazon "
        "Bedrock instance names no AWS profile, and the default credential chain has none. Sign "
        "in with your AWS tool (for example `aws sso login`, or `aws configure` to enter access "
        "keys), then try again, or set AWS Profile on this Amazon Bedrock instance in Settings → "
        "Providers (under Advanced) to a profile that has credentials. Details: Unable to locate "
        "credentials"
    ]
    assert said[0].exc_info is None, "no traceback rides on the record either"


def test_an_instance_that_names_no_region_uses_one_region_for_everything(monkeypatch):
    """Chat and the media calls went to us-west-2 while the model list came from us-east-1, so a
    listed model could be one the call's own region does not serve."""
    import json
    from pathlib import Path

    calls: dict = {}
    foundation = [
        _record("amazon.nova-pro-v1:0", "Nova Pro", "Amazon", ["TEXT"], ["TEXT"], streams=True,
                invoked=["ON_DEMAND"]),
    ]
    monkeypatch.setitem(sys.modules, "boto3", _fake_boto3(foundation, [], calls=calls))
    assert _run(_list())  # the instance names no region
    entry = {"name": "my-bedrock", "type": "bedrock", "options": {}}
    media = [
        *prov._scan_embedding([entry]), *prov._scan_image([entry]),
        *prov._scan_video([entry]), *prov._scan_stt([entry]),
    ]
    chat = [
        prov.create_provider({}),
        prov._factory(entry=prov.ProviderEntry(name="my-bedrock", type="bedrock", model="")),
    ]

    regions = {calls["region"], *(m._region for m in media), *(c._region for c in chat)}
    assert regions == {prov.DEFAULT_REGION} == {"us-east-1"}
    # ...and it is the region the Add-instance form fills in, which the help says.
    manifest = json.loads((Path(__file__).parent / "app.json").read_text())
    region = manifest["provider"]["settingsSchema"]["properties"]["region"]
    assert region["default"] == prov.DEFAULT_REGION
    assert f"Empty uses {prov.DEFAULT_REGION}," in region["x-meta"]["help"]


# ── each model is offered for what it does ──────────────────────────────────────────────────


def _offered(monkeypatch, foundation=FOUNDATION_MODELS, profiles=PROFILES):
    """The catalog's models by id, each with the capabilities it is offered for."""
    monkeypatch.setitem(sys.modules, "boto3", _fake_boto3(foundation, profiles))
    return {m["id"]: m["capabilities"] for m in _run(_list(region="us-east-1"))}


def _for(offered, capability):
    return {model_id for model_id, caps in offered.items() if capability in caps}


def test_only_the_models_that_can_chat_are_offered_for_chat(monkeypatch):
    """🔴 Red before: every inference profile was offered for chat whatever its model does, and a
    rerank model, which writes text, was too: the chat picker listed embedding, rerank,
    image-editing and video-analysis models beside the ones that chat. A chat turn is a Converse
    stream of text, so a model chats when it reads and writes text and streams its answer."""
    offered = _offered(monkeypatch)

    assert _for(offered, "chat") == {
        "amazon.nova-pro-v1:0",
        "us.amazon.nova-pro-v1:0",
        f"us.{CLAUDE}",
        f"global.{CLAUDE}",
        "mistral.voxtral-small-24b-2507",
    }


def test_a_profile_is_offered_for_what_its_model_does(monkeypatch):
    """A profile routes to one model, so it reads and writes what that model does: a Claude
    profile reads images, and an embedding model's profile embeds."""
    offered = _offered(monkeypatch)

    assert offered[f"us.{CLAUDE}"] == ["chat", "image_modality"]
    assert offered["us.amazon.nova-pro-v1:0"] == offered["amazon.nova-pro-v1:0"]
    assert _for(offered, "image_modality") == {
        "amazon.nova-pro-v1:0", "us.amazon.nova-pro-v1:0", f"us.{CLAUDE}", f"global.{CLAUDE}",
    }
    assert _for(offered, "audio_modality") == {"mistral.voxtral-small-24b-2507"}
    assert _for(offered, "embedding") == {
        "amazon.titan-embed-text-v2:0", "us.cohere.embed-v4:0", "global.cohere.embed-v4:0",
    }


def test_a_model_this_catalog_has_no_use_for_is_not_listed(monkeypatch):
    """Reranking, editing an image and analysing a video are no use case a binding can name here,
    and Nova Sonic speaks only a two-way stream: listed, each would be a model offered for
    nothing it can do. Nova Canvas makes images through the image adapter's own list."""
    offered = _offered(monkeypatch)

    for model_id in (
        "amazon.rerank-v1:0",
        "cohere.rerank-v3-5:0",
        "us.stability.stable-image-inpaint-v1:0",
        "us.twelvelabs.pegasus-1-2-v1:0",
        "global.twelvelabs.pegasus-1-2-v1:0",
        "amazon.nova-sonic-v1:0",
        "amazon.nova-canvas-v1:0",
    ):
        assert model_id not in offered, model_id
    assert all(caps for caps in offered.values()), "every listed model is offered for something"


def test_a_profile_whose_model_is_not_listed_is_not_offered(monkeypatch):
    """What a profile does is its model's record. One whose model no listing names is offered for
    nothing rather than guessed to chat."""
    unknown = _routes("us.example.unlisted-v1:0", "US Unlisted", "example.unlisted-v1:0")

    offered = _offered(monkeypatch, profiles=[*PROFILES, unknown])

    assert "us.example.unlisted-v1:0" not in offered
    assert f"us.{CLAUDE}" in offered


def test_profiles_with_no_model_listing_raise_why_the_models_could_not_be_listed(monkeypatch):
    """The model listing refused and the profile listing answered: no profile's model is known,
    so none is offered, and the refusal is the answer rather than a list of guesses."""
    client = MagicMock()
    client.list_foundation_models.side_effect = RuntimeError("models refused")
    client.list_inference_profiles.return_value = {"inferenceProfileSummaries": PROFILES}
    session = MagicMock()
    session.client.return_value = client
    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(Session=lambda **k: session))

    with pytest.raises(RuntimeError, match="models refused"):
        prov._list_bedrock_models_sync("us-east-1", "")
