"""Standalone smoke test for the claude-code-agent app — proves it loads + exposes the
bundle factory in isolation. Comprehensive bundle + core-registry integration
behavior is covered by the core suite (tests/test_acp_bundles.py)."""

from __future__ import annotations

import provider


def test_exposes_create_provider():
    assert callable(provider.create_provider)


def test_factory_without_binary_registers_nothing():
    # On a machine without the CLI on PATH, the bundle registers no entry and the
    # factory returns None (correct: the provider is unavailable there). Never raises.
    assert provider.create_provider({}) is None
