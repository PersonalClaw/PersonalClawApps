# OpenAI-Compatible

Any OpenAI-compatible Chat Completions endpoint (self-hosted, proxy, or an unlisted cloud). Supply the base URL + API key.

**OpenAI-Compatible** is a **model provider** — it registers chat/embedding models from any OpenAI-compatible Chat Completions endpoint under Settings → Models.

## The model list

The OpenAI models list an OpenAI-compatible server answers with names each model and says nothing about what it
does, so Settings → Models offers each for what its id says — and, for an alias
(`--served-model-name`), for what the model it serves says: the list's `root` field names that
model. A reranker, a moderation or safety classifier, and the other families no job here binds
are offered for nothing. A model whose name says nothing is offered for chat, the job such a
server's models are served for; if it is a pooling model (an embedder or a classifier under a
name that does not say so), its first chat turn reports the server's refusal.

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

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**OpenAI-Compatible** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or [install it from a shell](../docs/third-party-install.md#installing-from-a-shell).)

## Settings

| Key | Label | Notes |
|---|---|---|
| `endpoint` | Base URL | The OpenAI-compatible base URL (should end at /v1), e.g. https://my-gateway/v1. |
| `api_key` | API Key | Bearer API key for the endpoint. Leave empty to fall back to the OPENAI_API_KEY environment variable. |
| `default_model` | Default Model | The model id served by your endpoint. The model this instance answers with when nothing in Settings → Models names one. Leave it empty to choose its models in Settings → Models. |

## Network

Reaches only the endpoint you set in **Base URL**.

## Cost

An instance is never taken as a free local model, even at an address on this machine, since a
proxy there can front a paid cloud API. Its calls are priced by the model's id when PersonalClaw
knows that model's price, and otherwise have no price: Settings → Usage shows them as unpriced, and
a daily dollar cap refuses them until you set a price in **Settings → Usage → Model prices**. For an
endpoint that costs nothing, such as a model runtime on this machine, set the instance's price to
$0 (`<instance>:*`). Its prompts get the outbound scan Settings → Guardrails asks for.

## License

MIT — see the apps repo [LICENSE](../LICENSE).
