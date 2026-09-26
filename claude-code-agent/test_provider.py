"""The claude-code-agent app: it loads, and the Claude it spawns runs with a config of its own.

Measured before this was written:

* Isolation was opt-in (``PERSONALCLAW_CC_ISOLATE=1``) and off by default, so the spawned Claude
  read the operator's ``~/.claude`` and kept every ``permissions.allow`` rule and
  ``defaultMode`` in it, while the app's description said an isolated ``CLAUDE_CONFIG_DIR``
  stripped them.
* With it on, the "isolated" config was seeded from the real ``~/.claude/settings.json``, and a
  ``CLAUDE_CONFIG_DIR`` the operator had set became the isolated root and was rewritten.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

import provider

PERMISSIVE = {
    "permissions": {"allow": ["Bash(*)"], "defaultMode": "acceptEdits", "deny": ["Bash(rm:*)"]},
    "enabledPlugins": {"x": True},
    "apiKeyHelper": "print-my-key",
}


@pytest.fixture
def operator(monkeypatch, tmp_path):
    """An operator whose own ~/.claude auto-approves everything, and who set CLAUDE_CONFIG_DIR."""
    home = tmp_path / "home"
    own = home / ".claude"
    own.mkdir(parents=True)
    (own / "settings.json").write_text(json.dumps(PERMISSIVE))
    relocated = tmp_path / "their-claude"
    relocated.mkdir()
    (relocated / "settings.json").write_text(json.dumps(PERMISSIVE))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(relocated))
    return {"own": own, "relocated": relocated}


def _home() -> Path:
    """The PersonalClaw home as core resolves it (``PERSONALCLAW_HOME``, links resolved)."""
    from personalclaw.sdk.util import config_dir

    return config_dir()


def test_exposes_create_provider():
    assert callable(provider.create_provider)


def test_factory_without_binary_registers_nothing(monkeypatch):
    # On a machine without the CLI on PATH, the bundle registers no entry and the
    # factory returns None (correct: the provider is unavailable there). Never raises.
    monkeypatch.setattr(provider, "resolve_command", lambda provision=False: None)
    assert provider.create_provider({}) is None


def test_the_spawned_claude_runs_with_its_own_config_by_default(operator):
    """🔴 Red on main: with nothing set, the env carried no CLAUDE_CONFIG_DIR at all."""
    env = provider._build_env()
    root = _home() / "cc-config"
    assert env.get("CLAUDE_CONFIG_DIR") == str(root)


def test_the_isolated_config_starts_empty_and_nothing_is_copied_in(operator):
    """🔴 Red on main: the seed copied the operator's settings.json, apiKeyHelper and all."""
    env = provider._build_env()
    settings = Path(env["CLAUDE_CONFIG_DIR"]) / "settings.json"
    assert json.loads(settings.read_text()) == {}
    assert stat.S_IMODE(settings.stat().st_mode) == 0o600
    for untouched in (operator["own"], operator["relocated"]):
        assert json.loads((untouched / "settings.json").read_text()) == PERMISSIVE


def test_a_claude_config_dir_the_operator_set_is_never_the_isolated_root(operator):
    """🔴 Red on main: their own CLAUDE_CONFIG_DIR became the "isolated" root."""
    env = provider._build_env()
    assert env["CLAUDE_CONFIG_DIR"] != str(operator["relocated"])
    assert sorted(p.name for p in operator["relocated"].iterdir()) == ["settings.json"]


def test_what_the_owner_later_puts_in_the_isolated_config_is_kept(operator):
    root = _home() / "cc-config"
    root.mkdir(parents=True)
    (root / "settings.json").write_text('{"model": "their-pick"}\n')
    provider._build_env()
    assert json.loads((root / "settings.json").read_text()) == {"model": "their-pick"}


def test_isolation_off_uses_the_operator_s_own_claude(operator):
    assert "CLAUDE_CONFIG_DIR" not in provider._build_env(isolated=False)
    assert provider._isolated({"isolated_config": False}) is False
    assert provider._isolated({}) is True, "on unless the setting says off"


def test_signing_in_lands_in_the_config_the_spawned_claude_reads(operator, monkeypatch):
    """🔴 Red on main: "claude /login" signed in to ~/.claude, a config the isolated Claude never
    reads, so it stayed signed out."""
    monkeypatch.setattr(provider, "_resolve_claude_exec", lambda: "/opt/bin/claude")
    root = _home() / "cc-config"
    assert provider.login_command() == ["env", f"CLAUDE_CONFIG_DIR={root}", "/opt/bin/claude", "/login"]
    assert provider.login_command(isolated=False) == ["/opt/bin/claude", "/login"]


def test_the_registered_runtime_carries_the_isolated_config_and_its_sign_in(operator, monkeypatch):
    from personalclaw.llm.registry import get_default_registry

    monkeypatch.setattr(provider, "resolve_command", lambda provision=False: ["/opt/bin/claude-agent-acp"])
    monkeypatch.setattr(provider, "_resolve_claude_exec", lambda: "/opt/bin/claude")
    provider.create_provider({})
    entry = get_default_registry().get_entry("acp:claude-code")
    root = str(_home() / "cc-config")
    assert entry.options["env"]["CLAUDE_CONFIG_DIR"] == root
    assert entry.options["login_command"][:2] == ["env", f"CLAUDE_CONFIG_DIR={root}"]

    provider.create_provider({"isolated_config": False})
    entry = get_default_registry().get_entry("acp:claude-code")
    assert "CLAUDE_CONFIG_DIR" not in entry.options.get("env", {})


def test_the_app_text_says_what_the_code_does():
    manifest = json.loads((Path(provider.__file__).parent / "app.json").read_text())
    setting = manifest["provider"]["settingsSchema"]["properties"]["isolated_config"]
    assert setting["default"] is True
    assert "starts empty" in manifest["description"]
    assert "starts" in setting["x-meta"]["help"] and "empty" in setting["x-meta"]["help"]
