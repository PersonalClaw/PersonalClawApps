"""!dashboard slash command — presigned dashboard link via DM + SEL audit
(moved from core tests/test_token_auth.py; the token_auth middleware itself is
core-tested there — this is the Slack command surface)."""

import os
import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# -- URL uses hostname for remote access, localhost for local-only --


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dashboard_url, expected_host",
    [
        ("http://myhostname:8080", "myhostname"),
        ("", "localhost"),  # no URL → localhost-only default
    ],
)
async def test_dashboard_url_host_selection(dashboard_url: str, expected_host: str) -> None:
    """!dashboard sends presigned link via DM, never in channel."""
    from slack_runtime.handler import _handle_slash_command

    slack = MagicMock()
    slack.post_message = AsyncMock(return_value=None)
    slack.open_dm = AsyncMock(return_value="D_DM")
    slack.post_blocks = AsyncMock(return_value=None)
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)

    mock_cfg = MagicMock()
    mock_cfg.dashboard.url = dashboard_url

    expected_port = 8080 if dashboard_url else 10000

    # Unset PERSONALCLAW_PORT so parse_dashboard_url (which reads os.environ at
    # call time) uses the port from the URL or the hard-coded default.
    with (
        patch("slack_runtime.allowlist.AppConfig.load", return_value=mock_cfg),
        patch("personalclaw.dashboard.origin.socket.gethostname", return_value="myhostname"),
        patch("personalclaw.dashboard.origin.socket.gethostbyname", return_value="10.0.0.1"),
        patch("personalclaw.dashboard.origin.socket.getaddrinfo", side_effect=socket.gaierror),
        patch.dict(os.environ, {}, PERSONALCLAW_PORT=""),
        patch("slack_runtime.allowlist.sel") as mock_sel,
    ):
        mock_sel.return_value.log_api_access = MagicMock()
        await _handle_slash_command(
            "!dashboard", slack, sessions, "C123", "ts1", "ts2", "sess1", "U001"
        )

    # Link sent via DM (open_dm called), not in the channel
    slack.open_dm.assert_called_once_with("U001")
    dm_msg = slack.post_message.call_args_list[0][0]
    assert dm_msg[0] == "D_DM"  # sent to DM channel
    assert f"http://{expected_host}:{expected_port}/?token=" in dm_msg[1]


# -- SEL logs contain operation='slack.dashboard_token' with user_id and TTL --


@pytest.mark.asyncio
@pytest.mark.parametrize("duration_arg, expected_ttl", [("", 3600), ("2h", 7200), ("30m", 1800)])
async def test_dashboard_sel_log(duration_arg: str, expected_ttl: int) -> None:
    """!dashboard logs SEL with operation='slack.dashboard_token', caller, and ttl."""
    from slack_runtime.handler import _handle_slash_command

    slack = MagicMock()
    slack.post_message = AsyncMock(return_value=None)
    slack.open_dm = AsyncMock(return_value="D_DM")
    slack.post_blocks = AsyncMock(return_value=None)
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)

    mock_cfg = MagicMock()
    mock_cfg.dashboard.url = ""

    cmd_text = f"!dashboard {duration_arg}".strip()

    with (
        patch("slack_runtime.allowlist.AppConfig.load", return_value=mock_cfg),
        patch("personalclaw.dashboard.origin.socket.gethostname", return_value="myhostname"),
        patch("personalclaw.dashboard.origin.socket.gethostbyname", return_value="10.0.0.1"),
        patch("slack_runtime.allowlist.sel") as mock_sel,
    ):
        mock_log = MagicMock()
        mock_sel.return_value.log_api_access = mock_log
        await _handle_slash_command(
            cmd_text, slack, sessions, "C123", "ts1", "ts2", "sess1", "U_TEST"
        )

    mock_log.assert_called_once_with(
        caller="U_TEST",
        operation="slack.dashboard_token",
        outcome="ok",
        resources=f"ttl={expected_ttl}",
    )
