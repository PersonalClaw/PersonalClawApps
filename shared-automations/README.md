# Shared Automations

Serve trigger rows from one shared automations file — a synced folder, an NFS share, a
checked-out team repo. Everyone pointed at the file sees the same automations on their
Automations page, and each machine arms and fires **only** the rows attributed to its own
owner. No server and no credentials.

**Shared Automations** is a **trigger provider** — it implements the
`personalclaw.sdk.triggers` `TriggerStoreProvider` contract, so its rows appear beside your
local ones and are fired by your own harness under all of your own gates.

## What this is

A standalone PersonalClaw app bundle. It ships as a self-contained directory:

- `app.json` — the manifest (`provider.type: "trigger"` + `implementation`).
- `provider.py` — the implementation, exposed via `create_provider`.
- `test_provider.py` — the app's own tests.
- `team-automations.example.json` — a runnable example file (see below).

It imports only the PersonalClaw **SDK** (never core internals):

- `personalclaw.sdk.triggers`

## Rows, never execution

This app answers one question: *which automations exist*. It never observes anything and
never executes anything — it is never handed a fire, a payload, a run or a credential. Your
local harness does all the firing, and every gate it applies to a local automation applies
here unchanged: capability allowlist, budget, quiet hours, kill switch, injection screen.

`trigger` is not `trigger_source`. A **trigger source** supplies the *stimulus* (a live
observer pushing events). This supplies the *rule* (a passive store of definitions). An app
may register as both.

## Who can run what

Core arms and fires only rows whose `author` matches the local owner's username
(Settings → Account). Everyone else's rows:

- **render read-only** — they are listed on the Automations page with an author chip, no
  Edit, no Run now, no Dry run, no Delete; and
- **structurally cannot arm** — core drops them before the arm path is handed a single row,
  so there is no code path on your machine that could decide to run a teammate's automation.

An **unattributed** row (`"author": ""`) reads as the local owner's on *every* machine, so a
multi-user file that omits the field will have everybody arming everything. Attribute your
rows. The shipped example leaves the shared rows unattributed on purpose — so it runs for
whoever installs it — and attributes two rows to `alice` so you can see read-only rendering
without inventing a teammate.

## Write-back

When core fires one of your rows it writes the row's next schedule (plus `run_count`,
`last_fired_at` and the health fields) back **into this file**. That is what makes a shared
automation fire once rather than once per tick, and it is why `upsert` here really writes and
`get` really reads back.

Core verifies it: after a routed write it re-reads the row and checks `next_fire_at` actually
moved. A store that accepted the write and kept the old value is **quarantined** — its rows
stop being armed for the rest of the process and a warning is logged — instead of firing on a
frozen schedule every tick. So keep the file writable on the machine that runs the
automations; a read-only mount will cost you one fire and then go quiet, loudly.

A row whose id also exists in your local `triggers.json` is **not armed**: the local row wins,
because one identity cannot hold two schedules. Both stay visible so the collision is
reportable. Namespace your ids.

## File format

Core's own `triggers.json` envelope, so a team can share a copy of somebody's store unchanged:

```json
{
  "version": 1,
  "triggers": [
    {
      "id": "team-morning-brief",
      "name": "Team morning brief",
      "kind": "clock",
      "enabled": true,
      "author": "",
      "spec": {"kind": "cron", "expr": "30 8 * * 1-5"},
      "workflow": {"provider": "run-workflow", "config": {"workflow": "morning-brief"}},
      "capabilities": {"providers": ["run-workflow"]}
    }
  ]
}
```

A bare JSON list is read too. Every write is atomic (temp file in the same directory, then
`os.replace`), because a synced folder caught mid-write is worse than a missing one.

Reads never raise. An unmounted folder, a file mid-sync, or a teammate's broken hand-edit
costs *this app's* rows for that pass and nothing else — your local automations keep arming.
A row with a bad cron expression stays visible and inert rather than vanishing.

## Install

From the App Store, add the apps directory as a **local source**, then install **Shared
Automations** — the install runs through the security scanner and lifecycle like any other
app. (Or `POST /api/apps {"source": ".../apps/shared-automations"}`.) Then set the file path
below. To try it immediately, point `path` at a copy of `team-automations.example.json`.

## Settings

| Key | Label | Notes |
|---|---|---|
| `path` | Shared automations file | Absolute path to the shared JSON file holding the team's trigger rows (e.g. `~/synced/team/automations.json`). Supports `~` and `$VARS`. Leave empty to configure later — the store serves no rows until it is set. |

## Permissions

| Permission | Why |
|---|---|
| `storage` | Reads and writes the one shared automations file you point it at. Nothing else. |

No `network`: this app talks to a filesystem path and nothing else. Pair it with **Folder
Sync** (or git, or any sync client) if the folder needs to reach another machine.

## Tests

```
python -m pytest shared-automations -q
```

Pure filesystem — no gateway, no firing, no network. Covers the store contract, the read-side
tolerance, the write-back round trip core depends on, the change-notification, and the shipped
example (including that its `alice` rows are attributed and enabled — inert by ownership, not
by a toggle).

## License

MIT — see `LICENSE`.
