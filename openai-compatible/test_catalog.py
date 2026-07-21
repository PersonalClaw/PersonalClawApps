"""Catalog tests for the generic openai-compatible app — discovers live from the
configured endpoint's /v1/models; no curated fallback (endpoint is unknown)."""

from __future__ import annotations

import asyncio
import json

import provider as prov  # app-local; registers on import

from personalclaw.llm.catalog import ModelCatalog, ModelManager


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


def test_no_fallback_when_unreachable(monkeypatch):
    import aiohttp
    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: _FakeSession(_FakeResp(500, {})))
    cat = prov.create_catalog({"api_key": "k", "endpoint": "https://gw/v1"})
    # No curated list for an unknown endpoint → empty (never raises).
    assert _run(cat.list_models()) == []
