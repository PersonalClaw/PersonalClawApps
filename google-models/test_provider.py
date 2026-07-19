"""Unit tests for the google model provider app.

The OpenAI SDK is stubbed (construction triggers its lazy import); these tests assert
the app's spec wiring — the registered TYPE, the default base URL, api-key env
fallback, and that both the config-path and registry-path factories build a provider
pinned to the right endpoint.
"""

from __future__ import annotations

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


import provider as prov  # app-local; registers type + catalog on import

from personalclaw.llm.registry import ProviderEntry, get_default_registry
from personalclaw.llm.capabilities import Capability


def test_type_and_catalog_registered():
    reg = get_default_registry()
    assert reg.capability_of("google").type == "google"
    assert reg.catalog_of("google") is not None


def test_spec_defaults():
    assert prov.SPEC.type == "google"
    assert prov.SPEC.default_base_url == "https://generativelanguage.googleapis.com/v1beta/openai/"
    assert prov.SPEC.api_key_env == "GEMINI_API_KEY"
    assert prov.SPEC.default_model == ""  # de-hardcoded: discovery-resolved, no baked id
    assert Capability.CHAT in prov.SPEC.capabilities


def test_create_provider_uses_default_endpoint(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    p = prov.create_provider({})
    assert p._base_url == "https://generativelanguage.googleapis.com/v1beta/openai/"
    assert p._model == ""  # unpinned → empty at construction; resolved from /v1/models at start()


def test_create_provider_config_overrides(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    p = prov.create_provider({"api_key": "k", "model": "custom-model", "endpoint": "https://proxy/v1"})
    assert p._base_url == "https://proxy/v1"
    assert p._model == "custom-model"


def test_registry_build(monkeypatch):
    reg = get_default_registry()
    if not any(e.name == "google-inst" for e in reg.list_entries()):
        reg.register_entry(ProviderEntry(
            name="google-inst", type="google", model="m",
            options={"api_key": "k"},
            declared_capabilities=frozenset({Capability.CHAT}),
        ))
    p = reg.build("google-inst")
    assert p._base_url == "https://generativelanguage.googleapis.com/v1beta/openai/"
