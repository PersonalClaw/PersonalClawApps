# Anthropic

Anthropic Claude chat models (Messages API). Bring your own Anthropic API key.

**Anthropic** is a **model provider** — it registers Anthropic Claude chat models under Settings → Models.

## What this is

A standalone PersonalClaw app bundle (part of the core/app workspace split). It ships
as a self-contained directory:

- `app.json` — the manifest (identity, provider/backend/UI declarations, permissions).
- `provider.py` — the implementation, exposed via `create_provider`.
- `test_catalog.py`, `test_provider.py` — the app's own tests.
- `test_wire.py` — proves a call's per-call temperature and output budget reach the request, against
  a recording endpoint.

It imports only the PersonalClaw **SDK** (never core internals), so core can evolve
without breaking it:

- `personalclaw.sdk.model`
- `personalclaw.sdk.net` — the guarded `fetch` the connection test probes with, so the
  operator's egress policy applies to it like any other outbound call.

## Connection test

"Test connection" (Settings → Providers) is **measured**, not inferred: it issues an
authenticated `GET /v1/models` against the configured Base URL and reports what came
back. A key Anthropic rejects reads as rejected. The model list the picker offers stays
curated (see `provider.py`), because excluding invitation-only ids is a decision no
endpoint can make for us.

## Per-call sampling

When core asks one call for its own sampling temperature (best-of-N's ladder) and output budget,
the request carries both; `test_wire.py` proves it against a recording endpoint. The app
installs the 0.x `anthropic` SDK (`anthropic>=0.20,<1`), because core's Messages client passes
the temperature as a keyword argument that the 1.x SDK removed, and on 1.x every such call
failed before it was sent. Anthropic's newer models refuse a custom temperature: Opus 4.7 and
later and Fable 5 reject any, and Sonnet 5 a non-default one. A temperature sweep bound to one
of them fails at the API.

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**Anthropic** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or [install it from a shell](../docs/third-party-install.md#installing-from-a-shell).)

## Settings

| Key | Label | Notes |
|---|---|---|
| `api_key` | Anthropic API Key | Your Anthropic API key (console.anthropic.com). Leave empty to fall back to the ANTHROPIC_API_KEY environment variable. |
| `default_model` | Default Model | Anthropic model id. Empty = newest from the app's built-in catalog. |
| `endpoint` | Base URL | Optional custom Anthropic-compatible base URL. Empty uses the Anthropic default. |

## License

MIT — see the apps repo [LICENSE](../LICENSE).
