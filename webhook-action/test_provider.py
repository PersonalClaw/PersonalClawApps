"""Webhook action provider routes through the net.fetch egress chokepoint (N1).

The provider-local `_check_ssrf` was deleted; SSRF protection + delivery now come from
`net.fetch(policy=WEBHOOK)`, which also closes the DNS-rebind TOCTOU the old guard had.
These tests pin the guard's resolver (no real DNS) to prove the provider blocks
loopback/IMDS and delivers to a public host.
"""

import asyncio

import pytest

from personalclaw.action_providers.base import ActionContext
from provider import WebhookActionProvider


def _run(coro):
    return asyncio.run(coro)


def _ctx():
    return ActionContext(event="test_event", context="ctx", payload={"k": "v"})


def _fake_dns(mapping):
    """Patch socket.getaddrinfo (what net.guard._resolve calls) to return canned IPs."""
    import socket

    def _gai(host, *a, **k):
        ips = mapping.get(host)
        if ips is None:
            raise socket.gaierror(f"unknown host {host}")
        return [(socket.AF_INET, None, None, "", (ip, 0)) for ip in ips]
    return _gai


def test_webhook_blocks_loopback(monkeypatch):
    """A webhook pointed at a loopback-resolving host is blocked by the egress guard."""
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", _fake_dns({"internal.local": ["127.0.0.1"]}))
    r = _run(WebhookActionProvider().execute({"url": "http://internal.local/hook"}, _ctx()))
    assert r.success is False
    assert "non-public" in (r.error or "").lower()


def test_webhook_blocks_imds(monkeypatch):
    """A webhook pointed at the AWS IMDS link-local address is blocked."""
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", _fake_dns({"metadata.example": ["169.254.169.254"]}))
    r = _run(WebhookActionProvider().execute({"url": "http://metadata.example/latest"}, _ctx()))
    assert r.success is False
    assert "non-public" in (r.error or "").lower()


def test_webhook_delivers_to_public(monkeypatch):
    """A public-resolving host passes the guard and the fetch is attempted (stubbed)."""
    import socket
    import personalclaw.net.client as client
    monkeypatch.setattr(socket, "getaddrinfo", _fake_dns({"example.com": ["93.184.216.34"]}))

    async def fake_fetch(url, **kw):
        return client.FetchResponse(url=url, status=200, headers={}, body=b"ok")
    # execute() lazily does `from personalclaw.sdk.net import fetch as net_fetch`, so
    # patch the SDK re-export the provider actually binds (not personalclaw.net).
    import personalclaw.sdk.net as sdk_net
    monkeypatch.setattr(sdk_net, "fetch", fake_fetch)
    r = _run(WebhookActionProvider().execute({"url": "https://example.com/hook"}, _ctx()))
    assert r.success is True
    assert r.exit_code == 200


def test_webhook_missing_url():
    r = _run(WebhookActionProvider().execute({}, _ctx()))
    assert r.success is False
    assert "url" in (r.error or "").lower()


def test_webhook_headers_json_string_from_form(monkeypatch):
    """The trigger config form renders 'headers' as a TEXT field → a JSON string.
    execute() must parse that (not just accept a dict), or every UI-configured
    webhook with headers would break. Regression for the empty-settingsSchema bug:
    once the schema exposes a headers field, the value arrives as a string."""
    import socket
    import personalclaw.sdk.net as sdk_net
    monkeypatch.setattr(socket, "getaddrinfo", _fake_dns({"example.com": ["93.184.216.34"]}))
    seen = {}

    async def fake_fetch(url, **kw):
        seen.update(kw.get("headers") or {})
        import personalclaw.net.client as client
        return client.FetchResponse(url=url, status=200, headers={}, body=b"ok")
    monkeypatch.setattr(sdk_net, "fetch", fake_fetch)

    r = _run(WebhookActionProvider().execute(
        {"url": "https://example.com/hook", "headers": '{"Authorization": "Bearer T"}'}, _ctx()))
    assert r.success is True
    assert seen.get("Authorization") == "Bearer T"
    assert seen.get("Content-Type") == "application/json"  # default still applied


def test_webhook_headers_malformed_json_errors():
    r = _run(WebhookActionProvider().execute(
        {"url": "https://example.com/hook", "headers": "not json"}, _ctx()))
    assert r.success is False
    assert "headers" in (r.error or "").lower()


def test_settings_schema_exposes_url_field():
    """The manifest's settingsSchema must expose the fields execute() consumes —
    otherwise the trigger UI renders 'no configuration' and url can never be set.
    Reads app.json alongside this test (the source of truth the loader ships)."""
    import json
    import pathlib
    schema = json.loads((pathlib.Path(__file__).parent / "app.json").read_text())["provider"]["settingsSchema"]
    props = schema.get("properties", {})
    assert "url" in props and "url" in schema.get("required", [])
    for f in ("method", "headers", "body_template"):
        assert f in props, f"execute() reads {f} but the form schema omits it"
