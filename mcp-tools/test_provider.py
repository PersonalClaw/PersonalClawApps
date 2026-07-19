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
