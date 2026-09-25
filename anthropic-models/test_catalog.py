"""AnthropicCatalog — the curated Claude model list, plus the MEASURED connectivity
axis. The list moved out of core's discovery handler into the app; the connectivity
probe is a real authenticated request, so a key the vendor rejects reads as rejected."""

from __future__ import annotations

import asyncio
import json

import pytest

import provider as prov  # app-local, registers type + catalog on import

from personalclaw.llm.catalog import ModelCatalog, ModelManager
from personalclaw.llm.registry import get_default_registry


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    """A developer's own ANTHROPIC_API_KEY must not decide these outcomes."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    yield


class _FakeFetchResponse:
    def __init__(self, status, payload=None):
        self.status = status
        self.text = json.dumps(payload if payload is not None else {})
        self.headers: dict[str, str] = {}
        self.body = b""
        self.truncated = False


def _patch_fetch(monkeypatch, response):
    """Serve one canned response and record every url + header set seen.

    ``provider`` binds ``fetch`` at import, so that name is patched alongside the
    canonical core paths — patching only the core paths would leave the module's own
    reference live and the stub would never be reached.
    """
    calls: list[tuple[str, dict]] = []

    async def _fake_fetch(url, *, policy=None, method="GET", headers=None, data=None, **kw):
        calls.append((url, dict(headers or {})))
        if isinstance(response, Exception):
            raise response
        return response

    for target in (
        "personalclaw.net.client.fetch",
        "personalclaw.sdk.net.fetch",
        "personalclaw.net.fetch",
        "provider.fetch",
    ):
        monkeypatch.setattr(target, _fake_fetch, raising=False)
    return calls


def test_catalog_registered():
    assert get_default_registry().catalog_of("anthropic") is not None
    cat = prov.create_catalog({"api_key": "sk-ant"})
    assert isinstance(cat, ModelCatalog)
    assert not isinstance(cat, ModelManager)  # hosted API, no local management


def test_lists_current_claude_models():
    cat = prov.create_catalog({"api_key": "sk-ant"})
    models = _run(cat.list_models())
    ids = {m.id for m in models}
    # Current family surfaces (the picker must offer today's models) — the list is
    # sourced by internet search of the current Anthropic model docs. claude-sonnet-5
    # is the current Sonnet (this replaced the stale claude-sonnet-4-6 "current" id).
    assert "claude-opus-4-8" in ids
    assert "claude-sonnet-5" in ids
    assert "claude-haiku-4-5" in ids
    assert "claude-fable-5" in ids
    # Still-available legacy ids remain for back-compat with pinned accounts.
    assert "claude-opus-4-7" in ids
    assert "claude-sonnet-4-6" in ids
    assert "claude-opus-4-1" in ids  # deprecated but callable until 2026-08-05
    # Invitation-only Project Glasswing models must NOT surface in a self-serve picker.
    assert "claude-mythos-5" not in ids
    assert "claude-mythos-preview" not in ids
    # every entry is at least chat-capable
    for m in models:
        assert "chat" in m.capabilities


def test_default_model_derived_from_catalog_by_family_preference():
    # The unpinned default is DERIVED from the curated list (no separately-hardcoded
    # id) — Opus leads per the docs' "start with Claude Opus 4.8" guidance.
    assert prov._pick_default_model() == "claude-opus-4-8"
    # Whatever it resolves to must be a real catalog entry, never a stale literal.
    assert prov._pick_default_model() in {m["id"] for m in prov._ANTHROPIC_MODELS}


def test_connection_needs_a_key():
    no_key = prov.create_catalog({})
    no_key._api_key = ""  # ensure env isn't satisfying it
    result = _run(no_key.test_connection())
    assert result.ok is False
    assert "ANTHROPIC_API_KEY" in result.detail  # names the next action


def test_connection_rejects_a_key_the_vendor_rejects(monkeypatch):
    """A key Anthropic answers 401 to must FAIL the connection test.

    This is the regression. ``test_connection`` used to return ``ok=True`` from key
    PRESENCE alone, which core renders as ``status: "connected"`` and the UI paints as
    "Connected — N model(s) available". Measured on a fresh container install with
    `api_key="sk-ant-deliberately-invalid-000"`: Settings → Providers said
    "Connected — 10 model(s) available", first-run setup marked the provider "Ready"
    and advanced — while the same gateway logged
    `anthropic.AuthenticationError: Error code: 401 … 'API key is invalid.'`.
    """
    calls = _patch_fetch(
        monkeypatch,
        _FakeFetchResponse(401, {"error": {"type": "authentication_error", "message": "API key is invalid."}}),
    )
    result = _run(prov.create_catalog({"api_key": "sk-ant-bad"}).test_connection())
    assert result.ok is False, "a key the vendor rejects was reported as connected"
    assert "key" in result.detail.lower()
    assert "Settings" in result.detail  # names where to fix it
    assert calls, "test_connection made no request at all — it cannot have measured anything"
    assert calls[0][0].endswith("/v1/models")
    assert calls[0][1].get("x-api-key") == "sk-ant-bad", "the probe was not authenticated"
    assert calls[0][1].get("anthropic-version")


def test_connection_ok_only_after_a_real_authenticated_200(monkeypatch):
    calls = _patch_fetch(monkeypatch, _FakeFetchResponse(200, {"data": [{"id": "claude-x"}]}))
    result = _run(prov.create_catalog({"api_key": "sk-ant-good"}).test_connection())
    assert result.ok is True
    # The count reported is the CURATED picker list, not the endpoint's own count.
    assert result.model_count == len(prov._ANTHROPIC_MODELS)
    assert len(calls) == 1


def test_connection_probes_the_configured_base_url_not_anthropic_com(monkeypatch):
    """The settings schema exposes a Base URL and ``create_provider`` honours it, so a
    probe against api.anthropic.com would test a server the provider never calls."""
    calls = _patch_fetch(monkeypatch, _FakeFetchResponse(200))
    _run(prov.create_catalog({"api_key": "k", "endpoint": "https://proxy.example/v9/"}).test_connection())
    assert calls[0][0] == "https://proxy.example/v9/v1/models"


def test_connection_reports_an_unreachable_endpoint_instead_of_raising(monkeypatch):
    _patch_fetch(monkeypatch, RuntimeError("egress blocked: proxy.example"))
    result = _run(prov.create_catalog({"api_key": "k", "endpoint": "https://proxy.example"}).test_connection())
    assert result.ok is False
    assert "proxy.example" in result.detail


def test_connection_calls_a_404_a_wrong_base_url(monkeypatch):
    _patch_fetch(monkeypatch, _FakeFetchResponse(404, {"detail": "nope"}))
    result = _run(prov.create_catalog({"api_key": "k"}).test_connection())
    assert result.ok is False
    assert "Base URL" in result.detail
