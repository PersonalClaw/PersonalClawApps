"""``gemini-cli-agent`` bundle tests.

Mirrors the sibling ACP-bundle suites: registration wiring, launch-argv assembly,
the absent-binary path, and the one contract that is easy to get silently wrong —
the runtime id must be exactly ``acp:gemini-cli`` so the registered entry JOINS the
core runner-catalog row of the same id (that join is what makes Settings → Agents
show a Gemini row backed by a real bundle rather than a catalog stub).
"""

from __future__ import annotations

import importlib
import stat
import sys
from pathlib import Path

import pytest

import provider as gemini_cli
from personalclaw.llm.registry import get_default_registry, reset_default_registry


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Fresh default registry per test, restored on teardown (mirrors the public
    bundle suite's fixture — see test_acp_bundles.py for the rationale)."""
    import personalclaw.llm as _llm_pkg
    from personalclaw.agents import registry as _agent_reg
    from personalclaw.llm import registry as _model_reg

    saved_registry = _model_reg._default_registry
    saved_module = sys.modules.get("personalclaw.llm.acp_agent")
    saved_pkg_attr = getattr(_llm_pkg, "acp_agent", None)
    saved_agent_providers = dict(_agent_reg._providers)

    reset_default_registry()
    import personalclaw.llm.acp_agent as _acp_agent

    importlib.reload(_acp_agent)
    try:
        yield
    finally:
        _model_reg.set_default_registry(saved_registry)
        if saved_module is not None:
            sys.modules["personalclaw.llm.acp_agent"] = saved_module
            _llm_pkg.acp_agent = saved_pkg_attr
        _agent_reg._providers.clear()
        _agent_reg._providers.update(saved_agent_providers)


@pytest.fixture(autouse=True)
def _no_inherited_override(monkeypatch):
    """A GEMINI_CLI_EXECUTABLE in the developer's own shell must not leak into a
    PATH-resolution test and make it pass for the wrong reason."""
    monkeypatch.delenv("GEMINI_CLI_EXECUTABLE", raising=False)


def _make_exec(path: Path) -> None:
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _fake_on_path(monkeypatch, tmp_path, name: str) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    target = bindir / name
    _make_exec(target)
    monkeypatch.setenv("PATH", str(bindir))
    return target


# ── bundle discovery ─────────────────────────────────────────────────────────


def test_gemini_app_manifest_is_valid():
    """The app's own manifest is well-formed: a Tier-2 (opt-in) agent provider
    pointing at the app-local ``provider:create_provider``. It lives in the
    workspace ``apps/`` dir (first-party, NOT auto-installed), so it carries NO
    ``native`` flag (native:true is Tier-1-only, in src/personalclaw/apps/native/)
    and no legacy ``installByDefault`` (that flag was collapsed into ``native``)."""
    import json

    m = json.loads((Path(__file__).parent / "app.json").read_text())
    assert m["name"] == "gemini-cli-agent"
    # Tier-2 opt-in: neither the retired installByDefault nor native:true.
    assert "installByDefault" not in m
    assert not m.get("native", False)
    assert m["provider"]["type"] == "agent"
    assert m["provider"]["implementation"] == "provider:create_provider"
    assert m["provider"]["capabilities"] == ["acp"]


def test_gemini_declares_no_permissions():
    """Minimum permissions: an ACP agent bundle needs none. The Store shows the
    permission block as the install-consent surface, so an empty one is a claim
    worth pinning — a later drive-by widening has to change this test."""
    import json

    m = json.loads((Path(__file__).parent / "app.json").read_text())
    assert not m.get("permissions")


# ── registration + launch wiring ─────────────────────────────────────────────


def test_gemini_absent_registers_nothing(monkeypatch):
    """No npx fallback; an unresolvable binary → no entry registered. A generic
    machine without Gemini CLI must probe unavailable, never error."""
    monkeypatch.setattr(gemini_cli, "resolve_command", lambda: None)
    result = gemini_cli.create_provider({})
    assert result is None
    with pytest.raises(Exception):
        get_default_registry().get_entry("acp:gemini-cli")


def test_gemini_absent_availability_reports_a_reason(monkeypatch):
    """The unavailable case carries UI copy naming the missing binary, so the card
    can grey out with a reason instead of a bare disabled toggle."""
    monkeypatch.setattr(gemini_cli, "resolve_command", lambda: None)
    ok, reason = gemini_cli.availability()
    assert ok is False
    assert "gemini" in reason


def test_gemini_available_when_binary_resolves(monkeypatch, tmp_path):
    _fake_on_path(monkeypatch, tmp_path, "gemini")
    assert gemini_cli.availability() == (True, "")


def test_gemini_present_registers_default_dialect_with_acp_flag(monkeypatch, tmp_path):
    """Argv assembly uses the FLAG. Gemini enters ACP via ``--experimental-acp``;
    a subcommand (kiro's ``acp``) or an adapter binary (claude-code's
    ``claude-agent-acp``) would both be wrong here."""
    _fake_on_path(monkeypatch, tmp_path, "gemini")
    gemini_cli.create_provider({})
    entry = get_default_registry().get_entry("acp:gemini-cli")
    assert entry.options["dialect"] == "default"  # gemini speaks the baseline shape
    cmd = entry.options["command"]
    assert cmd[0].endswith("gemini")
    assert cmd[-1] == "--experimental-acp"
    assert "acp" not in cmd  # NOT a subcommand-style launch
    assert cmd == [str(tmp_path / "bin" / "gemini"), "--experimental-acp"]  # no adapter


def test_gemini_env_override_wins_over_path(monkeypatch, tmp_path):
    """GEMINI_CLI_EXECUTABLE beats a PATH hit, and the ACP flag survives: a
    single-token override is a binary, not a complete argv, so dropping the flag
    would launch the interactive REPL that never answers ``initialize``."""
    on_path = _fake_on_path(monkeypatch, tmp_path, "gemini")
    override = tmp_path / "elsewhere" / "gemini"
    override.parent.mkdir(exist_ok=True)
    _make_exec(override)
    monkeypatch.setenv("GEMINI_CLI_EXECUTABLE", str(override))

    gemini_cli.create_provider({})
    cmd = get_default_registry().get_entry("acp:gemini-cli").options["command"]
    assert cmd == [str(override), "--experimental-acp"]
    assert cmd[0] != str(on_path)


def test_gemini_settings_bin_path_beats_path(monkeypatch, tmp_path):
    """The ``acp_bin`` setting is the same override by another name (it writes the
    env var), so the config round-trip reaches the launch argv."""
    _fake_on_path(monkeypatch, tmp_path, "gemini")
    override = tmp_path / "settings" / "gemini"
    override.parent.mkdir(exist_ok=True)
    _make_exec(override)

    gemini_cli.create_provider({"acp_bin": str(override)})
    cmd = get_default_registry().get_entry("acp:gemini-cli").options["command"]
    assert cmd == [str(override), "--experimental-acp"]


def test_gemini_full_argv_override_honoured_verbatim(monkeypatch):
    """A multi-token override is the complete argv — and must not grow a second
    copy of the flag it already carries."""
    monkeypatch.setenv("GEMINI_CLI_EXECUTABLE", "/opt/gemini --experimental-acp")
    gemini_cli.create_provider({})
    cmd = get_default_registry().get_entry("acp:gemini-cli").options["command"]
    assert cmd == ["/opt/gemini", "--experimental-acp"]


def test_gemini_declares_no_engine_requirement(monkeypatch, tmp_path):
    """``adapter: null`` in the catalog: the ``gemini`` binary IS the engine, so the
    bundle declares no ``requires_executable`` (that is for thin shims like
    ``claude-agent-acp`` → ``claude``). Declaring one would make a working runtime
    probe ``not_found``."""
    _fake_on_path(monkeypatch, tmp_path, "gemini")
    gemini_cli.create_provider({})
    options = get_default_registry().get_entry("acp:gemini-cli").options
    assert "requires_executable" not in options


def test_gemini_login_command_is_the_bare_binary(monkeypatch, tmp_path):
    """Auth hint: Gemini self-authenticates (Google OAuth / GEMINI_API_KEY) and has
    no ``login`` subcommand, so the Sign-in terminal pre-types the resolved binary
    — with the ACP flag stripped, since ACP mode is not an interactive auth flow."""
    fake = _fake_on_path(monkeypatch, tmp_path, "gemini")
    gemini_cli.create_provider({})
    options = get_default_registry().get_entry("acp:gemini-cli").options
    assert options["login_command"] == [str(fake)]
    assert "--experimental-acp" not in options["login_command"]


def test_gemini_login_command_falls_back_to_the_bin_name():
    assert gemini_cli.login_command([]) == ["gemini"]


def test_gemini_factory_returns_none(monkeypatch, tmp_path):
    """Like every acp bundle, the factory returns None (config/registry-based)."""
    _fake_on_path(monkeypatch, tmp_path, "gemini")
    assert gemini_cli.create_provider({}) is None


# ── the core runner-catalog join ─────────────────────────────────────────────


def test_registered_runtime_id_joins_the_core_runner_catalog(monkeypatch, tmp_path):
    """The done-when clause: Settings → Agents shows Gemini CLI because the core
    catalog row and THIS bundle agree on the runtime id ``acp:gemini-cli``.

    Asserted from both ends — the id the bundle registers, and the id the shipped
    catalog declares — so a rename on either side reds here instead of silently
    producing a catalog row with no bundle behind it.
    """
    from personalclaw.agents import runners

    # Read the SHIPPED catalog only: no BYO row from the developer's real home may
    # stand in for the packaged one.
    monkeypatch.setattr(runners, "user_catalog_dir", lambda: tmp_path / "no-byo-rows")

    _fake_on_path(monkeypatch, tmp_path, "gemini")
    gemini_cli.create_provider({})
    entry = get_default_registry().get_entry("acp:gemini-cli")
    assert entry.name == f"acp:{gemini_cli.CLI}" == "acp:gemini-cli"
    assert entry.options["extension"] == "gemini-cli-agent"  # UI join back to this card

    row = runners.definition_for_runtime(entry.name)
    assert row is not None, "no shipped runner-catalog row for acp:gemini-cli"
    assert row.id == "gemini-cli"
    assert row.display_name == "Gemini CLI"
    # The catalog's declared launch shape is the one this bundle actually builds.
    assert list(row.bin_names) == ["gemini"]
    assert list(row.acp_args) == ["--experimental-acp"]
    assert row.env_var == "GEMINI_CLI_EXECUTABLE"
    assert row.adapter is None
    assert list(entry.options["command"])[1:] == list(row.acp_args)
