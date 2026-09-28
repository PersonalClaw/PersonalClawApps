# HTTP Webhook Hooks

POST agent lifecycle events to an HTTP endpoint. Supports method/headers overrides and a templated body.

**HTTP Webhook Hooks** is an **action provider** — it implements the `personalclaw.sdk.action` contract; attach it to a trigger/schedule to POST agent lifecycle events to an HTTP endpoint.

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

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**HTTP Webhook Hooks** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or [install it from a shell](../docs/third-party-install.md#installing-from-a-shell).)

## Settings

| Key | Label | Notes |
|---|---|---|
| `url` | Webhook URL | The HTTPS endpoint the event is POSTed to when this trigger fires. |
| `method` | Method | HTTP method (default POST). |
| `headers` | Headers (JSON) | Optional JSON object of extra request headers, e.g. {"Authorization": "Bearer …"}. Content-Type defaults to application/json. |
| `body_template` | Body template | Optional. Template with $EVENT, $CONTEXT, $now, $job_name, etc. Leave blank to send the full event JSON as-is. |

## Example: fire a Home Assistant automation

A Home Assistant [webhook trigger](https://www.home-assistant.io/docs/automation/trigger/#webhook-trigger)
is a plain HTTP POST to your instance, so this app covers it with no Home Assistant app of its
own:

1. In Home Assistant, create an automation with a **webhook trigger** and note its webhook id.
2. Attach **HTTP Webhook Hooks** to the trigger that should reach the house, with the URL
   `http://homeassistant.local:8123/api/webhook/<your webhook id>` and, if the automation reads
   the event, a body template such as `{"event": "$EVENT"}`.
3. Your Home Assistant is on your own network, and the egress guard sends webhooks to public
   hosts only. Allow its host (`homeassistant.local` in the example) under
   **Settings → Security → Network egress** first, or the call is refused.

The webhook id is the credential: Home Assistant does not ask for anything else, so keep the
URL out of anything you share.

## License

MIT — see the apps repo [LICENSE](../LICENSE).
