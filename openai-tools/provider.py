"""OpenAI-compatible tool server adapter for the ToolProvider interface.

Wraps REST-based tool servers that expose an OpenAI-compatible tools API:
  - Discovery: GET {endpoint}/tools (or /v1/tools)
  - Invocation: POST {endpoint}/tools/{tool_name} with JSON body

Each server instance is a separate provider that can be configured with
an endpoint URL, optional API key, and optional tool filter.
"""

import logging
import re
from typing import Any
from urllib.parse import urlparse

from personalclaw.sdk.tool import ToolDefinition, ToolProvider, ToolResult

logger = logging.getLogger(__name__)

# Slug-safe characters for provider name derivation
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _endpoint_slug(endpoint: str) -> str:
    """Derive a short slug from an endpoint URL for use as provider name."""
    if not endpoint:
        return "openai-tools"
    parsed = urlparse(endpoint)
    host = parsed.hostname or endpoint
    # Strip common prefixes/suffixes
    host = host.removeprefix("www.").removesuffix(".com").removesuffix(".dev")
    slug = _SLUG_RE.sub("-", host.lower()).strip("-")
    return f"openai-{slug}" if slug else "openai-tools"


class OpenAIToolProvider(ToolProvider):
    """Surfaces tools from an OpenAI-compatible tool server via REST API."""

    def __init__(
        self,
        endpoint: str,
        api_key: str = "",
        tool_filter: list[str] | None = None,
    ) -> None:
        self._endpoint = endpoint.rstrip("/") if endpoint else ""
        self._api_key = api_key
        self._tool_filter = tool_filter
        self._cached_tools: list[ToolDefinition] | None = None

    @property
    def name(self) -> str:
        return _endpoint_slug(self._endpoint)

    @property
    def display_name(self) -> str:
        if self._endpoint:
            return f"OpenAI Tools: {self._endpoint}"
        return "OpenAI Tool Server (unconfigured)"

    @property
    def connected(self) -> bool:
        if not self._endpoint:
            return False
        # Guard the operator endpoint through the same egress evaluator the data
        # paths use BEFORE any raw request — a private/blocked host reports "not
        # connected" rather than being probed (#41). ``evaluate`` is the SYNC guard
        # (resolves + classifies the host); the async net.fetch can't be used from
        # this sync property, but evaluate() gives the identical decision.
        try:
            from personalclaw.sdk.net import CONNECTOR, egress_policy_for, evaluate

            # Layer the operator's security.egress config onto CONNECTOR so a
            # self-hoster who allow-lists their tool host (or sets allow_private)
            # can reach a private/LAN tool server. Default stays public-only.
            if not evaluate(self._endpoint, egress_policy_for(CONNECTOR)).allow:
                return False
        except Exception:
            # Guard indeterminate (e.g. DNS failure) — fall through to the probe;
            # never silently skip the guard on a decision it actually returned.
            pass
        try:
            import urllib.request

            req = urllib.request.Request(self._endpoint, method="HEAD")
            if self._api_key:
                req.add_header("Authorization", f"Bearer {self._api_key}")
            urllib.request.urlopen(req, timeout=5)  # noqa: S310
            return True
        except Exception:
            return False

    def _headers(self) -> dict[str, str]:
        """Build request headers including optional auth."""
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def list_tools(self) -> list[ToolDefinition]:
        """Discover tools from the remote server via GET /tools."""
        if not self._endpoint:
            return []

        # Route discovery through the net.fetch egress chokepoint (host
        # classification, redirect-hop re-check, byte cap, timeout, SEL audit)
        # instead of raw aiohttp — the endpoint is operator-configured, so this
        # closes the SSRF/private-IP surface. #41.
        import json

        from personalclaw.sdk.net import CONNECTOR, EgressBlocked, egress_policy_for, fetch

        policy = egress_policy_for(CONNECTOR)
        tools_url = f"{self._endpoint}/tools"
        try:
            resp = await fetch(tools_url, policy=policy, method="GET", headers=self._headers())
            if resp.status != 200:
                # Try /v1/tools as fallback.
                fallback_url = f"{self._endpoint}/v1/tools"
                resp = await fetch(
                    fallback_url, policy=policy, method="GET", headers=self._headers()
                )
                if resp.status != 200:
                    logger.warning(
                        "OpenAI tool server %s returned %d", self._endpoint, resp.status
                    )
                    return []
            data = json.loads(resp.text)
        except EgressBlocked as exc:
            logger.warning("Tool discovery from %s blocked by egress guard: %s", self._endpoint, exc)
            return []
        except Exception as exc:
            logger.warning(
                "Failed to discover tools from %s: %s", self._endpoint, exc
            )
            return []

        # Parse the response — expect a list of tool objects or {"tools": [...]}
        raw_tools = data if isinstance(data, list) else data.get("tools", [])

        definitions: list[ToolDefinition] = []
        for tool in raw_tools:
            if not isinstance(tool, dict):
                continue
            name = tool.get("name", "")
            if not name:
                continue
            # Apply tool filter if configured
            if self._tool_filter and name not in self._tool_filter:
                continue
            description = tool.get("description", "")
            # Parameters can be under "parameters", "inputSchema", or "input_schema"
            parameters = (
                tool.get("parameters")
                or tool.get("inputSchema")
                or tool.get("input_schema")
                or {}
            )
            definitions.append(
                ToolDefinition(
                    name=name,
                    description=description,
                    provider=self.name,
                    parameters=parameters,
                    requires_approval=True,
                )
            )

        self._cached_tools = definitions
        return definitions

    async def invoke(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        """Execute a tool on the remote server via POST /tools/{tool_name}."""
        if not self._endpoint:
            return ToolResult(success=False, error="No endpoint configured")

        import json

        from personalclaw.sdk.net import CONNECTOR, EgressBlocked, egress_policy_for, fetch

        invoke_url = f"{self._endpoint}/tools/{tool_name}"
        try:
            # Route through the net.fetch egress chokepoint (operator endpoint →
            # SSRF-relevant; guard classifies the host + re-checks redirect hops). #41.
            # egress_policy_for layers the operator's security.egress allow-list so a
            # self-hosted LAN tool server is reachable when opted in.
            resp = await fetch(
                invoke_url, policy=egress_policy_for(CONNECTOR), method="POST",
                headers=self._headers(), data=json.dumps(arguments).encode(),
            )
            body = resp.text
            if resp.status >= 400:
                return ToolResult(
                    success=False,
                    error=f"HTTP {resp.status}: {body[:500]}",
                )
            # Try to parse as JSON
            try:
                result_data = json.loads(body)
                if isinstance(result_data, dict):
                    return ToolResult(
                        success=not result_data.get("isError", False),
                        output=str(
                            result_data.get("content")
                            or result_data.get("output")
                            or result_data.get("text")
                            or result_data.get("result")
                            or body
                        ),
                        error=result_data.get("error", ""),
                        metadata=result_data.get("metadata", {}),
                    )
                return ToolResult(success=True, output=body)
            except (json.JSONDecodeError, ValueError):
                return ToolResult(success=True, output=body)
        except EgressBlocked as exc:
            return ToolResult(success=False, error=f"Blocked by egress guard: {str(exc)[:400]}")
        except Exception as exc:
            return ToolResult(success=False, error=f"Request failed: {str(exc)[:500]}")


def create_openai_tool_provider(config: dict[str, Any] | None = None) -> OpenAIToolProvider:
    """Factory for the extension system. Returns an OpenAI tool server adapter.

    Called by the bundled ``openai-tools`` extension to create provider instances
    from user-supplied configuration (endpoint, api_key, tool_filter).
    """
    if not config:
        return OpenAIToolProvider("")
    endpoint = config.get("endpoint", "")
    api_key = config.get("api_key", "")
    tool_filter_raw = config.get("tool_filter", "")
    tool_filter = (
        [t.strip() for t in tool_filter_raw.split(",") if t.strip()] or None
    )
    return OpenAIToolProvider(endpoint, api_key, tool_filter)
