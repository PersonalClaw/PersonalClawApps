# vLLM

A local vLLM server (OpenAI-compatible). Point it at your vLLM endpoint; capabilities are model-dependent.

**vLLM** is a **model provider** — it registers models served by your local vLLM server (OpenAI-compatible) under Settings → Models.

## The model list

The OpenAI models list a vLLM server answers with names each model and says nothing about what it
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
- `personalclaw.sdk.net`

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**vLLM** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or [install it from a shell](../docs/third-party-install.md#installing-from-a-shell).)

## Settings

| Key | Label | Notes |
|---|---|---|
| `endpoint` | vLLM Base URL | The OpenAI-compatible base URL of your vLLM server (e.g. http://localhost:8000). |
| `default_model` | Default Model | The model id served by your vLLM deployment. The model this instance answers with when nothing in Settings → Models names one. Leave it empty to choose its models in Settings → Models. |

## Network

Reaches only the vLLM server you set in **vLLM Base URL** (by default `localhost:8000`, on this machine).

## Cost

A vLLM server runs the models it serves on its own machine, and this app says so. An instance on
this machine is a local model: its calls cost a known $0, the daily dollar cap does not limit them,
local-first routing tries it first, and its prompts never leave the machine. An instance on another
machine is a remote model: give its models a price in **Settings → Usage → Model prices** if the
daily dollar cap should count them.

## License

MIT — see the apps repo [LICENSE](../LICENSE).
