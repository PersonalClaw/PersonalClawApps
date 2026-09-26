"""A Configure form read before a change made in Slack cannot save over that change.

The Apps page's Configure → Save replaces this app's whole settings file with the form, and the
form carries the allowlist, the tracked channels and the per-channel modes along with the tokens.
Slack writes the same file itself: approving someone, tracking a channel or setting a channel's
mode from Slack saves it (``persist_allowed_user``, ``persist_tracking_channel``,
``_persist_channel_config``). A form read before one of those put its older copy back over it,
and the approval silently dropped out of the allowlist the app loads when it starts. Core refuses
such a save now. One built from a copy older than what is stored gets ``409 stale_write``, one that
names no copy gets ``428 revision_required``, and neither writes anything (core
``personalclaw/stale_write.py``).

Driven through core's own routes on a loopback test server, read and saved the way the page does.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from personalclaw.dashboard.handlers.apps import register_app_routes
from personalclaw.providers.settings import ProviderSettings
from slack_runtime.allowlist import persist_allowed_user

_APP = "slack-channel"
_BUNDLE = Path(__file__).resolve().parents[1]
_CONFIG = f"/api/apps/{_APP}/config"
_ADA = {"slack_id": "U0ADA", "name": "Ada"}
_BEFORE, _AFTER = "bot-token-before", "bot-token-after"


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """A scratch home with this app installed and its Bot Token saved."""
    monkeypatch.setenv("PERSONALCLAW_HOME", str(tmp_path))
    for key in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    home_app = tmp_path / "apps" / _APP
    home_app.mkdir(parents=True)
    shutil.copy(_BUNDLE / "app.json", home_app / "app.json")
    ProviderSettings.save(_APP, {"bot_token": _BEFORE})
    return tmp_path


def _based_on(read: dict) -> dict:
    """The header the page's save carries: the revision of the read the form was built from."""
    return {"If-Match": f'"{read["revision"]}"'}


@pytest.mark.asyncio
async def test_a_form_read_before_an_approval_in_slack_cannot_save_over_it(installed):
    app = web.Application()
    register_app_routes(app)
    async with TestClient(TestServer(app)) as client:
        form = await (await client.get(_CONFIG)).json()  # the owner opens Configure
        persist_allowed_user(_ADA["slack_id"], name=_ADA["name"])  # and approves Ada from Slack

        # The form's copy, with the token edited: its allowlist is the one it read, without Ada.
        edited = {**form["config"], "bot_token": _AFTER}
        stale = await client.put(_CONFIG, json=edited, headers=_based_on(form))
        assert stale.status == 409, await stale.text()
        assert (await stale.json())["error"]["code"] == "stale_write"
        # Naming no copy at all is no way around it.
        unnamed = await client.put(_CONFIG, json=edited)
        assert unnamed.status == 428, await unnamed.text()
        assert (await unnamed.json())["error"]["code"] == "revision_required"

        saved = ProviderSettings.load(_APP)
        assert saved["allowed_users"] == [_ADA], "a stale form undid the approval"
        assert saved["bot_token"] == _BEFORE, "a refused save wrote its token"

        # What the page offers instead: read again and re-apply the edit, which keeps both.
        fresh = await (await client.get(_CONFIG)).json()
        resp = await client.put(
            _CONFIG, json={**fresh["config"], "bot_token": _AFTER}, headers=_based_on(fresh)
        )
        assert resp.status == 200, await resp.text()

    saved = ProviderSettings.load(_APP)
    assert saved["allowed_users"] == [_ADA]
    assert saved["bot_token"] == _AFTER
