"""BedrockCatalog model discovery is dynamic via the AWS control plane.

``BedrockCatalog.list_models`` queries ``bedrock.list_foundation_models``
(ON_DEMAND text models) + ``list_inference_profiles`` (the cross-region ``us.*``
ids), using the entry's region/profile. There is no fallback catalog: when nothing
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


def test_discovery_combines_foundation_and_profiles(monkeypatch):
    calls: dict = {}
    foundation = [
        {"modelId": "amazon.nova-pro-v1:0", "modelName": "Nova Pro", "providerName": "Amazon",
         "inferenceTypesSupported": ["ON_DEMAND"], "inputModalities": ["TEXT", "IMAGE"],
         "modelLifecycle": {"status": "ACTIVE"}},
        # No ON_DEMAND → must NOT appear as a foundation model (only via profile).
        {"modelId": "anthropic.claude-sonnet-4-20250514-v1:0", "modelName": "Claude Sonnet 4",
         "providerName": "Anthropic", "inferenceTypesSupported": ["INFERENCE_PROFILE"],
         "inputModalities": ["TEXT"], "modelLifecycle": {"status": "ACTIVE"}},
        # Legacy/withdrawn → skipped.
        {"modelId": "old.model-v1:0", "modelName": "Old", "inferenceTypesSupported": ["ON_DEMAND"],
         "modelLifecycle": {"status": "LEGACY"}},
    ]
    profiles = [
        {"inferenceProfileId": "us.anthropic.claude-sonnet-4-20250514-v1:0",
         "inferenceProfileName": "Claude Sonnet 4 (US)", "status": "ACTIVE"},
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
    client.list_foundation_models.return_value = {"modelSummaries": []}
    client.list_inference_profiles.side_effect = [
        {"inferenceProfileSummaries": [{"inferenceProfileId": "us.a", "inferenceProfileName": "A", "status": "ACTIVE"}], "nextToken": "t1"},
        {"inferenceProfileSummaries": [{"inferenceProfileId": "us.b", "inferenceProfileName": "B", "status": "ACTIVE"}]},
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
        {"modelId": "amazon.nova-pro-v1:0", "modelName": "Nova Pro", "providerName": "Amazon",
         "inferenceTypesSupported": ["ON_DEMAND"], "inputModalities": ["TEXT"],
         "modelLifecycle": {"status": "ACTIVE"}},
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


def test_a_listing_that_fails_is_logged_once_with_its_traceback(monkeypatch, caplog):
    """The catalog is asked on every Models page read, so the log says a failure once every few
    minutes, not once per read."""
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
    assert len(warned) == 1, warned
    said, trace = warned[0].split("\n", 1)
    assert said == (
        "Listing Amazon Bedrock's models failed: No model list came back from Amazon Bedrock in "
        "us-east-1. Try again; if it keeps failing, check the gateway log. Details: the control "
        "plane did not answer [REDACTED: credential]"
    )
    assert trace.startswith("Traceback (most recent call last):") and "RuntimeError" in trace
    assert "AKIA" not in trace, "the traceback is redacted as the detail is"


def test_an_instance_that_names_no_region_uses_one_region_for_everything(monkeypatch):
    """Chat and the media calls went to us-west-2 while the model list came from us-east-1, so a
    listed model could be one the call's own region does not serve."""
    import json
    from pathlib import Path

    calls: dict = {}
    foundation = [
        {"modelId": "amazon.nova-pro-v1:0", "modelName": "Nova Pro", "providerName": "Amazon",
         "inferenceTypesSupported": ["ON_DEMAND"], "inputModalities": ["TEXT"],
         "modelLifecycle": {"status": "ACTIVE"}},
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
