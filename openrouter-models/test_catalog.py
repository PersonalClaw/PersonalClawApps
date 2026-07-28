"""Catalog tests for the OpenRouter app — live discovery only.

There is no curated fallback catalog: when discovery fails the picker is honestly
empty rather than showing model ids the user cannot call (the de-hardcode
directive). These tests lock that, plus the modality-filtered discovery URL that
makes the declared ``embedding`` capability real.
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


import provider as prov  # app-local; registers on import

from personalclaw.llm.catalog import ModelCatalog, ModelManager


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    yield


class _FakeFetchResponse:
    def __init__(self, status, payload):
        self.status = status
        self.text = json.dumps(payload)
        self.headers: dict[str, str] = {}
        self.body = b""
        self.truncated = False


def _patch_fetch(monkeypatch, response):
    """Record the fetched URL and serve one canned response.

    ``provider`` binds ``fetch`` at import, so that name is patched alongside the
    canonical core paths — patching only the core paths would leave the module's
    own reference pointing at the real network.
    """
    seen: list[str] = []

    async def _fake_fetch(url, *, policy=None, method="GET", headers=None, data=None, **kw):
        seen.append(url)
        return response

    for target in ("personalclaw.net.client.fetch", "personalclaw.sdk.net.fetch",
                   "personalclaw.net.fetch", "provider.fetch"):
        monkeypatch.setattr(target, _fake_fetch, raising=False)
    return seen


def test_catalog_is_plain_catalog():
    cat = prov.create_catalog({})
    assert isinstance(cat, ModelCatalog)
    assert not isinstance(cat, ModelManager)  # hosted API, no local model management


def test_empty_list_when_endpoint_unreachable(monkeypatch):
    # Endpoint 500 → EMPTY list. No curated fallback, no invented ids: an
    # unreachable provider shows nothing rather than models that would 404 on use.
    _patch_fetch(monkeypatch, _FakeFetchResponse(500, {}))
    assert _run(prov.create_catalog({"api_key": "k"}).list_models()) == []


def test_empty_list_when_response_unparseable(monkeypatch):
    class _Garbage:
        status = 200
        text = "<html>not json</html>"
        headers: dict[str, str] = {}

    _patch_fetch(monkeypatch, _Garbage())
    assert _run(prov.create_catalog({"api_key": "k"}).list_models()) == []


def test_live_models_win(monkeypatch):
    _patch_fetch(monkeypatch, _FakeFetchResponse(200, {"data": [{"id": "live-model-1"}]}))
    cat = prov.create_catalog({"api_key": "k", "endpoint": prov.SPEC.default_base_url})
    assert [m.id for m in _run(cat.list_models())] == ["live-model-1"]


def test_discovery_url_has_no_double_v1(monkeypatch):
    # The default base already ends in /v1, so a naive "+ /v1" would produce
    # …/api/v1/v1/models.
    seen = _patch_fetch(monkeypatch, _FakeFetchResponse(200, {"data": []}))
    _run(prov.create_catalog({"api_key": "k"}).list_models())
    assert seen == [
        "https://openrouter.ai/api/v1/models?output_modalities=text,embeddings"
    ]


def test_discovery_requests_embeddings_modality(monkeypatch):
    # Load-bearing for the declared ``embedding`` capability: OpenRouter's default
    # listing is text-only (verified live — 367 models, zero embedding), so without
    # the filter the embedding picker would be permanently empty.
    seen = _patch_fetch(monkeypatch, _FakeFetchResponse(200, {"data": []}))
    _run(prov.create_catalog({"api_key": "k"}).list_models())
    assert "output_modalities=text,embeddings" in seen[0]


def test_endpoint_override_is_honored(monkeypatch):
    seen = _patch_fetch(monkeypatch, _FakeFetchResponse(200, {"data": []}))
    _run(prov.create_catalog({"api_key": "k", "endpoint": "https://proxy/v1"}).list_models())
    assert seen[0].startswith("https://proxy/v1/models?")


def test_discovery_sends_bearer_and_attribution(monkeypatch):
    captured: dict = {}

    async def _fake_fetch(url, *, policy=None, method="GET", headers=None, data=None, **kw):
        captured.update(headers or {})
        return _FakeFetchResponse(200, {"data": []})

    monkeypatch.setattr("provider.fetch", _fake_fetch, raising=False)
    _run(prov.create_catalog({"api_key": "k"}).list_models())
    assert captured["Authorization"] == "Bearer k"
    assert captured["X-OpenRouter-Title"] == "PersonalClaw"


def test_discovery_omits_authorization_without_key(monkeypatch):
    # OpenRouter's discovery routes answer unauthenticated, so a keyless catalog
    # still populates the picker — but must not send an empty Bearer token.
    captured: dict = {}

    async def _fake_fetch(url, *, policy=None, method="GET", headers=None, data=None, **kw):
        captured.update(headers or {})
        return _FakeFetchResponse(200, {"data": [{"id": "m"}]})

    monkeypatch.setattr("provider.fetch", _fake_fetch, raising=False)
    _run(prov.create_catalog({}).list_models())
    assert "Authorization" not in captured


def test_test_connection_needs_key(monkeypatch):
    cat = prov.create_catalog({})
    cat._api_key = ""
    result = _run(cat.test_connection())
    assert result.ok is False
    assert "OPENROUTER_API_KEY" in result.detail


def test_test_connection_reports_model_count(monkeypatch):
    _patch_fetch(monkeypatch, _FakeFetchResponse(200, {"data": [{"id": "a"}, {"id": "b"}]}))
    result = _run(prov.create_catalog({"api_key": "k"}).test_connection())
    assert result.ok is True
    assert result.model_count == 2


def test_test_connection_fails_when_no_models(monkeypatch):
    _patch_fetch(monkeypatch, _FakeFetchResponse(200, {"data": []}))
    result = _run(prov.create_catalog({"api_key": "k"}).test_connection())
    assert result.ok is False


def _patch_fetch_by_route(monkeypatch, routes: dict[str, "_FakeFetchResponse"]):
    """Serve a different response per route, matched by substring.

    ``_patch_fetch`` answers EVERY url with one response, which cannot express the
    case that matters here: ``/key`` rejecting while ``/models`` still returns 200.
    """
    seen: list[str] = []

    async def _fake_fetch(url, *, policy=None, method="GET", headers=None, data=None, **kw):
        seen.append(url)
        for frag, resp in routes.items():
            if frag in url:
                return resp
        raise AssertionError(f"unstubbed url: {url}")

    for target in ("personalclaw.net.client.fetch", "personalclaw.sdk.net.fetch",
                   "personalclaw.net.fetch", "provider.fetch"):
        monkeypatch.setattr(target, _fake_fetch, raising=False)
    return seen


def test_test_connection_rejects_a_bad_key_even_though_models_is_public(monkeypatch):
    """A bad key must FAIL the connection test.

    ``GET /models`` is a PUBLIC route — verified live, it returns 200 both with a
    garbage key and with no key at all. So a test_connection that validates by
    listing models reports "connected" for a typo'd key, which is precisely the
    answer the Settings → "Test connection" button exists to rule out. The probe
    must hit an authenticated route (``GET /key``, which 401s).
    """
    seen = _patch_fetch_by_route(monkeypatch, {
        "/key": _FakeFetchResponse(401, {"error": {"message": "User not found.", "code": 401}}),
        "/models": _FakeFetchResponse(200, {"data": [{"id": "a"}, {"id": "b"}]}),
    })
    result = _run(prov.create_catalog({"api_key": "sk-or-v1-bad"}).test_connection())
    assert result.ok is False, "a rejected key reported as connected"
    assert "key" in result.detail.lower()
    assert any("/key" in u for u in seen), "never probed the authenticated route"


def test_test_connection_ok_path_probes_key_then_counts_models(monkeypatch):
    seen = _patch_fetch_by_route(monkeypatch, {
        "/key": _FakeFetchResponse(200, {"data": {"label": "sk-or-v1-...", "usage": 0}}),
        "/models": _FakeFetchResponse(200, {"data": [{"id": "a"}, {"id": "b"}]}),
    })
    result = _run(prov.create_catalog({"api_key": "k"}).test_connection())
    assert result.ok is True
    assert result.model_count == 2
    assert any("/key" in u for u in seen) and any("/models" in u for u in seen)


def test_catalog_replaces_the_stock_branded_catalog():
    # register_branded_app registers its own BrandedCatalog under this type; the
    # module re-registers afterwards (last-wins) so the filtered one is what the
    # registry hands out. If that ordering ever inverted, embedding discovery breaks.
    from personalclaw.llm.registry import get_default_registry

    assert get_default_registry().catalog_of("openrouter") is prov.create_catalog
    assert isinstance(prov.create_catalog({}), prov.OpenRouterCatalog)
