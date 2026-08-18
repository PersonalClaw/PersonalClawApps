# Claude (Subscription)

Claude models over your existing Claude Code sign-in — no API key. Reads the CLI's own
credential store read-only.

**Claude (Subscription)** is a **model provider**: it registers Claude chat models under
Settings → Models, and authenticates them with the sign-in the Claude Code CLI already
holds on this machine. There is nothing to paste and nothing to buy twice — if `claude`
works in your terminal, this provider works.

## What this is

The reference **subscription-credential** provider app. Some vendors bill per seat rather
than per token, so no API key exists to configure; the token lives in a store the vendor's
own CLI owns. This app declares where that store is, and core resolves the credential from
it at build time. Two declarations do the whole job (`provider.py`):

- a `SubscriptionSource` — the candidate store paths, the key walk to the token, the expiry
  stamp, and **this app's own** sign-in sentence;
- `BrandedProviderSpec(credential_source="claude-code", api_key_env="")` — naming that
  source, with no API-key fallback at any layer.

Everything else is the ordinary branded-app path: sessions, models and catalogs flow
through core's Anthropic Messages client, and **no agent runtime is involved**. This is a
model provider that resolves a credential differently, not a wrapper around the CLI.

It ships as a self-contained directory:

- `app.json` — the manifest (identity, provider declaration, permissions).
- `provider.py` — the implementation, exposed via `create_provider`.
- `test_provider.py` — the app's own tests.

It imports only the PersonalClaw **SDK** (never core internals):

- `personalclaw.sdk.model`
- `personalclaw.sdk.provider_helpers`

## Sign-in, and what happens when you are not signed in

Run the CLI's own sign-in — `claude login` — and nothing else. PersonalClaw reads that
store **read-only**: it never writes, refreshes, repairs, chmods or deletes it, not even to
renew an expired token. When the token expires you re-run `claude login` yourself.

Not signed in is not an error. Core derives an availability probe from the declared
`credential_source`, so the app is greyed out in the extensions list with this app's own
reason — *"claude-code is not signed in on this machine — sign in with `claude login`
first"* — instead of failing later at the wire. Expired, blank and half-written stores land
in the same soft outcome. No reason string ever contains any part of your token.

## Where the token is read from

Tried in order, first usable token wins:

| Path | When |
|---|---|
| `$CLAUDE_CONFIG_DIR/.credentials.json` | you relocated the CLI's config (explicit override wins) |
| `~/.claude/.credentials.json` | the default location |
| `~/.config/claude/.credentials.json` | XDG-style layouts |

Keys read: `claudeAiOauth.accessToken`, and `claudeAiOauth.expiresAt` (epoch ms) to detect
an expired sign-in.

### Known limitations

- **Keychain-only installs read as not-signed-in.** Core's resolver reads JSON *file*
  stores. On a macOS install where Claude Code keeps its token in the login Keychain and
  writes no `.credentials.json`, this app greys out with the sign-in hint rather than
  pretending. Reading a Keychain item needs a core-side resolver, not an app.
- **"Test connection" cannot see your sign-in.** The shared branded catalog probes with a
  configured API key, and this app has none, so connectivity reports "no API key
  configured". Sign-in state is shown by the extensions list instead (above).
- **Model list.** The Messages API exposes no models-list route, so the picker is fed from
  a curated list mirroring the sibling `anthropic-models` app. Which of those ids your plan
  may call is the vendor's business — one it does not include fails at the wire with
  Anthropic's own error, exactly as it would with an API key.

## Install

From the App Store, add the apps directory as a **local source**, then install **Claude
(Subscription)** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or `POST /api/apps {"source": ".../apps/claude-subscription"}`.)

## Settings

| Key | Label | Notes |
|---|---|---|
| `default_model` | Default Model | A Claude model id. Empty = the newest model from the app's built-in list. |
| `endpoint` | Base URL | Optional Anthropic-compatible base URL. Empty uses the official Anthropic host. |

There is deliberately **no `api_key` setting**. A key set by hand on the instance would
still win over the subscription (the credential order puts an explicit choice first), but
this app never asks for one and never reads `ANTHROPIC_API_KEY`.

## Permissions

`network` — the provider calls the vendor's API. Nothing else is claimed: the credential
read is performed by core, not by app code.

## License

MIT — see [LICENSE](LICENSE).
