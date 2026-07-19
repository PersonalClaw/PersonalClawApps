# Amazon Bedrock

Amazon Bedrock chat models via the Converse API. Authentication uses your AWS environment / named profile — no key is stored by PersonalClaw.

**Amazon Bedrock** is a **model provider** — it registers Amazon Bedrock chat models (Converse API) under Settings → Models; auth comes from your AWS environment/profile, no key is stored.

## What this is

A standalone PersonalClaw app bundle (part of the core/app workspace split). It ships
as a self-contained directory:

- `app.json` — the manifest (identity, provider/backend/UI declarations, permissions).
- `provider.py` — the implementation, exposed via `create_provider`.
- `test_catalog.py`, `test_provider.py`, `test_stream_timeout.py` — the app's own tests.

It imports only the PersonalClaw **SDK** (never core internals), so core can evolve
without breaking it:

- `personalclaw.sdk.model`

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**Amazon Bedrock** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or `POST /api/apps {"source": ".../apps/bedrock-models"}`.)

## Settings

| Key | Label | Notes |
|---|---|---|
| `region` | AWS Region | Bedrock region (e.g. us-west-2). Credentials come from your AWS environment / profile — no key is stored here. |
| `default_model` | Default Model | Bedrock model id (full versioned id). Empty = resolved from live Bedrock discovery (Claude preferred). |
| `profile` | AWS Profile | Optional named profile from ~/.aws. Empty uses the default credential chain (env / SSO / instance role). |
| `system_prompt` | System Prompt | Optional system prompt prepended to every turn. |

## Authentication

Uses your ambient AWS credential chain (environment variables, `~/.aws` profile, SSO). PersonalClaw stores no AWS key for this provider — configure the region/profile in the provider settings.

## License

MIT — see the apps repo [LICENSE](../LICENSE).
