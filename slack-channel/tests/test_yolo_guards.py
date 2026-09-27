"""YOLO trust-mode delegation through the Slack handler — TTL tiers,
config-permanent guards, the !yolo command noop paths (moved from core
tests/test_shepherd_fixes.py; the canonical state lives in core
personalclaw.trust_mode, the handler is a thin delegator).

Config-driven YOLO stands only while the config sets ``agent.yolo``: core reads it back on every
check, so taking it out of the config ends it however that happened. The gateway turns it on at
startup only when the config says so (``events.py``: ``if orch._cfg.agent.yolo:
set_yolo_mode(True)``), so a test of config YOLO starts the same way — :func:`_start_config_yolo`
writes the setting, then turns it on."""

from unittest.mock import patch

import pytest

import personalclaw.trust_mode as tm


def _config_yolo(on: bool) -> None:
    """``agent.yolo`` in this test's own home, written the way the Settings switch writes it."""
    from personalclaw.sdk.channel import AppConfig

    cfg = AppConfig.load()
    cfg.agent.yolo = on
    cfg.save()


def _start_config_yolo() -> None:
    """What the gateway does at startup for ``agent.yolo: true``: the config says it, then the
    handler turns it on."""
    from slack_runtime.handler import set_yolo_mode

    _config_yolo(True)
    set_yolo_mode(True)


class TestYoloExpiry:
    """Tests for YOLO mode tiered auto-timeout."""

    @pytest.fixture(autouse=True)
    def _reset_yolo(self):
        from slack_runtime.handler import disable_yolo
        disable_yolo()
        yield
        disable_yolo()

    def test_slack_yolo_expires(self) -> None:
        """!yolo on expires after _YOLO_TTL_SECS (30min)."""
        import slack_runtime.handler as h
        from slack_runtime.handler import enable_yolo_with_ttl, is_yolo_mode

        enable_yolo_with_ttl(h._YOLO_TTL_SECS)

        assert is_yolo_mode()

        future = tm._TRUST._expires_at + 1
        with patch("time.monotonic", return_value=future):
            assert not is_yolo_mode(), "Slack YOLO should have auto-expired"

    def test_config_yolo_never_expires(self) -> None:
        """set_yolo_mode from config sets no expiry."""
        import slack_runtime.handler as h
        from slack_runtime.handler import is_yolo_mode

        _start_config_yolo()

        assert is_yolo_mode()
        assert tm._TRUST._expires_at == 0.0  # no expiry

        # Even far in the future, still active
        with patch("time.monotonic", return_value=9999999999.0):
            assert is_yolo_mode(), "Config YOLO should never expire"

    def test_dashboard_yolo_expires_6h(self) -> None:
        """Dashboard YOLO uses _YOLO_DASHBOARD_TTL_SECS (6h)."""
        import slack_runtime.handler as h
        from slack_runtime.handler import (
            _YOLO_DASHBOARD_TTL_SECS,
            enable_yolo_with_ttl,
            is_yolo_mode,
        )

        enable_yolo_with_ttl(_YOLO_DASHBOARD_TTL_SECS)

        assert is_yolo_mode()
        assert tm._TRUST._expires_at > 0

        future = tm._TRUST._expires_at + 1
        with patch("time.monotonic", return_value=future):
            assert not is_yolo_mode(), "Dashboard YOLO should expire after 6h"

    def test_yolo_disable_clears(self) -> None:
        from slack_runtime.handler import disable_yolo, is_yolo_mode

        _start_config_yolo()
        assert is_yolo_mode()
        disable_yolo()
        assert not is_yolo_mode()


class TestYoloSlackCommandPath:
    """Guard test for the !yolo on Slack command path (handler.py)."""

    def test_enable_yolo_with_ttl_sets_expiry(self) -> None:
        import slack_runtime.handler as h
        from slack_runtime.handler import disable_yolo, enable_yolo_with_ttl

        disable_yolo()
        enable_yolo_with_ttl(h._YOLO_TTL_SECS)

        assert tm._TRUST._active is True
        assert tm._TRUST._expires_at > 0
        assert tm._TRUST._active_ttl == h._YOLO_TTL_SECS

        disable_yolo()
        assert tm._TRUST._expires_at == 0.0


class TestYoloFromConfigGuard:
    """Tests for _yolo_from_config flag.

    Bug 1: !yolo on overwrites config-permanent yolo with 30-min TTL.
    Bug 2: dashboard enable_yolo() sets 6h TTL on config-driven yolo.
    """

    @pytest.fixture(autouse=True)
    def _reset_yolo(self):
        from slack_runtime.handler import disable_yolo
        disable_yolo()
        yield
        disable_yolo()

    def test_config_yolo_sets_from_config_flag(self) -> None:
        import slack_runtime.handler as h

        _start_config_yolo()
        assert tm._TRUST._from_config is True
        assert tm._TRUST._expires_at == 0.0

    def test_enable_with_ttl_noop_when_config_active(self) -> None:
        """Bug 1: !yolo on must not overwrite config-permanent yolo."""
        import slack_runtime.handler as h
        from slack_runtime.handler import enable_yolo_with_ttl, is_yolo_mode

        _start_config_yolo()
        enable_yolo_with_ttl(h._YOLO_TTL_SECS)

        assert is_yolo_mode()
        assert tm._TRUST._expires_at == 0.0, "Config yolo should remain permanent"

    def test_config_yolo_survives_far_future(self) -> None:
        import slack_runtime.handler as h
        from slack_runtime.handler import is_yolo_mode

        _start_config_yolo()
        # Force a truthy, past expiry so the _yolo_from_config guard is actually exercised
        tm._TRUST._expires_at = 1.0
        with patch("time.monotonic", return_value=9999999999.0):
            assert is_yolo_mode(), "Config YOLO must never expire"

    def test_disable_clears_from_config_flag(self) -> None:
        import slack_runtime.handler as h
        from slack_runtime.handler import disable_yolo

        _start_config_yolo()
        assert tm._TRUST._from_config is True
        disable_yolo()
        assert tm._TRUST._from_config is False

    def test_set_yolo_mode_false_clears_flag(self) -> None:
        import slack_runtime.handler as h
        from slack_runtime.handler import set_yolo_mode

        _start_config_yolo()
        set_yolo_mode(False)
        assert tm._TRUST._from_config is False


class TestYoloFromConfigSlackGuards:
    """Cover _yolo_from_config early-return paths in events.py and handler.py."""

    @pytest.fixture(autouse=True)
    def _reset_yolo(self):
        from slack_runtime.handler import disable_yolo
        disable_yolo()
        yield
        disable_yolo()

    @pytest.mark.asyncio
    async def test_events_yolo_on_noop_when_config_permanent(self) -> None:
        """events.py: /personalclaw yolo on responds with noop when config-permanent."""
        from unittest.mock import AsyncMock, MagicMock

        import slack_runtime.handler as h

        _start_config_yolo()
        assert tm._TRUST._from_config is True

        orch = MagicMock()
        respond = AsyncMock()

        from slack_runtime.events import _handle_yolo

        with patch("slack_runtime.events.sel") as mock_sel, patch("slack_runtime.events.is_owner", return_value=True):
            await _handle_yolo(orch, "UOWNER", "on", respond)

        respond.assert_awaited_once()
        assert "permanently ON" in respond.call_args[0][0]
        mock_sel.return_value.log_api_access.assert_called_once()
        assert mock_sel.return_value.log_api_access.call_args.kwargs["outcome"] == "noop_config_permanent"

    @pytest.mark.asyncio
    async def test_handler_yolo_on_noop_when_config_permanent(self) -> None:
        """handler.py: !yolo on responds with noop when config-permanent."""
        from unittest.mock import AsyncMock, MagicMock

        import slack_runtime.handler as h
        from slack_runtime.handler import _handle_slash_command

        _start_config_yolo()
        assert tm._TRUST._from_config is True

        slack = AsyncMock()
        sessions = MagicMock()

        with patch("slack_runtime.handler.sel") as mock_sel, patch("slack_runtime.handler.is_owner", return_value=True):
            result = await _handle_slash_command("!yolo on", slack, sessions, "C123", "ts1", "ts2", "key1", "UOWNER")

        assert result is not None
        slack.post_message.assert_awaited()
        msg = slack.post_message.call_args[0][1]
        assert "permanently ON" in msg
        sel_call = [c for c in mock_sel.return_value.log_api_access.call_args_list if c.kwargs.get("outcome") == "noop_config_permanent"]
        assert len(sel_call) == 1

    @pytest.mark.asyncio
    async def test_handler_yolo_on_after_the_config_dropped_it(self) -> None:
        """Config YOLO taken out of the config (a rollback, ``personalclaw config set``, a hand
        edit) has ended, so ``!yolo on`` turns on a timed one instead of saying it is permanent."""
        from unittest.mock import AsyncMock, MagicMock

        import slack_runtime.handler as h
        from slack_runtime.handler import _handle_slash_command, is_yolo_mode

        _start_config_yolo()
        _config_yolo(False)

        slack = AsyncMock()
        with patch("slack_runtime.handler.sel") as mock_sel, patch("slack_runtime.handler.is_owner", return_value=True):
            await _handle_slash_command("!yolo on", slack, MagicMock(), "C123", "ts1", "ts2", "key1", "UOWNER")

        msg = slack.post_message.call_args[0][1]
        assert "permanently ON" not in msg
        assert f"auto-expires in {h._YOLO_TTL_SECS // 60}min" in msg
        assert is_yolo_mode() and not tm.yolo_from_config()
        outcomes = [c.kwargs.get("outcome") for c in mock_sel.return_value.log_api_access.call_args_list]
        assert "allowed" in outcomes and "noop_config_permanent" not in outcomes
