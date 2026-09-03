# Browser Connector

Attach your **own everyday browser** to a PersonalClaw you already run, so a browse task
with `target: "user_browser"` can act inside a session you are already logged into — your
paid subscriptions, your SSO, your cookies — instead of the gateway's own separate profile.

This is the app-bundle half of **BA-8**. The core half (the gateway seam it writes to) is
`browse.target.register_connector` plus the loopback route `POST /api/browse/connector`.

## How it connects

1. **Pair once.** The extension redeems a pairing code from **Settings → Devices**, exactly
   like a phone. That mints the ordinary session cookie the shipped device-session machinery
   (COMPANION-APPS §C1/C2) already uses — the connector is a normal paired device, listed in
   the same registry, revocable the same way. No second credential type.
2. **Announce, over loopback.** The extension reports the **CDP page-target endpoint** your
   browser exposes on loopback to `POST /api/browse/connector`. That endpoint is what the
   gateway's connector seam consumes; `resolve_cdp_url` hands it straight to the CDP transport.
3. **Drive.** A `user_browser` browse task then runs against that endpoint through a closed,
   typed contract.

The endpoint comes from your browser's **own** remote-debugging server (start the browser
with a loopback `--remote-debugging-port`). The extension does not — and cannot — open that
surface itself; it only *reports* the loopback URL and drives the page.

## The typed loopback contract

A deliberately narrow, **closed** vocabulary — a wider surface is a wider blast radius on a
logged-in session:

| Verb | Params | Where it runs |
|---|---|---|
| `navigate` | `url` | worker (tabs API) |
| `read-outline` | — | content script (DOM → stable refs) |
| `click` | `ref` | content script |
| `type` | `ref`, `value` | content script |
| `close` | — | worker (tabs API) |

`connector.py` is the source of truth for the vocabulary and the loopback rules;
`extension/contract.js` mirrors it, and `test_contract.py` fails if the two drift.

## Loopback only, no new listener

- The extension's `host_permissions` are **loopback-only** (`127.0.0.1` / `localhost`), so it
  literally cannot reach anywhere else.
- `announce_url` refuses a non-loopback gateway and `announce_payload` refuses a non-loopback
  `cdp_url`, so a public endpoint can never leave the bundle even if misconfigured.
- It opens **no listening socket**: every network call is an outbound loopback request; the
  only inbound channel is intra-extension messaging.
- It never reads, stores, or types into a **password field**.

## Install (client-side)

This app installs on **your** machine, not the server.

```bash
DEST="$HOME/.personalclaw/apps/browser-connector"
git clone --depth 1 --filter=blob:none --sparse https://github.com/PersonalClaw/PersonalClawApps "$DEST"
git -C "$DEST" sparse-checkout set browser-connector
```

Then, in your browser's extensions page (developer mode), **Load unpacked** →
`~/.personalclaw/apps/browser-connector/browser-connector/extension`. Start the browser with a
loopback remote-debugging port, and pair the connector from **Settings → Devices**. (On
Windows, use the equivalent `%USERPROFILE%` path.)

## Security posture

The blast radius of a `user_browser` task is your real session, which is why the design is
procedural: per-task grant, live watch, and close-to-kill (BA-9) — none of which this bundle
weakens. It is never marketed as bypassing anti-bot or CAPTCHA protections; its purpose is to
let the agent act in your browser with your explicit, per-task permission.

## Tests

```bash
python -m pytest browser-connector -q
```

`test_contract.py` pins the closed vocabulary, the JS↔Python parity, the loopback rail
(public endpoints and gateways refused), and the manifest's loopback-only host permissions.
