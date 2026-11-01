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
| `url` | Webhook URL | The https endpoint the event is POSTed to when this trigger fires. A plain http URL is refused unless it is on this machine (localhost, 127.0.0.1 or ::1), since the event and its headers would travel unencrypted. A webhook URL is a credential: whoever has it can post to it. In a scheduled or event automation, enter it as {{secret:NAME}}, a secret saved in Settings → Secrets, so it is filled in only when the event is sent and is never kept in the automation. A lifecycle hook fills it from PersonalClaw 0.3.5. |
| `method` | Method | HTTP method (default POST). |
| `headers` | Headers (JSON) | Optional JSON object of extra request headers. In a scheduled or event automation, enter a credential in one as {{secret:NAME}}, a secret saved in Settings → Secrets, e.g. {"Authorization": "Bearer {{secret:MY_TOKEN}}"}: it is filled in only when the event is sent. Content-Type defaults to application/json. A lifecycle hook fills it from PersonalClaw 0.3.5. |
| `body_template` | Body template | Optional. Template with $EVENT, $CONTEXT, $now, $job_name, etc. Leave blank to send the full event JSON as-is. |

## Example: fire a Home Assistant automation

A Home Assistant [webhook trigger](https://www.home-assistant.io/docs/automation/trigger/#webhook-trigger)
is a plain HTTP POST to your instance, so this app covers it with no Home Assistant app of its
own:

1. In Home Assistant, create an automation with a **webhook trigger** and note its webhook id.
2. Attach **HTTP Webhook Hooks** to the trigger that should reach the house, with the URL
   `https://homeassistant.local:8123/api/webhook/<your webhook id>` and, if the automation reads
   the event, a body template such as `{"event": "$EVENT"}`. The URL must be https: Home
   Assistant serves it once its `http` integration has a certificate this machine trusts
   (`ssl_certificate` and `ssl_key`). A plain http URL to another machine is refused, because the webhook id in it
   would travel unencrypted.
3. Your Home Assistant is on your own network, and the egress guard sends webhooks to public
   hosts only. Allow its host (`homeassistant.local` in the example) under
   **Settings → Security → Network egress** first, or the call is refused.

The webhook id is the credential: Home Assistant does not ask for anything else. Save the URL as
a secret in **Settings → Secrets** (say `HA_WEBHOOK`) and enter `{{secret:HA_WEBHOOK}}` as the
Webhook URL, so the automation keeps only the name and the URL is filled in when the event is sent.
That works in a scheduled or event automation; a lifecycle hook fills it from PersonalClaw 0.3.5.

## Network

Reaches only the URL you set in **Webhook URL**.

## License

MIT — see the apps repo [LICENSE](../LICENSE).
