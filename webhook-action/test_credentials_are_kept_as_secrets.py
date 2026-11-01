"""The Webhook URL and the request headers are credentials, so they are kept as secrets.

A webhook URL is its own credential (whoever has it can post to it), and the headers carry an
Authorization header. Both were plain settings: Configure saved them in the app's settings file
as typed, showed them back in full, and every export carried them. They are declared sensitive,
so PersonalClaw keeps them in its credential store and shows them masked, and a value saved before
moves into the store when PersonalClaw starts. In an automation, the help says to enter them as
``{{secret:NAME}}``, which PersonalClaw fills in only when the event is sent.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from personalclaw.action_providers.base import ActionContext
from personalclaw.providers.settings import ProviderSettings
from provider import WebhookActionProvider

APP = "webhook-action"
BUNDLE = Path(__file__).resolve().parent
URL = "https://example.com/fixture-hook-7c1d9e"
TOKEN = "fixture-token-5e4d3c2b"
HEADERS = json.dumps({"Authorization": f"Bearer {TOKEN}"})


@pytest.fixture
def installed() -> Path:
    """This app installed in the (conftest-isolated) home."""
    home_app = Path(ProviderSettings.config_path(APP)).parent.parent
    home_app.mkdir(parents=True, exist_ok=True)
    shutil.copy(BUNDLE / "app.json", home_app / "app.json")
    return home_app


def _settings_file(home_app: Path) -> str:
    path = home_app / "data" / "config.json"
    return path.read_text(encoding="utf-8") if path.exists() else ""


async def _configure_save(values: dict) -> dict:
    """The Apps page's Configure → Save over core's own routes, then the form's next read."""
    from personalclaw.dashboard.handlers.apps import register_app_routes

    app = web.Application()
    register_app_routes(app)
    async with TestClient(TestServer(app)) as client:
        read = await client.get(f"/api/apps/{APP}/config")
        assert read.status == 200, await read.text()
        revision = (await read.json())["revision"]
        saved = await client.put(
            f"/api/apps/{APP}/config", json=values, headers={"If-Match": f'"{revision}"'}
        )
        assert saved.status == 200, await saved.text()
        return await (await client.get(f"/api/apps/{APP}/config")).json()


def test_configure_keeps_the_url_and_headers_in_the_store_and_shows_them_masked(installed):
    """🔴 Red before: both were written to the settings file as typed and shown back in full."""
    from personalclaw.apps.secret_fields import SECRET_MASK

    shown = asyncio.run(_configure_save({"url": URL, "method": "POST", "headers": HEADERS}))

    stored = _settings_file(installed)
    assert URL not in stored and TOKEN not in stored, stored
    assert shown["config"]["url"] == SECRET_MASK
    assert shown["config"]["headers"] == SECRET_MASK
    assert shown["config"]["method"] == "POST", "a setting that is no credential is shown"
    loaded = ProviderSettings.load(APP)
    assert loaded["url"] == URL and loaded["headers"] == HEADERS


def test_a_value_saved_before_moves_into_the_store_when_personalclaw_starts(installed):
    """🔴 Red before: a URL or header saved by an earlier version stayed in the file."""
    from personalclaw.config.secret_refs import migrate_plaintext_secrets

    data = installed / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "config.json").write_text(json.dumps({"url": URL, "headers": HEADERS}))

    migrate_plaintext_secrets()

    stored = _settings_file(installed)
    assert URL not in stored and TOKEN not in stored, stored
    loaded = ProviderSettings.load(APP)
    assert loaded["url"] == URL and loaded["headers"] == HEADERS


def test_the_help_says_to_enter_a_credential_as_a_secret_reference():
    """🔴 Red before: the help gave a bearer token typed into the headers as its example."""
    props = json.loads((BUNDLE / "app.json").read_text())["provider"]["settingsSchema"][
        "properties"
    ]
    readme = (BUNDLE / "README.md").read_text(encoding="utf-8")
    for field in ("url", "headers"):
        meta = props[field]["x-meta"]
        assert meta.get("sensitive") is True, field
        assert "{{secret:" in meta["help"] and "Settings → Secrets" in meta["help"], field
        assert f"| `{field}` | {meta['label']} | {meta['help']} |" in readme, field


def _fake_dns(mapping):
    import socket

    def _gai(host, *a, **k):
        ips = mapping.get(host)
        if ips is None:
            raise socket.gaierror(f"unknown host {host}")
        return [(socket.AF_INET, None, None, "", (ip, 0)) for ip in ips]

    return _gai


def test_a_secret_reference_in_an_automation_reaches_the_request_filled_in(monkeypatch):
    """What the help says works: core fills each ``{{secret:NAME}}`` as the action is handed its
    config, and the request goes to the URL with the header the secrets hold."""
    import socket

    import personalclaw.net.client as client
    import personalclaw.sdk.net as sdk_net
    from personalclaw.sdk.channel import save_credential
    from personalclaw.triggers.secrets import resolve_for

    save_credential("FIXTURE_HOOK", URL)
    save_credential("FIXTURE_TOKEN", TOKEN)
    monkeypatch.setattr(socket, "getaddrinfo", _fake_dns({"example.com": ["93.184.216.34"]}))
    sent: dict = {}

    async def fake_fetch(url, **kw):
        sent.update(url=url, headers=dict(kw.get("headers") or {}))
        return client.FetchResponse(url=url, status=200, headers={}, body=b"ok")

    monkeypatch.setattr(sdk_net, "fetch", fake_fetch)
    provider = WebhookActionProvider()
    automation = {
        "url": "{{secret:FIXTURE_HOOK}}",
        "headers": '{"Authorization": "Bearer {{secret:FIXTURE_TOKEN}}"}',
    }
    handed, _filled = resolve_for(provider, automation)
    result = asyncio.run(
        provider.execute(handed, ActionContext(event="test_event", context="ctx", payload={}))
    )

    assert result.success is True, result.error
    assert sent["url"] == URL
    assert sent["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert "{{secret:" in json.dumps(automation), "the automation keeps only the names"
