"""BedrockCatalog model discovery is dynamic via the AWS control plane.

``BedrockCatalog.list_models`` queries ``bedrock.list_foundation_models``
(ON_DEMAND text models) + ``list_inference_profiles`` (the cross-region ``us.*``
ids), using the entry's region/profile. On any failure it falls back to a small
curated catalog so the dropdown is never empty. This logic moved out of core into
the app during the model-catalog-isolation slice; the test moved with it.
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
    assert {m["id"] for m in models} == {"us.a", "us.b"}


def test_discovery_empty_when_boto3_missing(monkeypatch):
    # boto3 import failure inside the sync worker → EMPTY list (no hardcoded fallback,
    # per the de-hardcode directive). Discovery is authoritative; the UI shows no
    # models rather than fake ids.
    def _boom(*a, **k):
        raise ImportError("No module named 'boto3'")
    monkeypatch.setattr(prov, "_list_bedrock_models_sync", _boom)

    assert _run(_list()) == []


def test_discovery_empty_when_no_models(monkeypatch):
    # AWS reachable but returns nothing → empty list (no hardcoded floor).
    monkeypatch.setattr(prov, "_list_bedrock_models_sync", lambda region, profile: [])
    assert _run(_list(region="eu-west-1")) == []
