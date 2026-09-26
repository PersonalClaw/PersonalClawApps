"""Mailbox settings saved on the Apps page reach Test, health and send without a restart.

The registry builds the transport once, from the app store as it is at enable time, and the Apps
page's Configure → Save (``PUT /api/apps/{name}/config``) writes the store and re-cycles nothing.
The transport overlaid that build-time dict on the store (so its old host and port won), and the
empty-dict path read a process-wide cache refreshed only by this app's own writes — either way a
saved change never reached the Channels surface until the gateway restarted.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from email_runtime.transport import create_provider
from personalclaw.dashboard.handlers.apps import api_app_config_put
from personalclaw.providers.settings import ProviderSettings
from personalclaw.sdk.channel import save_credential

_APP = "email-channel"
_BUNDLE = Path(__file__).resolve().parents[1]
_MAILBOX = {
    "imap_host": "imap.old.test", "imap_port": 993, "imap_user": "agent@old.test",
    "smtp_host": "smtp.old.test", "smtp_port": 587, "smtp_user": "agent@old.test",
    "address": "agent@old.test",
}


@pytest.fixture
def installed(tmp_path):
    """This app installed in the (conftest-isolated) home, configured for the OLD mailbox."""
    home_app = Path(ProviderSettings.config_path(_APP)).parent.parent
    home_app.mkdir(parents=True, exist_ok=True)
    shutil.copy(_BUNDLE / "app.json", home_app / "app.json")
    ProviderSettings.save(_APP, dict(_MAILBOX))
    save_credential("EMAIL_IMAP_PASS", "app-password")
    return home_app


async def _configure_save(values: dict) -> None:
    """The Apps page's Configure → Save: core's PUT /api/apps/{name}/config handler."""

    class _Request(dict):
        match_info = {"name": _APP}

        async def json(self):
            return values

    resp = await api_app_config_put(_Request())
    assert resp.status == 200, resp.text


@pytest.mark.asyncio
async def test_a_mailbox_saved_after_enable_is_the_one_health_reports(installed):
    transport = create_provider(ProviderSettings.load(_APP))  # what the registry does at enable
    assert "imap.old.test" in (await transport.health())["detail"]

    await _configure_save({**_MAILBOX, "imap_host": "imap.new.test", "smtp_host": "smtp.new.test"})

    detail = (await transport.health())["detail"]
    assert "imap.new.test" in detail and "smtp.new.test" in detail, detail
    assert transport._settings().imap_host == "imap.new.test"


@pytest.mark.asyncio
async def test_an_explicit_instance_config_still_overlays_an_unchanged_store(installed):
    transport = create_provider({"folder": "Agent"})
    assert transport._settings().folder == "Agent"
    assert transport._settings().imap_host == "imap.old.test"  # the rest from the store
