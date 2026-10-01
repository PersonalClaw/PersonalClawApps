"""The claude-code-agent app: it loads, and the Claude it spawns runs with a config of its own.

Measured before this was written:

* Isolation was opt-in (``PERSONALCLAW_CC_ISOLATE=1``) and off by default, so the spawned Claude
  read the operator's ``~/.claude`` and kept every ``permissions.allow`` rule and
  ``defaultMode`` in it, while the app's description said an isolated ``CLAUDE_CONFIG_DIR``
  stripped them.
* With it on, the "isolated" config was seeded from the real ``~/.claude/settings.json``, and a
  ``CLAUDE_CONFIG_DIR`` the operator had set became the isolated root and was rewritten.
* With it on, the session still loaded the settings of the folder Claude works in.
  ``CLAUDE_CONFIG_DIR`` moves only the user scope; the ACP adapter asks the Agent SDK for the
  ``user``, ``project`` and ``local`` setting sources unless the session's
  ``_meta.claudeCode.options.settingSources`` says otherwise. A repository's
  ``.claude/settings.json`` and ``.claude/settings.local.json`` and its ``.mcp.json`` therefore
  applied: hooks and the ``env`` block are used, and ``.mcp.json`` servers connected without
  asking, in an SDK session in a folder nobody trusted, as Claude Code's permissions
  documentation says ("What runs before you trust a folder"), as did an untracked local file's
  allow rules. Claude Code documents the answer for a repository you did not write: load the
  user source only.
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

import provider

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # the repo root: apps_testkit

from apps_testkit.acp_env import (  # noqa: E402
    PLANTED_SECRETS,
    acp_stub_command,
    handed_env,
    opened_sessions,
    stub_command,
)

#: What an isolated session asks the adapter for: Claude Code's ``user`` setting source alone,
#: which ``CLAUDE_CONFIG_DIR`` points at the isolated config. Nothing from the folder it works in.
ISOLATED_SESSION = {"claudeCode": {"options": {"settingSources": ["user"]}}}

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


def test_an_isolated_session_loads_none_of_the_folder_s_settings(operator, monkeypatch):
    """🔴 Red before: the entry carried no session options, so every session loaded the
    ``project`` and ``local`` setting sources of the folder Claude was pointed at."""
    from personalclaw.llm.registry import get_default_registry

    monkeypatch.setattr(provider, "resolve_command", lambda provision=False: ["/opt/bin/claude-agent-acp"])
    monkeypatch.setattr(provider, "_resolve_claude_exec", lambda: "/opt/bin/claude")
    provider.create_provider({})
    assert get_default_registry().get_entry("acp:claude-code").options["session_meta"] == ISOLATED_SESSION

    # Off, Claude loads what it loads anywhere: your own ~/.claude and the folder's settings.
    provider.create_provider({"isolated_config": False})
    assert "session_meta" not in get_default_registry().get_entry("acp:claude-code").options


def test_the_session_claude_is_asked_for_carries_the_isolation(operator, monkeypatch, tmp_path):
    """🔴 Red before: the session/new a chat sends asked for no setting sources, so the adapter
    loaded all three. A stub ACP agent in the adapter's place, started from the entry the app
    registers by core's runtime, records the session it is asked for and what it was handed."""
    command, record = acp_stub_command(tmp_path)
    monkeypatch.setattr(provider, "resolve_command", lambda provision=False: command)
    monkeypatch.setattr(provider, "_resolve_claude_exec", lambda: "/opt/bin/claude")
    provider.create_provider({})

    handed, sessions, modes = opened_sessions("acp:claude-code", record)

    assert sessions, "the stub was never asked for a session"
    assert [s.get("_meta") for s in sessions] == [ISOLATED_SESSION] * len(sessions)
    assert handed["CLAUDE_CONFIG_DIR"] == str(_home() / "cc-config"), "the user source is its own"
    # The adapter starts a session in the mode the settings it reads name (its own resolution
    # reads every source, whatever the session asks the CLI for), and the stub starts this one
    # approving edits itself. Each tool asks here only because the host then sets the mode that
    # asks, on every session, before any turn.
    assert modes == ["default"] * len(sessions)


def test_the_app_text_says_what_the_code_does():
    manifest = json.loads((Path(provider.__file__).parent / "app.json").read_text())
    setting = manifest["provider"]["settingsSchema"]["properties"]["isolated_config"]
    help_text = setting["x-meta"]["help"]
    readme = (Path(provider.__file__).parent / "README.md").read_text()
    assert setting["default"] is True
    assert "starts empty" in manifest["description"]
    assert "starts" in help_text and "empty" in help_text
    # Isolated: nothing from the folder it works in either, CLAUDE.md included. Off: the folder's
    # own settings apply too, and say what they can do.
    for text in (manifest["description"], help_text):
        assert "folder it works in" in text
        assert "CLAUDE.md" in text
    on, off = help_text.split("Off:")
    assert "nothing from the folder it works in" in on
    assert "the folder's own Claude settings" in off and "hooks and MCP servers" in off
    assert "nothing from the folder it works in" in readme
    assert "`--setting-sources user`" in readme or "`settingSources`" in readme


#: What an owner running Claude Code on Amazon Bedrock has in the gateway's environment.
BEDROCK = {
    "CLAUDE_CODE_USE_BEDROCK": "1",
    "AWS_PROFILE": "work",
    "AWS_REGION": "us-west-2",
    "ANTHROPIC_MODEL": "a-bedrock-model-id",
}


def test_the_cli_is_handed_the_variables_that_pick_its_provider(operator, monkeypatch, tmp_path):
    """🔴 Red on main: an ACP CLI gets no variable of the gateway's its app does not declare, and
    this app declared none, so Claude Code on Bedrock lost its selection and ran on its own
    default provider. A stub in Claude's place, spawned from the entry the app registers."""
    from personalclaw.llm.registry import get_default_registry

    for name, value in {**BEDROCK, **PLANTED_SECRETS, "UNRELATED_SETTING": "x"}.items():
        monkeypatch.setenv(name, value)
    command, record = stub_command(tmp_path)
    monkeypatch.setattr(provider, "resolve_command", lambda provision=False: command)
    monkeypatch.setattr(provider, "_resolve_claude_exec", lambda: "/opt/bin/claude")
    provider.create_provider({})

    handed = handed_env(get_default_registry().get_entry("acp:claude-code"), record, tmp_path / "w")
    assert {name: handed.get(name) for name in BEDROCK} == BEDROCK
    assert sorted(set(PLANTED_SECRETS) & set(handed)) == []
    assert "UNRELATED_SETTING" not in handed
    assert handed["CLAUDE_CONFIG_DIR"] == str(_home() / "cc-config"), "still its own config"


def test_the_readme_names_every_variable_the_app_passes():
    readme = (Path(provider.__file__).parent / "README.md").read_text()
    assert provider.PROVIDER_ENV
    assert [name for name in provider.PROVIDER_ENV if f"`{name}`" not in readme] == []


def test_install_consent_names_what_it_starts_installs_and_writes():
    """Install consent names the program this app has core start and the npm package core
    installs for it, from the manifest, and core does neither for an app whose manifest does
    not declare it. So the manifest says what the code does: the CLI it resolves, and the
    adapter package it asks for."""
    from personalclaw.sdk.manifest import AppManifest

    manifest = AppManifest.from_dict(
        json.loads((Path(provider.__file__).parent / "app.json").read_text())
    )
    assert manifest.validate() == []
    assert [p.program for p in manifest.launches] == provider._CLAUDE_BIN_NAMES
    assert manifest.dependencies.npmPackages == [provider._ACP_NPM_PKG]
    # Your own Claude sign-in, settings and rules, and the settings of the folder it works in,
    # come along only with isolation off.
    [claude] = manifest.launches
    assert claude.inherits == ["sign-in", "settings", "auto-approve-rules", "folder-settings"]
    assert claude.inheritsWhile is not None
    assert (claude.inheritsWhile.setting, claude.inheritsWhile.value) == (
        provider._ISOLATED_SETTING,
        False,
    )
    # The isolated config is the one place outside its own folder the app writes.
    assert [w.path for w in manifest.writes] == [provider._claude_config_root().name]
