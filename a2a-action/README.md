# A2A Agent Call

Hand one task to an external A2A agent when a trigger fires. Reaches only hosts the operator has allow-listed under Settings › Security › Network egress.

**A2A Agent Call** is an **action provider** — it implements the `personalclaw.sdk.action`
contract; attach it to any trigger, schedule or workflow action node to send an
[A2A](https://a2a-protocol.org) task to another agent.

This is the **outbound** half of PersonalClaw's A2A support. The inbound half lives in core
(an agent card at `GET /a2a/agent-card` plus `POST /a2a/tasks`, which maps an incoming task
onto a workflow run). Together they let PersonalClaw call other agents and be called by
them; either half works without the other.

## What this is

A standalone PersonalClaw app bundle (part of the core/app workspace split). It ships
as a self-contained directory:

- `app.json` — the manifest (identity, provider/backend/UI declarations, permissions).
- `provider.py` — the implementation, exposed via `create_provider`.
- `test_provider.py` — the app's own tests.

It imports only the PersonalClaw **SDK** (never core internals), so core can evolve
without breaking it:

- `personalclaw.sdk.action`
- `personalclaw.sdk.net`
- `personalclaw.sdk.security`

## Install

From the App Store, add the apps directory as a **local source**, then install
**A2A Agent Call** — the install runs through the security scanner and lifecycle exactly
like any other app. (Or `POST /api/apps {"source": ".../a2a-action"}`.)

## Egress is deny-by-default

The first thing this provider does on a fresh install is **refuse**, and that is correct.
Egress uses core's `a2a_outbound_policy`, which starts with an **empty** host allow-list and
`allow_only` set — so it reaches nowhere until an operator names a host under
**Settings › Security › Network egress**. The error message says so when it happens.

The policy is core's on purpose. This app never builds its own `EgressPolicy`: it could,
since the SDK exports the pieces, and that is precisely the hole being closed. A
self-composed policy is free to be the permissive shape that reaches every public host and
leaves the allow-list decorative. Core decides where a URL may point; the app supplies
the URL.

Everything the remote agent sends back is wrapped in an `<untrusted_content>` fence before
it reaches `stdout`, because `stdout` is read by a model downstream. Neither direction
trusts the other end.

## Settings

| Key | Label | Notes |
|---|---|---|
| `url` | Agent URL | The external agent's A2A tasks endpoint. The host must be allow-listed; egress is deny-by-default. |
| `skill` | Skill | The skill id to invoke, as it appears on the remote agent's card. Blank lets the remote agent choose. |
| `text` | Message | The text sent to the agent. Supports `$EVENT`, `$CONTEXT` and any trigger payload key. |
| `inputs` | Inputs (JSON) | Optional JSON object of structured inputs for the skill, e.g. `{"since": "7d"}`. |
| `headers` | Headers (JSON) | Optional extra request headers, e.g. `{"Authorization": "Bearer …"}`. |

## Autonomy

The manifest declares both an autonomy floor and a ceiling of `one_tap`, so this action
never escalates to unattended firing however much track record accrues. That is not
conservatism for its own sake: a delivered A2A task cannot be recalled. The remote agent may
bill for it and act on it, and there is no undo to promote the action to
`auto_with_undo` on the strength of.

## Wire format

One request per `execute`, in A2A's canonical `message/send` shape:

```json
{
  "message": {
    "role": "user",
    "messageId": "<uuid, per firing>",
    "parts": [{"kind": "text", "text": "<the rendered message>"}]
  },
  "metadata": {"skillId": "<skill>"},
  "inputs": {"since": "7d"}
}
```

`messageId` is the retry key. Core's inbound half derives an idempotency key from it, so a
retried delivery adopts the existing run rather than starting a second one — which means
two PersonalClaw instances interoperate through this provider with no special-casing on
either side.

The reply is expected to be an A2A Task envelope; its `status.state`, task id, text part and
artifact count are lifted to the front of `stdout`. A reply in an unrecognized dialect is
passed through as JSON rather than discarded — an agent that answered in a shape we did not
predict has still answered.

## License

MIT — see the apps repo [LICENSE](../LICENSE).
