"""Unit tests for the generic openai-compatible endpoint app.

Registers the ``openai_compatible`` TYPE (the one the Settings "Add OpenAI-Compatible
provider" flow persists) — installed by default. No baked-in endpoint: the user
supplies base_url + key. The OpenAI SDK is stubbed.
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


def test_registers_openai_compatible_type():
    reg = get_default_registry()
    assert reg.capability_of("openai_compatible").type == "openai_compatible"
    assert reg.catalog_of("openai_compatible") is not None


def test_spec_has_no_default_endpoint():
    assert prov.SPEC.type == "openai_compatible"
    assert prov.SPEC.default_base_url == ""  # user must supply it
    assert prov.SPEC.fallback_models == ()   # unknown endpoint → no curated catalog


def test_create_provider_uses_configured_endpoint(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    p = prov.create_provider({"api_key": "k", "endpoint": "https://my-gateway/v1", "model": "my-model"})
    assert p._base_url == "https://my-gateway/v1"
    assert p._model == "my-model"


def test_registry_build(monkeypatch):
    reg = get_default_registry()
    if not any(e.name == "MyGateway" for e in reg.list_entries()):
        reg.register_entry(ProviderEntry(
            name="MyGateway", type="openai_compatible", model="m",
            options={"api_key": "k", "endpoint": "https://gw/v1"},
            declared_capabilities=frozenset({Capability.CHAT}),
        ))
    p = reg.build("MyGateway")
    assert p._base_url == "https://gw/v1"
