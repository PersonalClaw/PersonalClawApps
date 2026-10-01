"""Standalone smoke test for the codex-agent app — proves it loads + exposes the
bundle factory in isolation. Comprehensive bundle + core-registry integration
behavior is covered by the core suite (tests/test_acp_bundles.py)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import provider

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # the repo root: apps_testkit

from apps_testkit.acp_env import PLANTED_SECRETS, handed_env, stub_command  # noqa: E402


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


def test_the_cli_is_handed_the_folder_that_picks_its_provider(monkeypatch, tmp_path):
    """🔴 Red on main: an ACP CLI gets no variable of the gateway's its app does not declare, so an
    owner's ``CODEX_HOME`` — the folder whose config names Codex's provider and model — never
    reached it. A stub in the adapter's place, spawned from the entry the app registers."""
    from personalclaw.llm.registry import get_default_registry

    codex_home = str(tmp_path / "their-codex")
    for name, value in {"CODEX_HOME": codex_home, **PLANTED_SECRETS}.items():
        monkeypatch.setenv(name, value)
    command, record = stub_command(tmp_path)
    monkeypatch.setattr(provider, "resolve_command", lambda provision=False: command)
    monkeypatch.setattr(provider, "_resolve_codex_exec", lambda: "/opt/host/codex")
    provider.create_provider({})

    handed = handed_env(get_default_registry().get_entry("acp:codex"), record, tmp_path / "w")
    assert handed.get("CODEX_HOME") == codex_home
    assert sorted(set(PLANTED_SECRETS) & set(handed)) == []
    assert handed["CODEX_PATH"] == "/opt/host/codex", "what the app computed still is"


def test_the_readme_names_every_variable_the_app_passes():
    readme = (Path(provider.__file__).parent / "README.md").read_text()
    assert [name for name in provider.PROVIDER_ENV if f"`{name}`" not in readme] == []


def test_install_consent_names_what_it_starts_and_installs():
    """Install consent names the program this app has core start and the npm package core
    installs for it, from the manifest, and core does neither for an app whose manifest does
    not declare it. So the manifest says what the code does: the CLI it resolves, and the
    adapter package it asks for."""
    from personalclaw.sdk.manifest import AppManifest

    manifest = AppManifest.from_dict(
        json.loads((Path(provider.__file__).parent / "app.json").read_text())
    )
    assert manifest.validate() == []
    assert [p.program for p in manifest.launches] == provider._CODEX_BIN_NAMES
    assert manifest.dependencies.npmPackages == [provider._ACP_NPM_PKG]
    assert manifest.launches[0].inherits == ["sign-in", "settings", "auto-approve-rules"]
    assert manifest.writes == []
