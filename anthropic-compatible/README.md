# Anthropic-Compatible

Any Anthropic-compatible (Messages API) endpoint. Supply the base URL + API key.

**Anthropic-Compatible** is a **model provider** — it registers chat models from any Anthropic-compatible (Messages API) endpoint under Settings → Models.

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

## Per-call sampling

When core asks one call for its own sampling temperature (best-of-N's ladder) and output budget,
the request carries both; `test_wire.py` proves it against a recording endpoint. The app
installs the 0.x `anthropic` SDK (`anthropic>=0.20,<1`), because core's Messages client passes
the temperature as a keyword argument that the 1.x SDK removed, and on 1.x every such call
failed before it was sent. Whether an endpoint honours the temperature is its own call;
Anthropic's newer models refuse a custom one (Opus 4.7 and later and Fable 5 reject any, Sonnet
5 a non-default one).

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**Anthropic-Compatible** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or [install it from a shell](../docs/third-party-install.md#installing-from-a-shell).)

## Settings

| Key | Label | Notes |
|---|---|---|
| `endpoint` | Base URL | The Anthropic-compatible base URL, e.g. https://my-gateway. |
| `api_key` | API Key | API key for the endpoint. Leave empty to fall back to the ANTHROPIC_API_KEY environment variable. |
| `default_model` | Default Model | The Anthropic model id served by your endpoint. |

## License

MIT — see the apps repo [LICENSE](../LICENSE).
