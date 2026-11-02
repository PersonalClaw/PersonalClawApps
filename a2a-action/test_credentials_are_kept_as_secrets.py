"""The request headers and the Agent password are credentials, so they are kept as secrets.

The headers carry an Authorization header for an agent that takes a bearer token. They were a
plain setting: Configure saved them in the app's settings file as typed, showed them back in full,
and every export carried them. They are declared sensitive, so PersonalClaw keeps them in its
credential store and shows them masked, and a value saved before moves into the store when
PersonalClaw starts. An agent that signs in with a user name and password gets settings of its own
(Agent user name, and Agent password, kept as a secret), so no password has to sit in the Agent
URL. In an automation, the help says to enter each as ``{{secret:NAME}}``, which PersonalClaw fills
in only when the task is sent.
"""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from personalclaw.action_providers.base import ActionContext
from personalclaw.providers.settings import ProviderSettings
from provider import PLAIN_HTTP_REFUSED, REDIRECT_REFUSED, SIGNED_IN_TWICE, A2AActionProvider

APP = "a2a-action"
BUNDLE = Path(__file__).resolve().parent
URL = "https://agent.example.com/a2a/tasks"
TOKEN = "fixture-token-5e4d3c2b"
PASSWORD = "fixture-pass-7c1d9e"
HEADERS = json.dumps({"Authorization": f"Bearer {TOKEN}"})
API_KEY_HEADER = json.dumps({"X-API-Key": TOKEN})


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


def test_configure_keeps_the_headers_and_password_in_the_store_and_shows_them_masked(installed):
    """🔴 Red before: the headers were written to the settings file as typed and shown back in
    full, and there was no setting for a password but the address itself."""
    from personalclaw.apps.secret_fields import SECRET_MASK

    shown = asyncio.run(
        _configure_save(
            {"url": URL, "headers": HEADERS, "username": "ada", "password": PASSWORD}
        )
    )

    stored = _settings_file(installed)
    assert TOKEN not in stored and PASSWORD not in stored, stored
    assert shown["config"]["headers"] == SECRET_MASK
    assert shown["config"]["password"] == SECRET_MASK
    assert shown["config"]["url"] == URL and shown["config"]["username"] == "ada"
    loaded = ProviderSettings.load(APP)
    assert loaded["headers"] == HEADERS and loaded["password"] == PASSWORD


def test_headers_saved_before_move_into_the_store_when_personalclaw_starts(installed):
    """🔴 Red before: headers saved by an earlier version stayed in the file."""
    from personalclaw.config.secret_refs import migrate_plaintext_secrets

    data = installed / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "config.json").write_text(json.dumps({"url": URL, "headers": HEADERS}))

    migrate_plaintext_secrets()

    assert TOKEN not in _settings_file(installed)
    assert ProviderSettings.load(APP)["headers"] == HEADERS


def test_the_help_says_where_a_credential_goes():
    """🔴 Red before: the help gave a bearer token typed into the headers as its example."""
    props = json.loads((BUNDLE / "app.json").read_text())["provider"]["settingsSchema"][
        "properties"
    ]
    for field in ("headers", "password"):
        meta = props[field]["x-meta"]
        assert meta.get("sensitive") is True, field
        assert "{{secret:" in meta["help"] and "Settings → Secrets" in meta["help"], field
    assert "Agent password" in props["url"]["x-meta"]["help"]
    assert props["password"]["x-meta"]["label"] == "Agent password"


def _sent(monkeypatch) -> dict:
    """Stub the SDK fetch `execute` binds, recording what the request was sent with."""
    import personalclaw.net.client as client
    import personalclaw.sdk.net as sdk_net

    sent: dict = {}
    reply = json.dumps({"kind": "task", "status": {"state": "completed"}}).encode("utf-8")

    async def fake_fetch(url, **kw):
        sent.update(url=url, headers=dict(kw.get("headers") or {}), policy=kw.get("policy"))
        return client.FetchResponse(url=url, status=200, headers={}, body=reply)

    monkeypatch.setattr(sdk_net, "fetch", fake_fetch)
    return sent


def _ctx() -> ActionContext:
    return ActionContext(event="test_event", context="ctx", payload={})


def test_secret_references_in_an_automation_reach_the_request_filled_in(monkeypatch):
    """What the help says works: core fills each ``{{secret:NAME}}`` as the action is handed its
    config, and the request carries what the secrets hold."""
    from personalclaw.sdk.channel import save_credential
    from personalclaw.triggers.secrets import resolve_for

    save_credential("FIXTURE_TOKEN", TOKEN)
    save_credential("FIXTURE_PASS", PASSWORD)
    provider = A2AActionProvider()

    sent = _sent(monkeypatch)
    bearer = {"url": URL, "headers": '{"Authorization": "Bearer {{secret:FIXTURE_TOKEN}}"}'}
    result = asyncio.run(provider.execute(resolve_for(provider, bearer)[0], _ctx()))
    assert result.success is True, result.error
    assert sent["headers"]["Authorization"] == f"Bearer {TOKEN}"

    sent = _sent(monkeypatch)
    basic = {"url": URL, "username": "ada", "password": "{{secret:FIXTURE_PASS}}"}
    result = asyncio.run(provider.execute(resolve_for(provider, basic)[0], _ctx()))
    assert result.success is True, result.error
    expected = base64.b64encode(f"ada:{PASSWORD}".encode()).decode()
    assert sent["headers"]["Authorization"] == f"Basic {expected}"
    assert sent["url"] == URL, "the password never goes into the address"


def test_a_password_and_an_authorization_header_together_are_refused(monkeypatch):
    sent = _sent(monkeypatch)
    result = asyncio.run(
        A2AActionProvider().execute(
            {"url": URL, "headers": HEADERS, "password": PASSWORD}, _ctx()
        )
    )
    assert result.success is False and result.error == SIGNED_IN_TWICE
    assert sent == {}, "nothing was sent"


@pytest.mark.parametrize(
    "signs_in",
    [{"password": PASSWORD}, {"headers": HEADERS}, {"headers": API_KEY_HEADER}],
    ids=["password", "authorization-header", "a-header-under-another-name"],
)
def test_a_task_that_signs_in_is_never_sent_over_plain_http_to_another_machine(
    monkeypatch, signs_in
):
    """🔴 Red before: the password or a header went to a plain http Agent URL unencrypted. Every
    header counts: Headers is kept as a secret, so a key under any name is a credential."""
    sent = _sent(monkeypatch)
    result = asyncio.run(
        A2AActionProvider().execute({"url": "http://agent.example.com/a2a", **signs_in}, _ctx())
    )
    assert result.success is False and result.error == PLAIN_HTTP_REFUSED
    assert sent == {}, "nothing was sent"


def test_a_task_that_signs_in_may_use_plain_http_on_this_machine(monkeypatch):
    sent = _sent(monkeypatch)
    result = asyncio.run(
        A2AActionProvider().execute(
            {"url": "http://127.0.0.1:8099/a2a", "password": PASSWORD}, _ctx()
        )
    )
    assert result.success is True, result.error
    assert sent["policy"].max_redirects == 0


def test_a_task_that_signs_in_is_held_to_https_and_never_redirected(monkeypatch):
    """🔴 Red before: core's guarded client follows a redirect re-sending every header, so the
    password or token went with it to wherever the agent pointed, plain http included."""
    sent = _sent(monkeypatch)
    asyncio.run(A2AActionProvider().execute({"url": URL, "headers": HEADERS}, _ctx()))
    assert sent["policy"].max_redirects == 0
    assert sent["policy"].allow_schemes == ("https",)


def test_a_header_under_another_name_holds_a_task_to_https_and_no_redirect(monkeypatch):
    """🔴 Red before: only an Authorization header counted, so an API key under another name went
    on through a redirect, to another host or plain http."""
    sent = _sent(monkeypatch)
    asyncio.run(A2AActionProvider().execute({"url": URL, "headers": API_KEY_HEADER}, _ctx()))
    assert sent["headers"]["X-API-Key"] == TOKEN
    assert sent["policy"].max_redirects == 0
    assert sent["policy"].allow_schemes == ("https",)


def test_a_task_that_does_not_sign_in_keeps_cores_policy(monkeypatch):
    from personalclaw.sdk.net import a2a_outbound_policy

    sent = _sent(monkeypatch)
    asyncio.run(A2AActionProvider().execute({"url": URL}, _ctx()))
    assert sent["policy"].max_redirects == a2a_outbound_policy().max_redirects


def test_a_redirect_a_signed_in_task_meets_is_refused_in_its_own_words(monkeypatch):
    """What the guarded client answers to a redirect it was told not to follow reaches the run as
    a sentence that says why and what to do."""
    import personalclaw.sdk.net as sdk_net
    from personalclaw.net.guard import GuardDecision

    async def redirected(url, **kw):
        raise sdk_net.EgressBlocked(
            GuardDecision(allow=False, url=url, reason="too many redirects (> 0)")
        )

    monkeypatch.setattr(sdk_net, "fetch", redirected)
    result = asyncio.run(A2AActionProvider().execute({"url": URL, "password": PASSWORD}, _ctx()))
    assert result.success is False and result.error == REDIRECT_REFUSED
