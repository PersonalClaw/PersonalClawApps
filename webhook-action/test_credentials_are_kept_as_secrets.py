"""The Webhook URL and the request headers are credentials, so they are kept as secrets.

A webhook URL is its own credential (whoever has it can post to it), and the headers carry an
Authorization header. Both were plain settings, saved as typed, shown back in full, and carried by
every export. They are declared sensitive, so PersonalClaw keeps them in its credential store and
shows them masked, and a value saved before moves into the store when PersonalClaw starts. The
action is set up on each automation that runs it: core 0.4.2 and later keeps those values on the
automation, in the credential store, and refuses the app-level Configure (it saved values no
automation read). A core before that one keeps them as the app's settings, and the app-level test
reads that instead. In an automation, the help says to enter them as
``{{secret:NAME}}``, which PersonalClaw fills in only when the event is sent.
"""

from __future__ import annotations

import asyncio
import importlib.util
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
LABEL = "HTTP Webhook Hooks"
#: Core 0.4.0 and later keep an automation's credential settings in the store.
HELD_ON_THE_AUTOMATION = importlib.util.find_spec("personalclaw.triggers.held_secrets") is not None


def _configure_is_per_automation() -> bool:
    """Whether this core refuses app-level Configure for an action set up per automation (0.4.2)."""
    from personalclaw.dashboard.handlers import apps

    return hasattr(apps, "_configured_per_automation")


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


@pytest.mark.skipif(
    _configure_is_per_automation(), reason="core 0.4.2 sets this action up per automation"
)
def test_configure_keeps_the_url_and_headers_in_the_store_and_shows_them_masked(installed):
    """🔴 Red before: both were written to the settings file as typed and shown back in full.
    (A core before 0.4.2, which keeps the action's settings as the app's.)"""
    from personalclaw.apps.secret_fields import SECRET_MASK

    shown = asyncio.run(_configure_save({"url": URL, "method": "POST", "headers": HEADERS}))

    stored = _settings_file(installed)
    assert URL not in stored and TOKEN not in stored, stored
    assert shown["config"]["url"] == SECRET_MASK
    assert shown["config"]["headers"] == SECRET_MASK
    assert shown["config"]["method"] == "POST", "a setting that is no credential is shown"
    loaded = ProviderSettings.load(APP)
    assert loaded["url"] == URL and loaded["headers"] == HEADERS


@pytest.fixture
def action(installed, monkeypatch) -> WebhookActionProvider:
    """This app's action registered as the installed app's, as core registers it when it loads."""
    from personalclaw.action_providers import registry

    provider = WebhookActionProvider()
    monkeypatch.setattr(registry, "_providers", {**registry._providers})
    monkeypatch.setattr(registry, "_origins", {**registry._origins})
    registry.register_action_provider(provider, app=APP, app_label=LABEL)
    return provider


@pytest.mark.skipif(not HELD_ON_THE_AUTOMATION, reason="core before 0.4.0 holds no automation's")
def test_an_automation_keeps_the_url_and_headers_in_the_store_and_shows_them_masked(action):
    """What an automation running the action is saved with: the URL and the headers are kept in
    the credential store under the automation's own keys and shown masked (the URL keeps where it
    posts in sight, the rest masked), and the action is handed them as typed."""
    from personalclaw.apps.secret_fields import SECRET_MASK
    from personalclaw.config.loader import config_dir
    from personalclaw.triggers.held_secrets import fill, shown
    from personalclaw.triggers.models import parse_trigger
    from personalclaw.triggers.store import TriggerStore

    config = {"url": URL, "method": "POST", "headers": HEADERS}
    trigger, _issues = parse_trigger(
        {
            "id": "fixture-webhook-automation",
            "kind": "clock",
            "name": "Post the event",
            "spec": {"clock": "interval", "every": "1h"},
            "workflow": {"inline": {"provider": action.name, "config": config}},
        }
    )
    saved = TriggerStore(config_dir()).upsert(trigger).workflow["inline"]["config"]

    on_disk = (Path(config_dir()) / "triggers.json").read_text(encoding="utf-8")
    assert URL not in on_disk and TOKEN not in on_disk, on_disk
    seen = shown(saved)
    assert seen["url"] == f"https://example.com/{SECRET_MASK}"
    assert seen["headers"] == SECRET_MASK
    assert seen["method"] == "POST", "a setting that is no credential is shown"
    handed = fill("fixture-webhook-automation", saved)[0]
    assert handed["url"] == URL and handed["headers"] == HEADERS


@pytest.mark.skipif(
    not _configure_is_per_automation(), reason="core before 0.4.2 keeps it as the app's"
)
def test_app_level_configure_says_where_the_settings_go(installed):
    """Its values are each automation's, so the app-level form is refused with the step to take,
    and nothing is saved."""
    from personalclaw.dashboard.handlers.apps import register_app_routes

    async def configure() -> tuple[int, dict, int, dict]:
        app = web.Application()
        register_app_routes(app)
        async with TestClient(TestServer(app)) as client:
            read = await client.get(f"/api/apps/{APP}/config")
            saved = await client.put(f"/api/apps/{APP}/config", json={"url": URL})
            return read.status, await read.json(), saved.status, await saved.json()

    read, read_body, saved, saved_body = asyncio.run(configure())
    guidance = (
        f"{LABEL} is set up on each automation that runs its action, not on the app. Choose it "
        "as an automation's action on the Automations page, and fill in its fields there."
    )
    assert (read, read_body["error"]) == (409, guidance)
    assert (saved, saved_body["error"]) == (409, guidance)
    assert _settings_file(installed) == "", "nothing was saved"


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
