"""AnthropicCatalog — the curated Claude model list (the Messages API exposes no
models endpoint). This list moved out of core's discovery handler into the app."""

from __future__ import annotations

import asyncio

import provider as prov  # app-local, registers type + catalog on import

from personalclaw.llm.catalog import ModelCatalog, ModelManager
from personalclaw.llm.registry import get_default_registry


def _run(coro):
    return asyncio.run(coro)


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


def test_connection_reports_key_presence():
    with_key = _run(prov.create_catalog({"api_key": "sk-ant"}).test_connection())
    assert with_key.ok is True
    assert with_key.model_count == len(prov._ANTHROPIC_MODELS)

    no_key = prov.create_catalog({})
    no_key._api_key = ""  # ensure env isn't satisfying it
    assert _run(no_key.test_connection()).ok is False
