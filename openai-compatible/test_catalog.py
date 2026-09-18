"""Catalog tests for the generic openai-compatible app — discovers live from the
configured endpoint's /v1/models; no curated fallback (endpoint is unknown), so a
discovery failure is raised rather than swallowed into an empty picker (core #955)."""

from __future__ import annotations

import asyncio
import json

import provider as prov  # app-local; registers on import
import pytest

from personalclaw.llm.catalog import ModelCatalog, ModelDiscoveryError, ModelManager


def _run(coro):
    return asyncio.run(coro)


class _FetchResp:
    """Minimal stand-in for a personalclaw.sdk.net.fetch response — the /models
    discovery reads only ``.status`` and ``.text`` (then json.loads the text)."""

    def __init__(self, status, payload):
        self.status = status
        self.text = json.dumps(payload)


def test_catalog_is_plain_catalog():
    cat = prov.create_catalog({"endpoint": "https://gw/v1"})
    assert isinstance(cat, ModelCatalog)
    assert not isinstance(cat, ModelManager)


def test_lists_live_models(monkeypatch):
    # /models discovery routes through the net.fetch egress chokepoint (not raw
    # aiohttp) — patch that. Patch at the definition module so the local import
    # inside openai_compatible_list_models picks up the fake.
    from unittest.mock import AsyncMock

    import personalclaw.sdk.net as _net

    monkeypatch.setattr(
        _net, "fetch", AsyncMock(return_value=_FetchResp(200, {"data": [{"id": "served-model"}]}))
    )
    cat = prov.create_catalog({"api_key": "k", "endpoint": "https://gw/v1"})
    models = _run(cat.list_models())
    assert [m.id for m in models] == ["served-model"]


def test_discovery_failure_is_raised_not_swallowed(monkeypatch):
    # Patch the SAME layer the live-models test does: discovery routes through the
    # net.fetch egress chokepoint, so the old aiohttp.ClientSession patch never
    # intercepted anything — the call escaped to the real egress guard and the test
    # only passed because the failure used to be swallowed into [].
    from unittest.mock import AsyncMock

    import personalclaw.sdk.net as _net

    monkeypatch.setattr(_net, "fetch", AsyncMock(return_value=_FetchResp(500, {})))
    assert list(prov.SPEC.fallback_models) == []  # the invariant the raise depends on
    cat = prov.create_catalog({"api_key": "k", "endpoint": "https://gw/v1"})
    # An unknown endpoint has no curated list to fall back on, so a discovery
    # failure is raised rather than reported as "this endpoint serves no models"
    # (core #955) — the caller relays it onto the provider row.
    with pytest.raises(ModelDiscoveryError) as exc:
        _run(cat.list_models())
    assert "500" in str(exc.value)  # the status the user has to act on, not a bare ""
