# Browser Connector

Attach your **own everyday browser** to a PersonalClaw you already run, so a browse task
with `target: "user_browser"` can act inside a session you are already logged into — your
paid subscriptions, your SSO, your cookies — instead of the gateway's own separate profile.

This is the app-bundle half of the `user_browser` target. The core half is the gateway's
connector seam: the loopback routes under `/api/browse/connector`, and the per-task grant.

## A run works only in a tab it opens for itself

A browse run never touches a tab you have open, whichever tab has focus:

1. **You allow the task.** The gateway asks you first, naming the task and the sites it will
   touch. Nothing happens in your browser until you allow it.
2. **The run opens its own tab.** The extension opens a new tab **in the background** — it
   does not take focus — inside a **tab group named after the task**. In a browser without
   tab groups it opens an unfocused **window of its own** instead.
3. **It announces that tab, and only that tab.** The extension finds the new tab's own page
   target on your browser's loopback debugger and announces it for that run; the gateway binds
   your grant to it. Any other page the debugger lists, including the one you are looking at,
   is never announced.
4. **Stop and take over are gestures.** Close the task's group (or its window, or the tab) and
   the run **stops** within one step. Bring the run's tab to the front and that is a
   **take-over**: the run **pauses** and says so, and the page is yours.
5. **No tab, no run.** If the extension cannot open a tab of the run's own, the gateway refuses
   the task with a sentence saying so. It never falls back to a tab you already have open.

When a run ends, its tab stays where it is, as yours, and nothing reaches it any more.

## How it connects

1. **Pair once.** The extension redeems a pairing code from **Settings → Devices**, exactly
   like a phone. That mints the ordinary session cookie of a paired device — the connector is
   a normal paired device, listed in the same registry and revocable the same way. No second
   credential type.
2. **Attach.** The extension tells the gateway this browser is connected
   (`POST /api/browse/connector`). Attaching announces no page.
3. **Answer runs.** While attached, the extension asks the gateway once a second whether an
   allowed run needs a tab (`GET /api/browse/connector/tabs`), opens one when it does, and
   announces it or reports its end (`POST /api/browse/connector/tabs/<run>`). If the gateway
   restarts, the extension attaches again on its own; if another browser attaches in its place,
   or you revoke its pairing, it stops asking until you attach it again.

The page targets come from your browser's **own** remote-debugging server (start the browser
with a loopback `--remote-debugging-port`). The extension does not — and cannot — open that
surface itself; it only *reports* the loopback URL of the run's tab.

## What it asks your browser for

| Permission | Why |
|---|---|
| `tabs` | open the run's tab, and act on that tab by its id |
| `tabGroups` | name the run's group after the task, and notice when you close it |
| `storage` | the gateway URL and debugger port, and which tab belongs to which run |

Host permissions are loopback-only: `127.0.0.1` and `localhost`, over http and ws.

## The typed loopback contract

A deliberately narrow, **closed** vocabulary — a wider surface is a wider blast radius on a
logged-in session. Every request names the **run** it is for, and acts on that run's own tab
only:

| Verb | Params | Where it runs |
|---|---|---|
| `navigate` | `url` | worker (tabs API) |
| `read-outline` | — | content script (DOM → stable refs) |
| `click` | `ref` | content script |
| `type` | `ref`, `value` | content script |
| `close` | — | worker (tabs API) |

`connector.py` is the source of truth for the vocabulary, the run-tab routes and the loopback
rules; `extension/contract.js` mirrors it, and `test_contract.py` fails if the two drift.

## Loopback only, no new listener

- The extension's `host_permissions` are **loopback-only** (`127.0.0.1` / `localhost`), so it
  literally cannot reach anywhere else.
- `announce_url` and the run-tab routes refuse a non-loopback gateway, and `announce_payload`
  refuses a non-loopback `cdp_url`, so a public endpoint can never leave the bundle even if
  misconfigured.
- It opens **no listening socket**: every network call is an outbound loopback request; the
  only inbound channel is intra-extension messaging.
- It never reads, stores, or types into a **password field**.

## Install (client-side)

This app installs on **your** machine, not the server.

```bash
DEST="${PERSONALCLAW_HOME:-$HOME/.personalclaw}/apps/browser-connector"
git clone --depth 1 --filter=blob:none --sparse https://github.com/PersonalClaw/PersonalClawApps "$DEST"
git -C "$DEST" sparse-checkout set browser-connector
```

It goes into the PersonalClaw home: `$PERSONALCLAW_HOME` when you run PersonalClaw on a home of
its own, else `~/.personalclaw`. Then, in your browser's extensions page (developer mode),
**Load unpacked** → `$DEST/browser-connector/extension`. Start the browser with a loopback
remote-debugging port, and pair the connector from **Settings → Devices**. (On Windows, use the
equivalent `%USERPROFILE%` path.)

## Security posture

The blast radius of a `user_browser` task is your real session, which is why the design is
procedural: a per-task grant, a tab of the run's own, a live watch, and close-to-kill — none of
which this bundle weakens. It is never marketed as bypassing anti-bot or CAPTCHA protections;
its purpose is to let the agent act in your browser with your explicit, per-task permission.

## Tests

```bash
python -m pytest browser-connector -q
```

`test_contract.py` pins the closed vocabulary and its run addressing, the JS↔Python parity,
the loopback rail (public endpoints and gateways refused), and the manifest's permissions.
`test_extension.py` runs `test_extension.mjs` with Node's own test runner (Node.js 18 or
newer; no npm install): it loads the real worker against a browser double and checks which tab
the run opens, which page it announces, which tab each verb touches, and what it reports when
the run's group closes or its tab is taken over.
