"""Tests for handler.py: !link-to-dashboard command and linked thread intercept."""

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest


def _make_slack():
    """Create a fully async-mocked Slack client."""
    slack = MagicMock()
    slack.post_message = AsyncMock()
    slack.post_blocks = AsyncMock()
    return slack


# ── !link-to-dashboard command tests ──


class TestLinkToDashboardCommand:
    """Cover handler.py lines 994-1011."""

    @pytest.mark.asyncio
    async def test_no_dashboard_state(self):
        from slack_runtime import handler

        slack = _make_slack()
        with (
            patch.object(handler, "_dashboard_state", None),
            patch.object(handler, "is_allowed_user", return_value=True),
        ):
            result = await handler._handle_slash_command(
                "!link-to-dashboard", slack, MagicMock(), "C1", "t1", "msg1", "t1", "U1",
            )
        assert result == ""
        assert any("not available" in str(c).lower() for c in slack.post_message.call_args_list)

    @pytest.mark.asyncio
    async def test_not_in_thread(self):
        from slack_runtime import handler

        slack = _make_slack()
        ds = MagicMock()
        ds.get_or_create_session = MagicMock()
        with (
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
        ):
            result = await handler._handle_slash_command(
                "!link-to-dashboard", slack, MagicMock(), "C1", "msg1", "msg1", "msg1", "U1",
            )
        assert result == ""
        assert any("thread" in str(c).lower() for c in slack.post_message.call_args_list)

    @pytest.mark.asyncio
    async def test_empty_thread_returns_error(self):
        from slack_runtime import handler

        slack = _make_slack()
        ds = MagicMock()
        with (
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch(
                "slack_runtime.interactions._import_thread_to_session",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await handler._handle_slash_command(
                "!link-to-dashboard", slack, MagicMock(), "C1", "t1", "msg1", "t1", "U1",
            )
        assert result == ""
        assert any("could not" in str(c).lower() for c in slack.post_message.call_args_list)

    @pytest.mark.asyncio
    async def test_unauthorized_user_blocked(self):
        from slack_runtime import handler

        slack = _make_slack()
        with patch.object(handler, "is_allowed_user", return_value=False):
            result = await handler._handle_slash_command(
                "!link-to-dashboard", slack, MagicMock(), "C1", "t1", "msg1", "t1", "UBAD",
            )
        assert result == ""
        assert any("not authorized" in str(c).lower() for c in slack.post_message.call_args_list)

    @pytest.mark.asyncio
    async def test_success_emits_sel_audit(self):
        from slack_runtime import handler

        slack = _make_slack()
        ds = MagicMock()
        session = MagicMock()
        session.key = "s1"
        session.messages = [{"role": "user", "content": "hi"}]
        mock_sel_inst = MagicMock()
        orig_sel = handler.sel
        handler.sel = lambda: mock_sel_inst
        try:
            with (
                patch.object(handler, "_dashboard_state", ds),
                patch.object(handler, "is_allowed_user", return_value=True),
                patch(
                    "slack_runtime.interactions._import_thread_to_session",
                    new_callable=AsyncMock,
                    return_value=session,
                ),
            ):
                result = await handler._handle_slash_command(
                    "!link-to-dashboard", slack, MagicMock(), "C1", "t1", "msg1", "t1", "U1",
                )
        finally:
            handler.sel = orig_sel
        assert result == ""
        mock_sel_inst.log_tool_invocation.assert_called_once()
        kw = mock_sel_inst.log_tool_invocation.call_args[1]
        assert kw["tool_name"] == "link_to_dashboard"
        assert kw["outcome"] == "success"


# ── Linked thread intercept tests ──


class TestLinkedThreadIntercept:
    """Cover handler.py lines 1323-1345."""

    @pytest.mark.asyncio
    async def test_unauthorized_user_denied_with_sel(self):
        from slack_runtime import handler

        slack = _make_slack()
        ds = MagicMock()
        _session = MagicMock(key="session1")
        type(_session).running = PropertyMock(return_value=False)
        ds.get_linked_session = MagicMock(return_value=_session)
        mock_sel_inst = MagicMock()
        orig_sel = handler.sel
        handler.sel = lambda: mock_sel_inst
        try:
            with (
                patch.object(handler, "_dashboard_state", ds),
                patch.object(handler, "is_allowed_user", return_value=False),
            ):
                await handler.handle_message(
                    slack, MagicMock(), "C1", "hello", "t1", "msg1", "UBAD",
                )
                mock_sel_inst.log_tool_invocation.assert_called_once()
                kw = mock_sel_inst.log_tool_invocation.call_args[1]
                assert kw["outcome"] == "denied"
                assert kw["metadata"]["user_id"] == "UBAD"
        finally:
            handler.sel = orig_sel

    @pytest.mark.asyncio
    async def test_authorized_routes_to_session_not_running(self):
        from slack_runtime import handler

        slack = _make_slack()
        session = MagicMock()
        type(session).running = PropertyMock(return_value=False)
        session.key = "session1"
        session._queue = []
        ds = MagicMock()
        ds.get_linked_session = MagicMock(return_value=session)
        ds._background_tasks = set()
        ds.broadcast_ws = MagicMock()
        ds.push_sessions_update = MagicMock()

        with (
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch("personalclaw.sdk.channel._run_chat", new_callable=AsyncMock) as mock_run_chat,
        ):
            await handler.handle_message(
                slack, MagicMock(), "C1", "hello", "t1", "msg1", "U1",
            )
            session.append.assert_called_once()
            mock_run_chat.assert_called_once()
            ds.broadcast_ws.assert_called_once()
            ds.push_sessions_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_redact_for_ui_original_for_llm(self):
        """Verify redacted text goes to UI (session.append) but original goes to LLM (_run_chat)."""
        from slack_runtime import handler

        slack = _make_slack()
        session = MagicMock()
        type(session).running = PropertyMock(return_value=False)
        session.key = "session1"
        session._queue = []
        ds = MagicMock()
        ds.get_linked_session = MagicMock(return_value=session)
        ds._background_tasks = set()
        ds.broadcast_ws = MagicMock()
        ds.push_sessions_update = MagicMock()

        with (
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch("personalclaw.sdk.channel._run_chat", new_callable=AsyncMock) as mock_run_chat,
            patch.object(handler, "redact_exfiltration_urls", return_value=("[REDACTED-URL]", True)),
            patch.object(handler, "redact_credentials", return_value=("[REDACTED]", True)),
        ):
            await handler.handle_message(
                slack, MagicMock(), "C1", "hello http://evil.com", "t1", "msg1", "U1",
            )
            # UI gets redacted text
            session.append.assert_called_once_with("user", "[REDACTED]", "msg msg-u")
            # LLM gets original text
            assert mock_run_chat.call_args[0][2] == "hello http://evil.com"

    @pytest.mark.asyncio
    async def test_authorized_queues_when_running(self):
        from slack_runtime import handler

        slack = _make_slack()
        session = MagicMock()
        type(session).running = PropertyMock(return_value=True)
        session.key = "session1"
        session._queue = []
        session.queue_append = lambda content: (session._queue.append({"id": "test", "content": content}) or "test")
        ds = MagicMock()
        ds.get_linked_session = MagicMock(return_value=session)
        ds.broadcast_ws = MagicMock()
        ds.push_sessions_update = MagicMock()

        with (
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch("personalclaw.sdk.channel._run_chat", new_callable=AsyncMock) as mock_run_chat,
        ):
            await handler.handle_message(
                slack, MagicMock(), "C1", "hello", "t1", "msg1", "U1",
            )
            assert len(session._queue) == 1
            mock_run_chat.assert_not_called()
