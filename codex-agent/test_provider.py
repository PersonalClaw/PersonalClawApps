"""Standalone smoke test for the codex-agent app — proves it loads + exposes the
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


def test_build_env_forwards_codex_path(monkeypatch):
    """The codex-acp adapter drives ``<CODEX_PATH ?? "codex"> app-server`` and
    inherits THAT codex's auth. We must forward the resolved host codex under the
    exact var the adapter reads — ``CODEX_PATH`` — NOT ``CODEX_EXECUTABLE`` (which
    the adapter ignores, so it would fall back to its bundled OpenAI-auth codex and
    fail ``initialize`` with "Authentication required"). Regression for that
    env-var-name mismatch: any working host codex (Bedrock/OpenAI/ChatGPT) must be
    reused without assuming an auth type."""
    monkeypatch.setattr(provider, "_resolve_codex_exec", lambda: "/opt/host/codex")
    env = provider._build_env()
    assert env == {"CODEX_PATH": "/opt/host/codex"}
    assert "CODEX_EXECUTABLE" not in env


def test_build_env_empty_when_no_codex(monkeypatch):
    monkeypatch.setattr(provider, "_resolve_codex_exec", lambda: "")
    assert provider._build_env() == {}
