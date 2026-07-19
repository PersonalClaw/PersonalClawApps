"""Catalog tests for the generic anthropic-compatible app — the Anthropic wire has
no models-list endpoint, so discovery returns the (empty) curated fallback and
connectivity is reported from key presence."""

from __future__ import annotations

import asyncio

import provider as prov  # app-local; registers on import

from personalclaw.llm.catalog import ModelCatalog, ModelManager


def _run(coro):
    return asyncio.run(coro)


def test_catalog_is_plain_catalog():
    cat = prov.create_catalog({"endpoint": "https://agw"})
    assert isinstance(cat, ModelCatalog)
    assert not isinstance(cat, ModelManager)


def test_list_models_empty_no_models_endpoint():
    # Anthropic protocol → no /v1/models; no curated fallback for an unknown
    # endpoint, so the list is empty (never raises, never hits the network).
    cat = prov.create_catalog({"api_key": "k", "endpoint": "https://agw"})
    assert _run(cat.list_models()) == []


def test_test_connection_reports_key_presence(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cat = prov.create_catalog({})
    cat._api_key = ""
    assert _run(cat.test_connection()).ok is False
