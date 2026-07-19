"""kiro-cli bundle tests — kept OUT of the public repo (gitignored alongside the
``acp_bundles/kiro_cli.py`` module + ``providers/bundled/kiro-cli-agent/`` bundle).

kiro-cli is an Amazon-internal CLI vended only as a removable bundle; its source
and these tests are excluded from the published OSS tree. The public bundle
tests live in ``test_acp_bundles.py`` and never import kiro.
"""

from __future__ import annotations

import asyncio
import importlib
import stat
import sys
from pathlib import Path

import pytest

import provider as kiro_cli
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


# ── bundle discovery (kiro-only) ─────────────────────────────────────────────


def test_kiro_app_manifest_is_valid():
    """The app's own manifest is well-formed: a Tier-2 (opt-in) agent provider
    pointing at the app-local ``provider:create_provider``. kiro-cli lives in the
    workspace ``apps/`` dir (first-party, NOT auto-installed), so it carries NO
    ``native`` flag (native:true is Tier-1-only, in src/personalclaw/apps/native/)
    and no legacy ``installByDefault`` (that flag was collapsed into ``native``)."""
    import json
    from pathlib import Path

    m = json.loads((Path(__file__).parent / "app.json").read_text())
    # Tier-2 opt-in: neither the retired installByDefault nor native:true.
    assert "installByDefault" not in m
    assert not m.get("native", False)
    assert m["provider"]["type"] == "agent"
    assert m["provider"]["implementation"] == "provider:create_provider"


# ── registration + launch wiring ─────────────────────────────────────────────


def test_kiro_absent_registers_nothing(monkeypatch):
    """kiro has no npx fallback; an unresolvable binary → no entry registered."""
    monkeypatch.setattr(kiro_cli, "resolve_command", lambda: None)
    result = kiro_cli.create_provider({})
    assert result is None
    with pytest.raises(Exception):
        get_default_registry().get_entry("acp:kiro-cli")


def test_kiro_present_registers_default_dialect_with_acp_subcommand(monkeypatch, tmp_path):
    # Binary is kiro-cli (NOT kiro), launched as `kiro-cli acp`.
    _fake_on_path(monkeypatch, tmp_path, "kiro-cli")
    monkeypatch.delenv("KIRO_CLI_BIN", raising=False)
    kiro_cli.create_provider({})
    entry = get_default_registry().get_entry("acp:kiro-cli")
    assert entry.options["dialect"] == "default"  # kiro speaks the baseline shape
    cmd = entry.options["command"]
    assert cmd[0].endswith("kiro-cli")
    assert cmd[-1] == "acp"  # ACP stdio-mode subcommand appended


def test_kiro_full_argv_override_honoured_verbatim(monkeypatch):
    # A multi-token KIRO_CLI_BIN override is the complete argv — no extra `acp`.
    monkeypatch.setenv("KIRO_CLI_BIN", "/opt/kiro-cli acp")
    kiro_cli.create_provider({})
    cmd = get_default_registry().get_entry("acp:kiro-cli").options["command"]
    assert cmd == ["/opt/kiro-cli", "acp"]


def test_kiro_factory_returns_none(monkeypatch, tmp_path):
    """Like every acp bundle, the factory returns None (config/registry-based)."""
    _fake_on_path(monkeypatch, tmp_path, "kiro-cli")
    monkeypatch.delenv("KIRO_CLI_BIN", raising=False)
    assert kiro_cli.create_provider({}) is None


# ── persona discovery ────────────────────────────────────────────────────────


def _stub_discovery_client(monkeypatch, session_new: dict):
    """Stub AcpConnection spawn + handshake so discover_agents reads `session_new`
    without launching a real process. Post-P9#7 discover_agents probes on a throwaway
    AcpConnection (spawn → initialize → new_session → last_session_new_snapshot) — the
    old AcpClient._spawn seam was removed in the cutover. Mirrors the public bundle
    suite's stub in test_acp_bundles.py."""
    from unittest.mock import AsyncMock, MagicMock

    from personalclaw.acp import session as session_mod
    from personalclaw.llm import acp_agent as acp_mod

    fake_conn = MagicMock()
    fake_conn.initialize = AsyncMock(return_value={})
    fake_conn.new_session = AsyncMock(return_value=MagicMock())
    fake_conn.last_session_new_snapshot = session_new
    fake_conn.close = AsyncMock()

    async def fake_spawn(**kwargs):  # AcpConnection.spawn(...) classmethod
        return fake_conn

    monkeypatch.setattr(session_mod.AcpConnection, "spawn", staticmethod(fake_spawn))
    import shutil as _shutil
    monkeypatch.setattr(_shutil, "which", lambda c: "/usr/bin/" + str(c))
    from personalclaw.agents.provider import ReadinessStatus

    async def fake_probe(cls, options):
        return ReadinessStatus(ready=True, state="ready")

    monkeypatch.setattr(acp_mod.AcpAgentProvider, "probe_readiness", classmethod(fake_probe))
    return acp_mod


@pytest.mark.asyncio
async def test_discover_agents_kiro_personas(monkeypatch, tmp_path):
    """kiro discovery → one DiscoveredAgent per availableMode, provider_agent set,
    runtime's models attached to each."""
    from personalclaw.llm.acp_agent import AcpAgentProvider

    snew = {
        "modes": {"availableModes": [
            {"id": "gpu-dev", "name": "gpu-dev", "description": "Dev"},
            {"id": "planner", "name": "Planner", "description": "Plan"},
        ]},
        "models": {"availableModels": [{"modelId": "auto"}, {"modelId": "glm-5"}]},
    }
    _stub_discovery_client(monkeypatch, snew)
    fake = _fake_on_path(monkeypatch, tmp_path, "kiro-cli")
    agents = await AcpAgentProvider.discover_agents({
        "command": [str(fake), "acp"], "dialect": "default", "runtime_id": "acp:kiro-cli",
    })
    assert [a.id for a in agents] == ["acp:kiro-cli/gpu-dev", "acp:kiro-cli/planner"]
    assert agents[0].name == "gpu-dev"
    assert agents[0].provider_agent == "gpu-dev"
    assert agents[0].runtime == "acp:kiro-cli"
    assert agents[0].models == ["auto", "glm-5"]
    assert all(a.reasoning_effort == "" for a in agents)
