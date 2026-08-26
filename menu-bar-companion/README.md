# Menu Bar Companion

A macOS menu-bar presence for a PersonalClaw you already run. It sits in the status bar
on **your** Mac, shows what is happening and — more to the point — what is *waiting on
you*:

- **Live run rows** — every loop that has not ended, with its status.
- **Pending approvals** with one-click **Approve** / **Deny**.
- **Needs-input deep links** — one click lands you on `#/loops/<id>` in the dashboard,
  already authenticated.
- **A badge** counting exactly what is blocked on you: pending approvals + runs needing
  input. Nothing else.
- **Mute notifications** from the Settings item.

**Menu Bar Companion** is a **client app** (`platform.installMode: "client"`,
`platform.os: ["darwin"]`). PersonalClaw does not install it on the server; the App
Store shows you a copy-paste one-liner to run on the Mac that should show the menu bar
item. It can point at a gateway on the same machine or another one.

## The one design rule

**The WebSocket is a doorbell, not a data channel.**

The app holds exactly **one** `/api/ws` connection for its whole lifetime. When a frame
arrives it means one thing — *something changed, go read it again over HTTP*. The frame's
bytes are never rendered, never parsed for state, never trusted.

That is enforced structurally rather than by convention:

| Where | What it does |
|---|---|
| `doorbell.read_frame(sock) -> int` | Returns the **opcode**. The payload is received and dropped before the function returns, so there is no return path a payload could travel on. |
| `Doorbell(on_ring=…)` | Invoked as `on_ring()` with **zero arguments**, and a callback that *requires* an argument is refused at construction (`TypeError`). A payload-consuming design cannot be installed. |
| `CompanionModel.refresh()` | Takes no server-supplied argument either. It re-reads `GET /api/loops` and `GET /api/approvals` and replaces state from the HTTP response. |

`test_doorbell.py` proves it end to end: a frame carrying 99 fabricated approvals rings
the doorbell and changes the badge to what **HTTP** said (3), with the sentinel absent
from everything rendered — and the same test file contains a payload-consuming reader
that *does* surface the 99, so the assertion is discriminating rather than vacuous.

Control frames are not rings: the gateway's 30-second heartbeat `PING` is answered with a
`PONG` and does **not** trigger a refetch, or the socket would just be a 30-second poll
wearing a socket's clothes.

Reconnection uses a growing backoff — 1s, 2s, 4s, 8s, 16s, capped at 30s — and a
successful connect resets the ladder.

There is also a **floor poll** (default 60s). It is not a fallback for reading payloads;
it is the guarantee that a change which produces no frame at all still lands.

## Everything shown is derived

The badge is a `@property` over the same two lists the menu renders, computed on every
read. There is no counter kept beside them — a count maintained next to a list is two
facts that can disagree, and the one the user sees is the wrong one.
`model.INSTANCE_ATTRS` pins the model's whole attribute set so that adding a cache later
fails a test instead of drifting quietly.

## Approve / Deny is a write, and a failed write says so

`POST /api/approvals/{id}/{action}` — the gateway's action pair is `approve` / `reject`,
so the menu's **Deny** maps to `reject` on the wire (`api.WIRE_ACTION`).

If the POST fails:

- the failure is rendered **in the menu the click happened in** (`⚠ …`, carrying the
  gateway's own reason and status code),
- and **nothing moves locally** — the row still reads as pending and the badge still
  counts it. The truth comes from the next `GET`, so a failed write can never leave a row
  looking decided.

## Permissions

```json
"permissions": {
  "api": ["/api/loops", "/api/approvals", "/api/ws"],
  "events": ["approval", "approval_resolved"]
}
```

That is the whole list, and each entry is used:

- `/api/loops` — the live run rows and the needs-input rows (read-only).
- `/api/approvals` — the pending approvals, and the one write this app makes.
- `/api/ws` — the single doorbell connection.
- `events` — the two approval event families. The app rings on *any* frame and reads no
  payload, so it deliberately does not claim a broader event set; the floor poll covers
  anything a narrow grant would filter out.

Nothing else is claimed. In particular:

- **no `storage`** — a client app is never server-installed, so the platform never hands
  it a `DATA_DIR`. It keeps its own settings on your Mac (below).
- **no `network`** — it talks to your own gateway, not out to the internet.
- **no `cron`, `agent`, `memory`, `mcpTools`, app messaging or shared storage.**

Native notifications are posted **locally** with `osascript`, on your own machine — not
through the gateway's desktop seam — so no `desktop` capability is claimed either.

## Local state

Settings live on your Mac, not in the gateway's home:

```
~/Library/Application Support/PersonalClaw Companion/settings.json    (mode 0600)
```

Override the directory with `PERSONALCLAW_COMPANION_HOME`.

That file holds your **gateway token**, which is a bearer credential for the whole
gateway — hence 0600. If you would rather not persist it, supply both by environment
instead and nothing is written:

```sh
export PERSONALCLAW_COMPANION_URL=http://localhost:10000
export PERSONALCLAW_COMPANION_TOKEN=…      # from `personalclaw token`
```

Toggling mute writes **only** the preference keys back, so a Settings click never
silently persists an env-supplied token.

## Install and run

The App Store shows the `platform.clientInstall` one-liner (clone this app + install the
GUI dependency). Then, on the Mac:

```sh
python3 run.py --configure http://localhost:10000 <token>   # both from `personalclaw token`
python3 run.py --check                                      # one read; prints the menu
python3 run.py                                              # live in the menu bar
```

`--check` is the honest smoke test: it proves the token works, prints exactly what the
menu would show, and reports whether a status-item backend is present.

## The status-item host

The menu-bar chrome is the only part of this app that needs a GUI toolkit. It uses
[`rumps`](https://pypi.org/project/rumps/) (a thin wrapper over PyObjC's
`NSStatusItem`) — imported **lazily**, and deliberately **not** a declared
`pythonDependencies` entry: a client app's dependencies are installed by the
`clientInstall` one-liner on the user's own Mac, and pinning a darwin-only wheel in the
manifest would also break this repo's Linux test job.

Everything else — the menu's content, the badge, the writes, the socket discipline — is
plain stdlib and runs (and is tested) anywhere. If no backend is importable, `run.py`
says why and falls back to printing the menu on every change rather than dying inside an
`import Cocoa`.

`test_status_item.py` drives the status-item path against a **stubbed** `rumps` in
`sys.modules` — the same convention the model-provider apps use for vendor SDKs CI cannot
install. It proves the path is live (the derived badge reaches the title, the Approve item
reaches the write, the redraw timer is armed) against exactly the three names the real
module must provide (`App`, `MenuItem`, `Timer`). What it deliberately does **not** prove
is a real `NSStatusItem` drawn by real PyObjC on a Mac with a window server — nothing
automated can, so that one clause is verified by installing `rumps` and looking at your
menu bar.

This app does **not** ship an Electron shell and does not need one. It is not the
PersonalClaw desktop app; it is a small client of the same gateway.

## Files

- `app.json` — the manifest (`platform.installMode: "client"`, `os: ["darwin"]`).
- `run.py` — the launcher (`--configure` / `--check` / live).
- `menubar_companion/settings.py` — local preferences + credentials (0600).
- `menubar_companion/api.py` — the three gateway calls, stdlib `urllib` only.
- `menubar_companion/model.py` — the rendered state; every number derived.
- `menubar_companion/doorbell.py` — the one socket, payload-blind, backoff-reconnecting.
- `menubar_companion/notify.py` — native notifications and the mute gate.
- `menubar_companion/tray.py` — the menu as data, plus the status-item host seam.
- `menubar_companion/app.py` — the wiring; the only `Doorbell(...)` in the package.
- `test_*.py` — the app's own tests.

## Tests

```sh
python3 -m pytest menu-bar-companion -q
```

No PersonalClaw core import anywhere outside `test_manifest.py` (which checks the
manifest against core's own `AppManifest.from_dict`, and skips when core is absent).
