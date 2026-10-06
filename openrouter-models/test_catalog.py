"""Catalog tests for the OpenRouter app — live discovery only.

There is no curated fallback catalog: a discovery that fails raises
``ModelDiscoveryError`` saying why, rather than showing model ids the user cannot
call or an empty list that reads as an account serving no models. These tests lock
that, plus the modality-filtered discovery URL that makes the declared
``embedding`` capability real.
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
    # The error a refused request is raised as, so the SDK does not retry it.
    fake.OpenAIError = type("OpenAIError", (Exception,), {})

    class _AsyncOpenAI:
        def __init__(self, **kw):
            self.kw = kw

    fake.AsyncOpenAI = _AsyncOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake)
    yield


import provider as prov  # app-local; registers on import

from personalclaw.llm.catalog import ModelCatalog, ModelManager
from personalclaw.sdk.model import ModelDiscoveryError


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


_LISTING_URL = "https://openrouter.ai/api/v1/models?output_modalities=text,embeddings"
_RETRY_LISTING = (
    "Try again in a moment; if it keeps happening, check Base URL on this OpenRouter instance "
    "in Settings → Providers (under Advanced)."
)


def _listing_failure(catalog) -> ModelDiscoveryError:
    with pytest.raises(ModelDiscoveryError) as ei:
        _run(catalog.list_models())
    return ei.value


@pytest.mark.parametrize(("status", "payload", "sentence"), [
    (500, {"error": {"message": "Internal Server Error", "code": 500}},
     "OpenRouter failed on its side (model listing, HTTP 500). Try again in a few minutes. "
     "Details: Internal Server Error"),
    (404, {"error": {"message": "Not Found", "code": 404}},
     "OpenRouter answered the model listing request with HTTP 404 (not found). Check Base URL "
     "on this OpenRouter instance in Settings → Providers (under Advanced); left empty, it uses "
     "https://openrouter.ai/api/v1. Details: Not Found"),
])
def test_a_listing_answered_with_an_error_raises_what_to_do(monkeypatch, status, payload, sentence):
    # No curated fallback and no invented ids, and no [] either: that read as an account that
    # serves no models, when the listing never got a list.
    _patch_fetch(monkeypatch, _FakeFetchResponse(status, payload))
    exc = _listing_failure(prov.create_catalog({"api_key": "k"}))
    assert str(exc) == sentence
    assert (exc.url, exc.status, exc.rejected_credential) == (_LISTING_URL, status, False)


@pytest.mark.parametrize("status", [401, 403])
def test_a_listing_that_refuses_the_key_flags_it(monkeypatch, status):
    _patch_fetch(monkeypatch, _FakeFetchResponse(
        status, {"error": {"message": "User not found.", "code": status}}))
    exc = _listing_failure(prov.create_catalog({"api_key": "k"}))
    assert str(exc) == (
        "OpenRouter rejected the API key (model listing). Check the key in Settings → "
        "Providers, or OPENROUTER_API_KEY."
    )
    assert exc.rejected_credential is True


def test_a_keyless_listing_that_is_refused_asks_for_a_key(monkeypatch):
    # Sent with no key, so "rejected the API key" would not be true: none was sent.
    _patch_fetch(monkeypatch, _FakeFetchResponse(
        401, {"error": {"message": "No auth credentials found", "code": 401}}))
    exc = _listing_failure(prov.create_catalog({}))
    assert str(exc) == (
        "OpenRouter asked for an API key to list its models (HTTP 401). Add the key on this "
        "OpenRouter instance in Settings → Providers, or set OPENROUTER_API_KEY."
    )


def test_a_listing_that_gets_no_answer_says_what_to_check(monkeypatch):
    async def _fake_fetch(url, *, policy=None, method="GET", headers=None, data=None, **kw):
        raise ConnectionResetError(54, "Connection reset by peer")

    for target in ("personalclaw.net.client.fetch", "personalclaw.sdk.net.fetch",
                   "personalclaw.net.fetch", "provider.fetch"):
        monkeypatch.setattr(target, _fake_fetch, raising=False)
    exc = _listing_failure(prov.create_catalog({"api_key": "k"}))
    assert str(exc) == (
        "The connection to OpenRouter failed during the model listing. Check this computer's "
        "internet connection and try again; if Base URL is set on this OpenRouter instance in "
        "Settings → Providers (under Advanced), check it too. Details: [Errno 54] Connection "
        "reset by peer"
    )
    assert (exc.url, exc.status, exc.rejected_credential) == (_LISTING_URL, None, False)


def test_an_unreadable_listing_raises_rather_than_listing_none(monkeypatch):
    class _Garbage:
        status = 200
        text = "<html>not json</html>"
        headers: dict[str, str] = {}

    _patch_fetch(monkeypatch, _Garbage())
    exc = _listing_failure(prov.create_catalog({"api_key": "k"}))
    assert str(exc) == (
        f"OpenRouter's answer to the model listing request could not be read. {_RETRY_LISTING} "
        "Details: Expecting value: line 1 column 1 (char 0)"
    )


@pytest.mark.parametrize("payload", [
    [], "a string", 7, None,             # JSON that is not an object: these raised AttributeError
    {}, {"data": "x"}, {"data": None},   # an object with no "data" list: these listed none
    {"data": [{"name": "no id"}]},       # entries, none of them carrying a model id
])
def test_a_listing_that_is_not_openrouters_list_raises_rather_than_listing_none(
    monkeypatch, payload,
):
    _patch_fetch(monkeypatch, _FakeFetchResponse(200, payload))
    exc = _listing_failure(prov.create_catalog({"api_key": "k"}))
    assert str(exc) == (
        "OpenRouter answered the model listing request, but not with the model list it sends. "
        f"{_RETRY_LISTING} Details: {json.dumps(payload)}"
    )
    assert (exc.status, exc.rejected_credential) == (200, False)


def test_a_listing_of_none_is_an_empty_list(monkeypatch):
    # The one [] left: OpenRouter answered with its list, and the list is empty.
    _patch_fetch(monkeypatch, _FakeFetchResponse(200, {"data": []}))
    assert _run(prov.create_catalog({"api_key": "k"}).list_models()) == []


def test_a_failed_listing_is_not_remembered(monkeypatch):
    # Once the endpoint answers, the next listing has its models: the failure was not kept
    # as an empty list.
    cat = prov.create_catalog({"api_key": "k"})
    _patch_fetch(monkeypatch, _FakeFetchResponse(500, {}))
    _listing_failure(cat)
    _patch_fetch(monkeypatch, _FakeFetchResponse(200, {"data": [{"id": "live-model-1"}]}))
    assert [m.id for m in _run(cat.list_models())] == ["live-model-1"]


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
    # This read "No API key configured (set it or OPENROUTER_API_KEY)", naming no setting.
    assert result.detail == (
        "No OpenRouter API key is set. Add it in OpenRouter API Key on this OpenRouter instance "
        "in Settings → Providers, or set OPENROUTER_API_KEY."
    )
    assert result.rejected_credential is False  # nothing was sent, so nothing was refused


def test_test_connection_reports_model_count(monkeypatch):
    _patch_fetch(monkeypatch, _FakeFetchResponse(200, {"data": [{"id": "a"}, {"id": "b"}]}))
    result = _run(prov.create_catalog({"api_key": "k"}).test_connection())
    assert result.ok is True
    assert result.model_count == 2


@pytest.mark.parametrize(("endpoint", "detail"), [
    ("", "OpenRouter accepted the key but listed no chat or embedding models. Try again in a "
         "few minutes."),
    ("https://proxy.example/v1",
     "OpenRouter accepted the key, but the server at Base URL listed no chat or embedding "
     "models. Check Base URL on this OpenRouter instance in Settings → Providers (under "
     "Advanced); left empty, it uses https://openrouter.ai/api/v1."),
])
def test_test_connection_fails_when_no_models(monkeypatch, endpoint, detail):
    # This read "Key is valid but no models were listed", with no next step. The listing
    # route is public, so an empty list is the server's own answer, whatever the key.
    _patch_fetch(monkeypatch, _FakeFetchResponse(200, {"data": []}))
    result = _run(prov.create_catalog({"api_key": "k", "endpoint": endpoint}).test_connection())
    assert result.ok is False
    assert result.detail == detail


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
    result = _run(prov.create_catalog({"api_key": "fake-openrouter-bad"}).test_connection())
    assert result.ok is False, "a rejected key reported as connected"
    assert "key" in result.detail.lower()
    assert any("/key" in u for u in seen), "never probed the authenticated route"


@pytest.mark.parametrize(("status", "rejected"), [
    (401, True), (403, True),                    # OpenRouter refused the key it was sent
    (402, False), (404, False), (500, False),    # credits, the address, OpenRouter's own side
])
def test_test_connection_flags_the_key_only_when_openrouter_refuses_it(
    monkeypatch, status, rejected,
):
    # A refused key never said so, so the page could not say to update it.
    _patch_fetch_by_route(monkeypatch, {
        "/key": _FakeFetchResponse(
            status, {"error": {"message": "upstream words", "code": status}}),
    })
    result = _run(prov.create_catalog({"api_key": "k"}).test_connection())
    assert result.ok is False
    assert result.rejected_credential is rejected


def test_test_connection_ok_path_probes_key_then_counts_models(monkeypatch):
    seen = _patch_fetch_by_route(monkeypatch, {
        "/key": _FakeFetchResponse(200, {"data": {"label": "fake-openrouter-1", "usage": 0}}),
        "/models": _FakeFetchResponse(200, {"data": [{"id": "a"}, {"id": "b"}]}),
    })
    result = _run(prov.create_catalog({"api_key": "k"}).test_connection())
    assert result.ok is True
    assert result.model_count == 2
    assert any("/key" in u for u in seen) and any("/models" in u for u in seen)


@pytest.mark.parametrize(("status", "detail", "rejected"), [
    (404, "OpenRouter answered the model listing request with HTTP 404 (not found). Check Base "
          "URL on this OpenRouter instance in Settings → Providers (under Advanced); left empty, "
          "it uses https://openrouter.ai/api/v1. Details: upstream words", False),
    (401, "OpenRouter rejected the API key (model listing). Check the key in Settings → "
          "Providers, or OPENROUTER_API_KEY.", True),
])
def test_test_connection_with_an_accepted_key_says_why_the_listing_failed(
    monkeypatch, status, detail, rejected,
):
    # This read "Key is valid but no models were listed" whatever had gone wrong.
    _patch_fetch_by_route(monkeypatch, {
        "/key": _FakeFetchResponse(200, {"data": {"label": "fake-openrouter-1", "usage": 0}}),
        "/models": _FakeFetchResponse(
            status, {"error": {"message": "upstream words", "code": status}}),
    })
    result = _run(prov.create_catalog({"api_key": "k"}).test_connection())
    assert result.ok is False
    assert result.detail == detail
    assert result.rejected_credential is rejected


def test_test_connection_answered_not_found_points_at_base_url(monkeypatch):
    """A key check answered with neither 200 nor a key refusal is about where it was sent. It
    read "failed (HTTP 404):" and OpenRouter's words, with no next step."""
    _patch_fetch_by_route(monkeypatch, {
        "/key": _FakeFetchResponse(404, {"error": {"message": "Not Found", "code": 404}}),
    })
    result = _run(prov.create_catalog(
        {"api_key": "k", "endpoint": "https://proxy.example/v1"}).test_connection())
    assert result.ok is False
    assert result.detail == (
        "OpenRouter answered the key check request with HTTP 404 (not found). Check Base URL on "
        "this OpenRouter instance in Settings → Providers (under Advanced); left empty, it uses "
        "https://openrouter.ai/api/v1. Details: Not Found"
    )


def _unreachable():
    from personalclaw.sdk.net import EgressBlocked, GuardDecision

    return EgressBlocked(GuardDecision(
        allow=False, host="proxy.invalid", reason="host 'proxy.invalid' is not resolvable",
        category="unresolvable",
    ))


@pytest.mark.parametrize(("error", "detail"), [
    (ConnectionResetError(54, "Connection reset by peer"),
     "The connection to OpenRouter failed during the key check. Check this computer's internet "
     "connection and try again; if Base URL is set on this OpenRouter instance in Settings → "
     "Providers (under Advanced), check it too. Details: [Errno 54] Connection reset by peer"),
    (_unreachable(),
     "OpenRouter key check was not sent: proxy.invalid could not be found. Check this "
     "computer's internet connection and try again; if Base URL is set on this OpenRouter "
     "instance in Settings → Providers (under Advanced), check it too. Details: host "
     "'proxy.invalid' is not resolvable"),
])
def test_test_connection_that_gets_no_answer_says_what_to_check(monkeypatch, error, detail):
    # This read "Could not reach OpenRouter:" and the error's words, with no next step.
    async def _fake_fetch(url, *, policy=None, method="GET", headers=None, data=None, **kw):
        raise error

    for target in ("personalclaw.net.client.fetch", "personalclaw.sdk.net.fetch",
                   "personalclaw.net.fetch", "provider.fetch"):
        monkeypatch.setattr(target, _fake_fetch, raising=False)
    result = _run(prov.create_catalog(
        {"api_key": "k", "endpoint": "https://proxy.invalid/v1"}).test_connection())
    assert result.ok is False
    assert result.detail == detail
    assert result.rejected_credential is False  # no answer, so no refusal


def test_catalog_replaces_the_stock_branded_catalog():
    # register_branded_app registers its own BrandedCatalog under this type; the
    # module re-registers afterwards (last-wins) so the filtered one is what the
    # registry hands out. If that ordering ever inverted, embedding discovery breaks.
    from personalclaw.llm.registry import get_default_registry

    assert get_default_registry().catalog_of("openrouter") is prov.create_catalog
    assert isinstance(prov.create_catalog({}), prov.OpenRouterCatalog)
