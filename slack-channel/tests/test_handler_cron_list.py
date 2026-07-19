"""Tests for _handle_cron_command next_run display in Slack keyword handler."""
import re
import time
from unittest.mock import patch

import pytest

from personalclaw.schedule import ScheduleJob, ScheduleDefinition, ScheduleService, make_agent_action
from slack_runtime.handler import _handle_cron_command


@pytest.fixture()
def cron_service(tmp_path):
    return ScheduleService(base_dir=tmp_path)


def _make_job(*, enabled: bool = True, last_status: str = "ok") -> ScheduleJob:
    job = ScheduleJob(
        id="abc123",
        name="test-job",
        action=make_agent_action(message="do something important"),
        schedule=ScheduleDefinition(kind="cron", cron_expr="0 13 * * *"),
        enabled=enabled,
    )
    job.last_status = last_status
    return job


class TestHandleCronListNextRun:
    """Verify cron list keyword includes next run info."""

    def test_includes_next_run(self, cron_service: ScheduleService) -> None:
        cron_service._jobs = [_make_job()]
        now = time.time()
        with patch("slack_runtime.handler.compute_next_run_ts", return_value=now + 7200):
            result = _handle_cron_command("cron list", cron_service, "C123", "t123")
        assert result is not None
        assert "⏭ in" in result
        assert re.search(r"⏭ in \d+h", result)

    def test_no_next_run_for_disabled(self, cron_service: ScheduleService) -> None:
        cron_service._jobs = [_make_job(enabled=False)]
        with patch("slack_runtime.handler.compute_next_run_ts", return_value=None):
            result = _handle_cron_command("cron list", cron_service, "C123", "t123")
        assert result is not None
        assert "⏭" not in result

    def test_next_run_days(self, cron_service: ScheduleService) -> None:
        cron_service._jobs = [_make_job()]
        now = time.time()
        with patch("slack_runtime.handler.compute_next_run_ts", return_value=now + 3 * 86400 + 7200):
            result = _handle_cron_command("cron list", cron_service, "C123", "t123")
        assert result is not None
        assert "⏭ in 3d" in result

    def test_next_run_minutes(self, cron_service: ScheduleService) -> None:
        cron_service._jobs = [_make_job()]
        now = time.time()
        with patch("slack_runtime.handler.compute_next_run_ts", return_value=now + 1800):
            result = _handle_cron_command("cron list", cron_service, "C123", "t123")
        assert result is not None
        assert "⏭ in" in result
        assert re.search(r"⏭ in \d+m", result)

    def test_next_run_less_than_one_minute(self, cron_service: ScheduleService) -> None:
        cron_service._jobs = [_make_job()]
        now = time.time()
        with patch("slack_runtime.handler.compute_next_run_ts", return_value=now + 30):
            result = _handle_cron_command("cron list", cron_service, "C123", "t123")
        assert result is not None
        assert "⏭ in <1m" in result

    def test_next_run_past_due(self, cron_service: ScheduleService) -> None:
        cron_service._jobs = [_make_job()]
        now = time.time()
        with patch("slack_runtime.handler.compute_next_run_ts", return_value=now - 5):
            result = _handle_cron_command("cron list", cron_service, "C123", "t123")
        assert result is not None
        assert "⏭ now" in result

    def test_message_is_redacted(self, cron_service: ScheduleService) -> None:
        job = _make_job()
        job.action = make_agent_action(message="token=AKIAIOSFODNN7EXAMPLE")
        cron_service._jobs = [job]
        with patch("slack_runtime.handler.compute_next_run_ts", return_value=None), \
             patch("slack_runtime.handler.redact_exfiltration_urls",
                   return_value=("[URL_REDACTED]", True)) as mock_url, \
             patch("slack_runtime.handler.redact_credentials",
                   return_value=("[REDACTED]", True)) as mock_cred:
            result = _handle_cron_command("cron list", cron_service, "C123", "t123")
        mock_url.assert_called_once_with(job.message)
        mock_cred.assert_called_once_with("[URL_REDACTED]")
        assert result is not None
        assert "[REDACTED]" in result
        assert "AKIAIOSFODNN7EXAMPLE" not in result
