"""_should_auto_approve_spawn — the Slack handler's spawn auto-approve helper
reading core HookManager config (moved from core tests/test_hooks.py)."""

from unittest.mock import MagicMock

from personalclaw.hooks import HooksConfig


class TestShouldAutoApproveSpawn:
    """Test _should_auto_approve_spawn helper from handler.py."""

    def test_approves_spawn_run_when_flag_true(self):
        from personalclaw.hooks import HookManager
        from slack_runtime.handler import _should_auto_approve_spawn
        ctx = MagicMock()
        ctx.hooks = HookManager(HooksConfig.from_dict({"auto_approve_subagent_spawn": True}))
        assert _should_auto_approve_spawn(ctx, "subagent_run") is True

    def test_rejects_when_flag_false(self):
        from personalclaw.hooks import HookManager
        from slack_runtime.handler import _should_auto_approve_spawn
        ctx = MagicMock()
        ctx.hooks = HookManager(HooksConfig.from_dict({"auto_approve_subagent_spawn": False}))
        assert _should_auto_approve_spawn(ctx, "subagent_run") is False

    def test_rejects_non_spawn_tool(self):
        from personalclaw.hooks import HookManager
        from slack_runtime.handler import _should_auto_approve_spawn
        ctx = MagicMock()
        ctx.hooks = HookManager(HooksConfig.from_dict({"auto_approve_subagent_spawn": True}))
        assert _should_auto_approve_spawn(ctx, "spawn_run_privileged") is False

    def test_rejects_none_context(self):
        from slack_runtime.handler import _should_auto_approve_spawn
        assert _should_auto_approve_spawn(None, "subagent_run") is False

    def test_rejects_none_hooks(self):
        from slack_runtime.handler import _should_auto_approve_spawn
        ctx = MagicMock()
        ctx.hooks = None
        assert _should_auto_approve_spawn(ctx, "subagent_run") is False
