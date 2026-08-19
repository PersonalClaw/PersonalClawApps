# Kiro CLI

Run the kiro-cli agent (acp:kiro-cli) over ACP. kiro-cli is an Amazon-internal CLI; this provider activates only when the `kiro-cli` binary is present on the machine, and is unavailable otherwise.

**Kiro CLI** is an **ACP agent bundle** — it registers an `acp:<cli>` agent via `personalclaw.sdk.acp` and appears in the Agents list.

## What this is

A standalone PersonalClaw app bundle (part of the core/app workspace split). It ships
as a self-contained directory:

- `app.json` — the manifest (provider type + `implementation`; Tier-2 apps carry no `native` flag — that's Tier-1-only).
- `provider.py` — the implementation, exposed via `create_provider`.
- `test_provider.py` — the app's own tests.

It imports only the PersonalClaw **SDK** (never core internals), so core can evolve
without breaking it:

- `personalclaw.sdk.acp`

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**Kiro CLI** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or `POST /api/apps {"source": ".../apps/kiro-cli-agent"}`.)

## Settings

| Key | Label | Notes |
|---|---|---|
| `model` | Default Model | Optional model the agent defaults to. Empty uses the kiro CLI's own default. |
| `acp_bin` | CLI Path | Optional absolute path to the kiro-cli binary. Empty auto-resolves via PATH. Equivalent to the KIRO_CLI_BIN env var. |

## Capability boundary

kiro-cli speaks the baseline ACP shape, which means several host features are supplied by
PersonalClaw and a few are unavailable. [The ACP parity statement](https://github.com/PersonalClaw/PersonalClaw/blob/main/docs/agents/acp-parity.md) states which is which per
provider, with the CLI version each verdict was measured against.

## License

MIT — see `LICENSE`.
