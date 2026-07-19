"""MCP adapter for the ToolProvider interface.

Surfaces tools from external MCP servers (configured in
``~/.personalclaw/mcp.json``) through the unified ``ToolProvider`` abstraction so
they are discoverable on the Tools page and invocable by the native agent loop.
Backed by :mod:`personalclaw.mcp_client` — a long-lived stdio/SSE MCP client. The
client requires the optional ``mcp`` SDK (``personalclaw[mcp]``); without it the
registry is ``None`` and this provider lists zero tools cleanly.

Tool names are namespaced ``mcp/<server>/<tool>`` so they never collide with the
builtin/in-process core tools, and ``invoke`` routes back to the owning server.
"""

from typing import Any

from personalclaw.sdk.tool import RiskLevel, ToolDefinition, ToolProvider, ToolResult

_TOOL_PREFIX = "mcp"


def create_mcp_provider(config: dict[str, Any] | None = None) -> "McpToolProvider":
    """Factory for the extension system. Returns the MCP adapter bound to the
    live in-process MCP client registry (``None`` when the SDK isn't installed)."""
    from personalclaw.sdk.mcp import get_mcp_client_registry

    return McpToolProvider(get_mcp_client_registry)


class McpToolProvider(ToolProvider):
    """Surfaces tools from all connected external MCP servers."""

    def __init__(self, get_mcp_registry_fn):
        self._get_registry = get_mcp_registry_fn

    @property
    def name(self) -> str:
        return "mcp"

    @property
    def display_name(self) -> str:
        return "MCP Servers"

    @property
    def connected(self) -> bool:
        return self._get_registry() is not None

    async def list_tools(self) -> list[ToolDefinition]:
        registry = self._get_registry()
        if not registry:
            return []
        from personalclaw.sdk.mcp import infer_risk_from_name

        tools: list[ToolDefinition] = []
        for server_name, conn in registry.items():
            for tool in await conn.list_tools():
                # MCP tools declare no risk (the spec's readOnlyHint/destructiveHint
                # annotations aren't plumbed through McpToolSpec yet), so infer a
                # declared risk from the tool name — a `*_delete`/`*_send` reads as
                # caution/destructive instead of silently defaulting SAFE. The
                # approval gate still downgrades read-only invocations per call.
                tools.append(
                    ToolDefinition(
                        name=f"{_TOOL_PREFIX}/{server_name}/{tool.name}",
                        description=tool.description,
                        provider="mcp",
                        parameters=tool.input_schema,
                        requires_approval=True,
                        risk_level=RiskLevel(infer_risk_from_name(tool.name)),
                    )
                )
        return tools

    async def invoke(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        registry = self._get_registry()
        if not registry:
            return ToolResult(success=False, error="MCP client unavailable (install the 'mcp' extra)")

        # Expected shape: mcp/<server>/<tool>. Tool ids may themselves contain a
        # slash, so split into at most 3 parts and validate the prefix.
        parts = tool_name.split("/", 2)
        if len(parts) != 3 or parts[0] != _TOOL_PREFIX:
            return ToolResult(success=False, error=f"Invalid MCP tool name: {tool_name}")
        _, server_name, tool = parts

        # Route to the caller's session-scoped connection for stateful servers, so
        # one session's browser/shell state can't leak into another's. Poolable
        # servers ignore the key and share one connection (registry decides).
        from personalclaw.sdk.mcp import get_current_session_key

        conn = registry.get(server_name, get_current_session_key())
        if not conn:
            return ToolResult(success=False, error=f"MCP server '{server_name}' not found")

        ok, output = await conn.call_tool(tool, arguments)
        if not ok:
            return ToolResult(success=False, output="", error=output)
        # OP5 — MCP results are the highest-volume, least-controllable outputs (a server
        # can return anything). Apply the SAME dispatch-time projection discipline as
        # native tools: type-aware preview + retain the raw for tool_result_get. Uses the
        # session-scoped raw store (the same session key that routed the call above).
        from personalclaw.sdk.tool import DEFAULT_TOOL_OUTPUT_CAP as _MAX_OUTPUT_CHARS
        from personalclaw.sdk.tool import project_and_retain

        proj_text, meta = project_and_retain(
            output, session_key=get_current_session_key() or "", cap=_MAX_OUTPUT_CHARS,
        )
        return ToolResult(
            success=True, output=proj_text,
            truncated=("raw_ref" in meta), original_length=len(output), metadata=meta,
        )
