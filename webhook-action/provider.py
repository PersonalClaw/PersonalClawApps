"""Webhook hook provider — POSTs the structured event payload to an HTTP endpoint.

`action_config` shape:
    {
        "url": "https://example.com/webhook",
        "method": "POST",                          # default POST
        "headers": {"Authorization": "Bearer ..."},
        "body_template": "{\"event\": \"$EVENT\"}",  # optional override
    }

Without ``body_template``, the JSON event payload is sent as-is. With a
template, ``$EVENT`` and ``$CONTEXT`` placeholders (and any keys from
``ctx.payload``) are interpolated via ``string.Template``.
"""

import asyncio
import json
import logging
import time
from string import Template
from typing import Any

from personalclaw.sdk.action import (
    ActionContext,
    ActionResult,
    ActionProvider,
)

logger = logging.getLogger(__name__)


class WebhookActionProvider(ActionProvider):
    @property
    def name(self) -> str:
        return "webhook"

    @property
    def display_name(self) -> str:
        return "HTTP Webhook"

    async def execute(
        self,
        action_config: dict[str, Any],
        ctx: ActionContext,
        timeout: int = 30,
    ) -> ActionResult:
        url = (action_config.get("url") or "").strip()
        if not url:
            return ActionResult(
                success=False, error="Webhook hook is missing 'url' field"
            )
        method = (action_config.get("method") or "POST").upper()
        # headers may arrive as a dict (programmatic caller) or a JSON string (the
        # trigger config form renders it as a text field). Accept both; a blank or
        # malformed string is treated as "no extra headers" rather than an error.
        raw_headers = action_config.get("headers") or {}
        if isinstance(raw_headers, str):
            raw_headers = raw_headers.strip()
            try:
                raw_headers = json.loads(raw_headers) if raw_headers else {}
            except json.JSONDecodeError:
                return ActionResult(
                    success=False,
                    error="Invalid 'headers': must be a JSON object, e.g. "
                    '{"Authorization": "Bearer …"}',
                )
        if not isinstance(raw_headers, dict):
            return ActionResult(success=False, error="'headers' must be a JSON object")
        headers = {str(k): str(v) for k, v in raw_headers.items()}
        headers.setdefault("Content-Type", "application/json")
        body_template = action_config.get("body_template")

        if body_template:
            mapping = {"EVENT": ctx.event, "CONTEXT": ctx.context}
            mapping.update({k: str(v) for k, v in (ctx.payload or {}).items()})
            try:
                body = Template(body_template).safe_substitute(mapping)
            except Exception as exc:
                return ActionResult(
                    success=False, error=f"Invalid body_template: {exc}"
                )
        else:
            body = json.dumps({"event": ctx.event, "context": ctx.context, **ctx.payload})

        # SSRF guard + delivery via the ONE egress chokepoint (net.fetch). The WEBHOOK
        # profile blocks loopback/RFC-1918/link-local/IMDS/multicast/reserved (a hook
        # pointed at 169.254.169.254 on EC2 could exfil instance creds; one at 127.0.0.1
        # could probe internal services) AND pins the resolved IP so the host can't
        # DNS-rebind to a private address between check and connect (the TOCTOU hole the
        # old provider-local _check_ssrf left open). Operators who legitimately POST to a
        # LAN service opt in by adding the host to security.egress.allow_hosts (Settings ›
        # Security › Network egress), which egress_policy_for layers onto the profile.
        from personalclaw.sdk.net import WEBHOOK, EgressBlocked, egress_policy_for
        from personalclaw.sdk.net import fetch as net_fetch

        policy = egress_policy_for(WEBHOOK).with_overrides(timeout_s=float(timeout))
        start = time.monotonic()
        try:
            resp = await net_fetch(url, policy=policy, method=method,
                                   headers=headers, data=body.encode("utf-8"))
            elapsed = int((time.monotonic() - start) * 1000)
            return ActionResult(
                success=200 <= resp.status < 300,
                exit_code=resp.status,
                stdout=resp.text[:4096],
                stderr="" if resp.status < 400 else f"HTTP {resp.status}",
                duration_ms=elapsed,
            )
        except EgressBlocked as exc:
            return ActionResult(
                success=False, error=str(exc),
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        except asyncio.TimeoutError:
            return ActionResult(
                success=False,
                error=f"Timed out after {timeout}s",
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        except Exception as exc:
            return ActionResult(
                success=False,
                error=str(exc),
                duration_ms=int((time.monotonic() - start) * 1000),
            )


def create_provider(config: dict[str, Any] | None = None) -> "WebhookActionProvider":
    return WebhookActionProvider()
