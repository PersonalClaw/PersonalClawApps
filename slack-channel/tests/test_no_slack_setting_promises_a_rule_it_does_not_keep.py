"""The Slack Configure form offers no access rule the app does not keep.

Two fields promised one. **Open Channels**, "Channel IDs where all users are authorized without
allowlist", was read into a set that a check hard-coded to refuse every channel consulted. **Allowed
Enterprise IDs**, "Slack Enterprise Grid org IDs to allow for workspace validation", was handed to the
workspace check, which said it accepted the list "for call-site compatibility" and gated nothing. An
owner who filled either believed the bot was open to a channel, or kept to an organisation, and it
was neither. Both fields are gone, with their setting, the parameter and the state behind them.
Who may talk to the bot is its owner and Allowed Users, in the one workspace its bot token belongs
to; a store an earlier release wrote with either key still loads, and lets nobody in by it.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import fields
from pathlib import Path

import pytest

from personalclaw.sdk.channel import ProviderSettings, owner_id_credential

import slack_runtime.enterprise as enterprise
import slack_runtime.handler as H
import slack_runtime.runtime as runtime_mod
import slack_runtime.settings as settings_mod
from slack_runtime.settings import SlackSettings, reload_settings

_APP_DIR = Path(__file__).resolve().parents[1]
_GONE = ("open_channels", "allowed_enterprise_ids")


def _schema() -> dict:
    manifest = json.loads((_APP_DIR / "app.json").read_text(encoding="utf-8"))
    return manifest["provider"]["settingsSchema"]["properties"]


@pytest.mark.parametrize("key", _GONE)
def test_the_configure_form_offers_neither_field(key):
    assert key not in _schema()
    assert key not in {f.name for f in fields(SlackSettings)}
    assert key not in settings_mod._OWNED_KEYS


def test_the_workspace_check_takes_no_org_list():
    assert list(inspect.signature(enterprise.validate_enterprise).parameters) == ["bot_token"]
    assert not hasattr(SlackSettings, "enterprise_ids")
    assert not hasattr(enterprise, "_validated_enterprise_id")


def test_no_open_channel_state_is_left():
    for name in ("is_open_channel", "set_open_channels", "_open_channels"):
        assert not hasattr(H, name), name
    assert "_open_channels" not in inspect.getsource(runtime_mod.SlackRuntime.__init__)


def test_a_store_an_earlier_release_wrote_still_loads_and_lets_nobody_in(monkeypatch):
    """Both keys saved by the old form: the store loads, and a stranger posting in the channel
    the form called open is refused like anywhere else."""
    ProviderSettings.update(
        "slack-channel",
        {"open_channels": ["C0OPENROOM"], "allowed_enterprise_ids": ["E0EXAMPLE"]},
    )

    settings = reload_settings()

    assert not any(hasattr(settings, key) for key in _GONE)
    monkeypatch.setenv(owner_id_credential("slack"), "U0NOOROWNER")
    monkeypatch.setattr(H, "_allowed_users", set())
    assert H.is_allowed_user("U0STRANGER") is False
    assert H.is_allowed_user("U0NOOROWNER") is True, "vacuity floor: the owner is let in"
