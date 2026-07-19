"""Unit tests for the generic anthropic-compatible endpoint app.

Registers the ``anthropic_compatible`` TYPE (the one the Settings "Add
Anthropic-Compatible provider" flow persists) — installed by default. Builds on the
Anthropic wire client (stubbed here); the Anthropic protocol has no models-list
endpoint, so the catalog uses the configured model. The user supplies base_url + key.
"""

from __future__ import annotations

import sys
import types

import pytest


@pytest.fixture(autouse=True)
def _stub_anthropic(monkeypatch):
    fake = types.ModuleType("anthropic")

    class _AsyncAnthropic:
        def __init__(self, **kw):
            self.kw = kw

    fake.AsyncAnthropic = _AsyncAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    yield


import provider as prov  # app-local; registers type + catalog on import

from personalclaw.llm.registry import ProviderEntry, get_default_registry
from personalclaw.llm.capabilities import Capability


def test_registers_anthropic_compatible_type():
    reg = get_default_registry()
    assert reg.capability_of("anthropic_compatible").type == "anthropic_compatible"
    assert reg.catalog_of("anthropic_compatible") is not None


def test_spec_is_anthropic_protocol():
    assert prov.SPEC.type == "anthropic_compatible"
    assert prov.SPEC.protocol == "anthropic"
    assert prov.SPEC.default_base_url == ""
    assert prov.SPEC.max_tokens == 4096  # the Anthropic wire requires a max_tokens


def test_create_provider_builds_anthropic(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    p = prov.create_provider({"api_key": "k", "endpoint": "https://my-anthropic", "model": "claude-x"})
    # AnthropicProvider stores base_url + model
    assert p._model == "claude-x"


def test_registry_build(monkeypatch):
    reg = get_default_registry()
    if not any(e.name == "MyAnthropicGW" for e in reg.list_entries()):
        reg.register_entry(ProviderEntry(
            name="MyAnthropicGW", type="anthropic_compatible", model="claude-y",
            options={"api_key": "k", "endpoint": "https://agw"},
            declared_capabilities=frozenset({Capability.CHAT}),
        ))
    p = reg.build("MyAnthropicGW")
    assert p._model == "claude-y"
