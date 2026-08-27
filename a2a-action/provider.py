"""`a2a-call` — hand ONE task to an external A2A agent (EXTERNAL-ACCESS §5, EA-8).

The outbound half of PersonalClaw's A2A support. The inbound half lives in core
(`personalclaw.inbound.a2a`: an agent card plus `POST /a2a/tasks` mapped onto a
`WorkflowRun`); this app is the other direction — a trigger fires, and one A2A task goes
out to somebody else's agent.

`action_config` shape::

    {
        "url": "https://agent.example.com/a2a/tasks",   # required
        "skill": "weekly-digest",                       # optional remote skill id
        "text": "$EVENT fired: $CONTEXT",               # optional message template
        "inputs": {"since": "7d"},                      # optional structured inputs
        "headers": {"Authorization": "Bearer ..."},     # optional
    }

Two design points are deliberate and load-bearing.

**The egress policy is core's, not this app's.** `sdk.net.a2a_outbound_policy` returns an
`allow_only=True` policy whose allow-list starts EMPTY, so the default posture is "reaches
nowhere" and the operator names each reachable agent under Settings › Security › Network
egress. This app never constructs an `EgressPolicy`. It could — the SDK exports the
building blocks — and that is exactly why it doesn't: a self-composed policy is free to be
the additive `egress_policy_for(CONNECTOR)` shape that reaches every public host and makes
the allow-list decorative. Core decides where a URL may point; this app supplies the URL.

**The response is fenced.** Whatever the remote agent returns is attacker-controlled text
that lands in `ActionResult.stdout`, and stdout is read back by a model in the workflow and
trigger paths. So it goes through `sdk.security.fence_untrusted` before it is returned.
Fencing on the way OUT of this provider mirrors what core's inbound half does on the way
out of `POST /a2a/tasks` — neither direction trusts the other end.
"""

import asyncio
import json
import logging
import time
import uuid
from string import Template
from typing import Any

from personalclaw.sdk.action import (
    ActionContext,
    ActionProvider,
    ActionResult,
)

logger = logging.getLogger(__name__)

#: Cap on the remote agent's reply before it is fenced into `stdout`. Matches the
#: `webhook-action` precedent's 4096 rather than the policy's `max_bytes`: the policy bound
#: is what we are willing to RECEIVE, this is what we are willing to paste into a model's
#: context, and they are not the same number.
_MAX_STDOUT = 4096


def _as_dict(raw: Any, label: str) -> tuple[dict[str, Any] | None, str]:
    """Coerce a config field that may arrive as a dict or a JSON string.

    The trigger config form renders these as text fields while a programmatic caller passes
    a real dict, so both are accepted. A blank string is "not supplied", not an error —
    every one of these fields is optional.
    """
    if not raw:
        return {}, ""
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return {}, ""
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None, f"Invalid '{label}': must be a JSON object"
    if not isinstance(raw, dict):
        return None, f"'{label}' must be a JSON object"
    return dict(raw), ""


def _message_text(action_config: dict[str, Any], ctx: ActionContext) -> str:
    """The text part of the outbound message.

    `$EVENT`/`$CONTEXT` and the payload keys interpolate exactly as they do in
    `webhook-action`, so an operator who has written one trigger body already knows this
    field. `safe_substitute` is deliberate: an unknown `$name` survives as literal text
    rather than raising, because a template typo should not silently drop the whole task.
    """
    template = (action_config.get("text") or "").strip()
    if not template:
        return f"{ctx.event}: {ctx.context}" if ctx.context else str(ctx.event)
    mapping = {"EVENT": ctx.event, "CONTEXT": ctx.context}
    mapping.update({k: str(v) for k, v in (ctx.payload or {}).items()})
    return Template(template).safe_substitute(mapping)


def _task_request(
    skill: str, text: str, inputs: dict[str, Any], message_id: str
) -> dict[str, Any]:
    """One A2A `message/send` request body.

    ONE spelling, the spec-canonical one: the skill travels in `metadata.skillId` and the
    text in `message.parts[].text`. Core's own inbound `_skill_id_of` reads that metadata
    key, so a PersonalClaw instance is reachable by this provider without either side
    special-casing the other — which is the interop claim §5 actually makes.

    `messageId` is the caller's retry key. Core's inbound turns it into an idempotency key
    (`a2a:<id>`), so a retried delivery adopts the existing run instead of starting a
    second one. It is generated per `execute` call rather than per config, because two
    firings of the same trigger are two different tasks.
    """
    body: dict[str, Any] = {
        "message": {
            "role": "user",
            "messageId": message_id,
            "parts": [{"kind": "text", "text": text}],
        },
        "metadata": {},
    }
    if skill:
        body["metadata"]["skillId"] = skill
    if inputs:
        body["inputs"] = inputs
    return body


def _summarize(payload: Any) -> str:
    """The remote reply, rendered for `stdout`, BEFORE fencing.

    An A2A Task envelope is the expected shape, so its state and text part are lifted to
    the front where an operator reading a trigger's run history will see them. Anything
    else is passed through as JSON rather than being called an error: a remote agent that
    answers in a dialect we did not predict has still answered, and discarding its reply
    would make this provider's failures indistinguishable from its successes.
    """
    if not isinstance(payload, dict):
        return json.dumps(payload) if payload is not None else ""
    status = payload.get("status")
    if not isinstance(status, dict):
        return json.dumps(payload)
    lines: list[str] = []
    state = status.get("state")
    if isinstance(state, str) and state:
        lines.append(f"state: {state}")
    task_id = payload.get("id")
    if isinstance(task_id, str) and task_id:
        lines.append(f"task: {task_id}")
    message = status.get("message")
    if isinstance(message, dict):
        for part in message.get("parts") or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                lines.append(part["text"])
    artifacts = payload.get("artifacts")
    if isinstance(artifacts, list) and artifacts:
        lines.append(f"artifacts: {len(artifacts)}")
    return "\n".join(lines) if lines else json.dumps(payload)


class A2AActionProvider(ActionProvider):
    @property
    def name(self) -> str:
        return "a2a-call"

    @property
    def display_name(self) -> str:
        return "A2A Agent Call"

    async def execute(
        self,
        action_config: dict[str, Any],
        ctx: ActionContext,
        timeout: int = 30,
    ) -> ActionResult:
        url = (action_config.get("url") or "").strip()
        if not url:
            return ActionResult(
                success=False, error="A2A action is missing 'url' field"
            )

        inputs, err = _as_dict(action_config.get("inputs"), "inputs")
        if err:
            return ActionResult(success=False, error=err)
        raw_headers, err = _as_dict(action_config.get("headers"), "headers")
        if err:
            return ActionResult(success=False, error=err)
        assert inputs is not None and raw_headers is not None  # both errors returned above

        headers = {str(k): str(v) for k, v in raw_headers.items()}
        headers.setdefault("Content-Type", "application/json")
        headers.setdefault("Accept", "application/json")

        skill = (action_config.get("skill") or "").strip()
        body = _task_request(
            skill, _message_text(action_config, ctx), inputs, str(uuid.uuid4())
        )

        # The ONE egress chokepoint. `a2a_outbound_policy` is core's — see the module
        # docstring for why this app does not build its own. `with_overrides` narrows the
        # timeout to the caller's and touches nothing else; it cannot widen the host reach,
        # because `allow_only` and `allow_hosts` are not among the fields set here.
        from personalclaw.sdk.net import EgressBlocked, a2a_outbound_policy
        from personalclaw.sdk.net import fetch as net_fetch
        from personalclaw.sdk.security import fence_untrusted

        policy = a2a_outbound_policy().with_overrides(timeout_s=float(timeout))
        start = time.monotonic()
        try:
            resp = await net_fetch(
                url,
                policy=policy,
                method="POST",
                headers=headers,
                data=json.dumps(body).encode("utf-8"),
            )
        except EgressBlocked as exc:
            # Deny-by-default landing here is the NORMAL first experience, so the message
            # names the remedy: the operator has not allow-listed this agent's host yet.
            return ActionResult(
                success=False,
                error=(
                    f"{exc} — add the agent's host to Settings › Security › Network "
                    "egress to allow it."
                ),
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        except asyncio.TimeoutError:
            return ActionResult(
                success=False,
                error=f"A2A agent did not respond within {timeout}s",
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        except Exception as exc:  # noqa: BLE001 — a transport fault is an action failure
            return ActionResult(
                success=False,
                error=str(exc),
                duration_ms=int((time.monotonic() - start) * 1000),
            )

        elapsed = int((time.monotonic() - start) * 1000)
        try:
            payload = json.loads(resp.text) if resp.text.strip() else None
        except json.JSONDecodeError:
            payload = None
        rendered = _summarize(payload) if payload is not None else resp.text
        return ActionResult(
            success=200 <= resp.status < 300,
            exit_code=resp.status,
            # `source` follows the `mail-inbox` / `web-tools` convention — the fence names
            # WHICH agent said this, so a reader of the run history can tell two remote
            # agents apart inside one workflow.
            stdout=fence_untrusted(rendered[:_MAX_STDOUT], source=f"a2a:{url}"),
            stderr="" if resp.status < 400 else f"HTTP {resp.status}",
            duration_ms=elapsed,
        )


def create_provider(config: dict[str, Any] | None = None) -> "A2AActionProvider":
    return A2AActionProvider()
