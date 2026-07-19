"""Standalone smoke test for the openai-tools app (OpenAI tool-schema adapter).

Also covers the #41 migration: outbound HTTP (list_tools discovery + invoke) routes
through the net.fetch egress chokepoint, and the sync connected() probe guards the
operator endpoint through the egress evaluator before any raw request.
"""

from __future__ import annotations

import json

import pytest
import provider

from personalclaw.sdk.tool import ToolProvider


def test_exposes_factory():
    assert callable(provider.create_openai_tool_provider)


def test_provider_is_tool_provider_subclass():
    assert issubclass(provider.OpenAIToolProvider, ToolProvider)


class _FakeFetchResponse:
    def __init__(self, status, payload):
        self.status = status
        self.text = json.dumps(payload) if not isinstance(payload, str) else payload


class _FakeFetch:
    calls: list = []
    status = 200
    payload = {"tools": []}

    async def __call__(self, url, *, policy=None, method="GET", headers=None, data=None):
        body = json.loads(data.decode()) if data else None
        type(self).calls.append({"url": url, "method": method, "headers": headers or {},
                                 "json": body, "policy": policy})
        return _FakeFetchResponse(type(self).status, type(self).payload)


@pytest.fixture
def fake_fetch(monkeypatch):
    fake = _FakeFetch()
    _FakeFetch.calls = []
    _FakeFetch.status = 200
    _FakeFetch.payload = {"tools": []}
    monkeypatch.setattr("personalclaw.net.client.fetch", fake, raising=False)
    monkeypatch.setattr("personalclaw.sdk.net.fetch", fake, raising=False)
    monkeypatch.setattr("personalclaw.net.fetch", fake, raising=False)
    return _FakeFetch


@pytest.mark.asyncio
async def test_list_tools_routes_through_chokepoint(fake_fetch):
    fake_fetch.payload = {"tools": [
        {"name": "echo", "description": "Echo", "parameters": {"type": "object"}},
    ]}
    p = provider.create_openai_tool_provider({"endpoint": "https://tools.example.com", "api_key": "k"})
    tools = await p.list_tools()
    assert [t.name for t in tools] == ["echo"]
    # routed through net.fetch with CONNECTOR + the GET on /tools
    assert fake_fetch.calls[0]["url"] == "https://tools.example.com/tools"
    assert fake_fetch.calls[0]["policy"] is not None
    assert fake_fetch.calls[0]["headers"]["Authorization"] == "Bearer k"


@pytest.mark.asyncio
async def test_list_tools_falls_back_to_v1(monkeypatch):
    # /tools → 404 on the first hop makes the provider retry /v1/tools.
    seen: list[str] = []

    async def _seq_fetch(url, *, policy=None, method="GET", headers=None, data=None):
        seen.append(url)
        status = 404 if url.endswith("/tools") and not url.endswith("/v1/tools") else 200
        return _FakeFetchResponse(status, {"tools": [{"name": "t2", "description": "d"}]})

    monkeypatch.setattr("personalclaw.net.client.fetch", _seq_fetch, raising=False)
    monkeypatch.setattr("personalclaw.sdk.net.fetch", _seq_fetch, raising=False)
    monkeypatch.setattr("personalclaw.net.fetch", _seq_fetch, raising=False)
    p = provider.create_openai_tool_provider({"endpoint": "https://tools.example.com"})
    tools = await p.list_tools()
    assert any(u.endswith("/v1/tools") for u in seen)  # fell back
    assert [t.name for t in tools] == ["t2"]


@pytest.mark.asyncio
async def test_invoke_routes_through_chokepoint(fake_fetch):
    fake_fetch.payload = {"output": "done"}
    p = provider.create_openai_tool_provider({"endpoint": "https://tools.example.com", "api_key": "k"})
    r = await p.invoke("echo", {"msg": "hi"})
    assert r.success is True
    assert "done" in r.output
    inv = [c for c in fake_fetch.calls if c["method"] == "POST"][0]
    assert inv["url"] == "https://tools.example.com/tools/echo"
    assert inv["json"] == {"msg": "hi"}
    assert inv["policy"] is not None


def test_connected_blocks_private_endpoint():
    # The egress guard must classify a loopback/private endpoint as not-connected
    # BEFORE any raw probe (#41 SSRF guard on the operator-configured endpoint).
    p = provider.create_openai_tool_provider({"endpoint": "http://127.0.0.1:9999"})
    assert p.connected is False


def test_connected_false_without_endpoint():
    p = provider.create_openai_tool_provider({})
    assert p.connected is False
