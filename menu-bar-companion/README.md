# Menu Bar Companion

A macOS menu-bar presence for a PersonalClaw you already run. It sits in the status bar
on **your** Mac, shows what is happening and — more to the point — what is *waiting on
you*:

- **Everything waiting on you**, every kind, in the order PersonalClaw keeps everywhere (what
  answering it lets happen, then how long it has waited), each row its kind and one line in
  its source's words, as Home and the desktop menu bar say it: `Approval: …`, `Question: …`,
  `Loop: …`, `Workflow: …`, `Automation: …`, `Browser: …`, `Plan: …`, `Control bridge: …`.
- **A tool approval** opens the brief of the call — what it would run, where it came from,
  what it can touch, what **Deny** does — above its **Approve** and **Deny**.
- **Every other kind** opens where it is answered in the dashboard, one click away (a
  loop's question its loop, a workflow's gate its run), with what it asks under its row.
- **Everything running**, each saying what it is doing, a click from its page.
- **A badge** counting everything waiting on you, every kind. Nothing else.
- **Notifications** for each new thing waiting on you that PersonalClaw says may interrupt
  you now (not muted, not in your quiet hours), and **Mute notifications** from the Settings
  item.

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
| `CompanionModel.refresh()` | Takes no server-supplied argument either. It re-reads `GET /api/waiting` and `GET /api/running` and replaces state from the HTTP response. |

`test_doorbell.py` proves it end to end: a frame carrying 99 fabricated approvals rings
the doorbell and changes the badge to what **HTTP** said (3), with the sentinel absent
from everything rendered — and the same test file contains a payload-consuming reader
that *does* surface the 99, so the assertion is discriminating rather than vacuous.

**A burst of frames is one read.** The socket hears every frame the gateway sends, a
streamed chat's every chunk among them, and it cannot tell one from another without reading
it. So a ring only says a read is wanted (`coalesce.Coalescer.ring()`, which takes no
argument either), and one refresher thread does every read: at once when a second
(`coalesce.GAP_SECS`) has passed since the last read, otherwise when it has. A burst of any
size is one read at its start and at most one at its end, a steady stream is one read a
second, and the first change after a quiet spell, a new approval among them, shows at once.
The floor poll and **Refresh now** ring the same way, so no read runs on the socket's thread. `test_coalesce.py` sends a burst of 50 frames through the real
doorbell and counts 4 requests, where a read per frame would be 100.

Control frames are not rings: the gateway's 30-second heartbeat `PING` is answered with a
`PONG` and does **not** trigger a refetch, or the socket would just be a 30-second poll
wearing a socket's clothes.

Reconnection uses a growing backoff — 1s, 2s, 4s, 8s, 16s, capped at 30s — and a
successful connect resets the ladder.

There is also a **floor poll** (default 60s). It is not a fallback for reading payloads;
it is the guarantee that a change which produces no frame at all still lands.

## Everything shown is derived

What it shows is read from two lists and nothing else: `GET /api/waiting`, everything waiting
on you, and `GET /api/running`, everything running. They are core's, and the core features the
manifest requires (`requiresCoreFeatures: ["running-list", "waiting-list"]`), so a PersonalClaw
built before them refuses to install this version rather than serving it an empty menu. The
menu keeps the list's order and never sorts it again. A kind of item this version has no name
for is still listed and counted, as `Waiting on you`, and a source the gateway could not read
is said (`⚠ Could not read: …`), never shown as nothing waiting.

The badge is a `@property` over the same waiting list the menu renders, computed on every
read. There is no counter kept beside it — a count maintained next to a list is two
facts that can disagree, and the one the user sees is the wrong one.
`model.INSTANCE_ATTRS` pins the model's whole attribute set so that adding a cache later
fails a test instead of drifting quietly.

## Nothing is approved blind

Each pending approval is a row that opens a submenu, and **Approve** is only ever inside it,
under the brief of the call. The brief is what the dashboard's approval card shows, from the
same fields its waiting item carries (the approval registry's own), in the same words:

```
Permission needed to run bash
  {"command": "rm -rf build/cache"}
Clear the stale build cache                              ← the purpose the runner gave
From loop “Fix the README”                               ← where it came from
Can: writes files, runs a command · Risk: Destructive    ← what it can touch, its risk
It reaches packages.example.com, which is not on your …  ← when it reaches past your hosts
Jonas (U0JONASCOL) on teamchat asked for this, not you…  ← when someone else asked for it
The agent CLI offers no way to skip only this step: …    ← when Deny does more than decline
Approve
Deny
Review in PersonalClaw
```

A menu holds less than a card, so every part is shown **whole or not at all**:

- What will run is shown character for character. A line wider than the menu goes on in
  the lines under it, each starting `↪`, so a wrapped line never reads like a line break in
  the command. A character that prints nothing, or moves the text around it, is shown as its
  code point, such as `\u200b` for a zero-width space.
- A part too long for the menu (a long script, a file's whole contents) is replaced by a line
  saying so, and **Approve** is not offered: **Review in PersonalClaw** opens the approval in
  the dashboard, which shows all of it. **Deny** stays while what Deny does is shown whole.
- A row the menu cannot read — no tool, a field of the wrong kind, a risk or a facet this
  version has no words for — says so and offers only **Review in PersonalClaw**.

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
  "network": true,
  "api": ["/api/waiting", "/api/running", "/api/approvals", "/api/ws"],
  "events": ["waiting_changed", "running_changed"]
},
"requiresCoreFeatures": ["running-list", "waiting-list"]
```

That is the whole list, and each entry is used:

- `network` — its one connection is your own gateway, at the URL you give it, which can be
  on another machine. It reaches nothing else.
- `/api/waiting` — everything waiting on you, the rows and the badge (read-only).
- `/api/running` — everything running (read-only).
- `/api/approvals` — the one write this app makes: Approve or Deny on a tool approval.
- `/api/ws` — the single doorbell connection.
- `events` — the two lists' own frames, each saying only that its list moved. The app rings
  on *any* frame and reads no payload, so it deliberately does not claim a broader event set;
  the floor poll covers anything a narrow grant would filter out.
- `requiresCoreFeatures` — the two lists, by the names core gives them.

Nothing else is claimed. In particular:

- **no `storage`** — a client app is never server-installed, so the platform never hands
  it a `DATA_DIR`. It keeps its own settings on your Mac (below).
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
gateway — hence 0600. It is sent only in the `Authorization: Bearer` header of each request
and of the socket's upgrade, never in a URL: not in a request line a log keeps, not in the
output of `--check`, and not in a link the menu opens. A loop's link and **Review in
PersonalClaw** open the dashboard with no credential in them, so your browser signs in the way
it always does there: with the session it already has, else on the gateway's sign-in page,
which takes you on to the same page afterwards.

If you would rather not persist the token, supply both by environment instead and nothing is
written:

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
menu would show (each approval's brief and the answers it offers included), and reports
whether a status-item backend is present.

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
- `menubar_companion/brief.py` — what an approval shows before Approve, whole or not at all.
- `menubar_companion/model.py` — the rendered state; every number derived.
- `menubar_companion/doorbell.py` — the one socket, payload-blind, backoff-reconnecting.
- `menubar_companion/coalesce.py` — the refresher: one read per burst of rings.
- `menubar_companion/notify.py` — native notifications and the mute gate.
- `menubar_companion/tray.py` — the menu as data, plus the status-item host seam.
- `menubar_companion/app.py` — the wiring; the only `Doorbell(...)` in the package.
- `test_*.py` — the app's own tests.

## Tests

```sh
python3 -m pytest menu-bar-companion -q
```

No PersonalClaw core import anywhere outside two tests, each of which skips when core is
absent: `test_manifest.py` checks the manifest against core's own `AppManifest.from_dict`,
and `test_brief.py` checks the brief's risk and facet words, and its summary line, against
core's own (`personalclaw.approval_brief`), so the menu cannot drift from the dashboard.

## What it starts

Install consent names these before anything installs (the manifest's `launches`). Both run on
your Mac, from the companion's own process:

- `open` opens the PersonalClaw dashboard in your browser when you pick something waiting on
  you or something running, or **Review in PersonalClaw** on an approval, from the menu.
- `osascript` shows a macOS notification, unless you mute notifications in its Settings
  menu.

## Network

Reaches only the PersonalClaw gateway URL you give it (`run.py --configure`, or `PERSONALCLAW_COMPANION_URL`).
