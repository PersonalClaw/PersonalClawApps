"""mcp-tools app — the MCP-server tool adapter (McpToolProvider).

Covers the adapter's output-projection integration with core (a huge MCP result is
projected + retained via the shared result store, not dumped raw). The adapter is
app-local (``import provider``); it uses core's projection/result-store + mcp_core
session-key infra."""

from __future__ import annotations

import pytest

import provider


def _isolate_store(tmp_path, monkeypatch):
    import personalclaw.config.loader as cfg
    import personalclaw.session_workspace as ws
    monkeypatch.setattr(cfg, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(ws, "config_dir", lambda: tmp_path)


def test_create_mcp_provider_exposed():
    assert callable(provider.create_mcp_provider)


@pytest.mark.asyncio
async def test_mcp_adapter_projects_large_result(tmp_path, monkeypatch):
    """OP5: an MCP tool returning a huge result is projected + retained, not dumped raw."""
    _isolate_store(tmp_path, monkeypatch)

    # Must exceed _MAX_OUTPUT_CHARS (60k) so projection engages (fail-soft under cap).
    big = "ERROR mcp boom\n" + "noise\n" * 20000

    class _Conn:
        async def call_tool(self, tool, args):
            return True, big

    class _Reg:
        def get(self, server, key):
            return _Conn()

    adapter = provider.McpToolProvider(lambda: _Reg())
    monkeypatch.setattr("personalclaw.mcp_core.get_current_session_key", lambda: "mcp-sess")
    res = await adapter.invoke("mcp/server/bigtool", {})
    assert res.success and len(res.output) < len(big)
    assert res.metadata.get("raw_ref") and "tool_result_get(result_id=" in res.output


# ── what a server's tool is taken to do: its declaration, never its name ──


class _Listing:
    """A registry of one server whose tools carry the annotations given."""

    def __init__(self, tools):
        self._tools = tools

    def items(self):
        tools = self._tools

        class _Conn:
            async def list_tools(self):
                return tools

        return [("acme", _Conn())]


def _spec(name, annotations=None):
    from personalclaw.sdk.mcp import McpToolSpec

    spec = McpToolSpec(name=name, description="d", input_schema={"type": "object"})
    if annotations is not None:
        spec.annotations = annotations
    return spec


async def _risks(monkeypatch, *, trusted):
    from personalclaw import mcp_client

    monkeypatch.setattr(mcp_client, "read_only_labels_trusted", lambda server: trusted)
    listing = _Listing(
        [
            _spec("list_and_archive"),
            _spec("search_docs", {"readOnlyHint": True}),
            _spec("wipe", {"readOnlyHint": False, "destructiveHint": True}),
        ]
    )
    tools = await provider.McpToolProvider(lambda: listing).list_tools()
    return {t.name: t.risk_level.value for t in tools}


@pytest.mark.asyncio
async def test_a_tool_is_not_a_read_because_of_its_name(monkeypatch):
    """Red before: the adapter inferred `safe` from `list_…`, so Ask mode and Trust reads ran
    a tool that archives. A tool that says nothing is a change, and asks."""
    risks = await _risks(monkeypatch, trusted=False)
    assert risks["mcp/acme/list_and_archive"] == "caution"


@pytest.mark.asyncio
async def test_a_read_only_label_counts_only_from_a_server_the_owner_trusts(monkeypatch):
    assert (await _risks(monkeypatch, trusted=False))["mcp/acme/search_docs"] == "caution"
    assert (await _risks(monkeypatch, trusted=True))["mcp/acme/search_docs"] == "safe"


@pytest.mark.asyncio
async def test_a_destructive_label_counts_from_any_server(monkeypatch):
    assert (await _risks(monkeypatch, trusted=False))["mcp/acme/wipe"] == "destructive"
