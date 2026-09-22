# Folder Notification Drop

Deliver a teammate-addressed notification into a shared folder, one JSON file per note.
Pairs with **Folder Sync** or **S3 Sync**: that folder is how the note actually reaches the
other person's machine. No credential, no network permission, no vendor.

**Folder Notification Drop** is a **notification delivery backend** — it implements the
`personalclaw.sdk.notification` `NotificationDeliveryProvider` contract and becomes a
delivery route for foreign-addressed notifications as soon as it is installed and enabled.

## What this is

A standalone PersonalClaw app bundle (part of the core/app workspace split). It ships as a
self-contained directory:

- `app.json` — the manifest (`provider.type: "notification"` + `implementation`).
- `provider.py` — the implementation, exposed via `create_provider`.
- `test_provider.py` — the app's own tests.

It imports only the PersonalClaw **SDK** (never core internals), so core can evolve without
breaking it:

- `personalclaw.sdk.notification`

## Why this app exists

It is the **reference app for the `notification` provider type**, which had no example
anywhere: no first-party app, no published exemplar, and no core reference implementation
either — core ships the ABC and the registry and nothing that implements them. If you are
writing your own delivery backend (Slack DM, SMS, a team webhook), start here and replace
`deliver`.

## What problem the type solves

`DashboardState.notify` is the single choke point every notification passes through, and
every destination behind it is **local**: a toast on this dashboard, a row in this digest, a
push to this owner's phone, a banner on this desktop. That is correct while every row in
every store belongs to the owner. Once a shared store contributes rows somebody else owns, a
note can be *about* a teammate — and firing it here spends the wrong person's attention.

So a notification carries an **addressee**. A foreign-addressed note is:

- **recorded** locally (it stays visible in the bell and `GET /api/notifications`),
- **fired** nowhere locally, and
- **offered** to each registered `type=notification` backend until one says it can reach the
  addressee.

With no such backend installed, nothing routes it and the note is visible-but-inert —
"nobody could reach them" must never read as "delivered". This app is what turns that into a
real delivery.

## Install

From the App Store, add the `apps/` directory as a **local source**, then install **Folder
Notification Drop** — the install runs through the security scanner and lifecycle exactly
like any other app. (Or `POST /api/apps {"source": ".../apps/dir-notification"}`.) Then set
the two settings below.

## Settings

| Key | Label | Notes |
|---|---|---|
| `drop_dir` | Drop folder | Absolute path to the shared folder notes are written into (e.g. `~/synced/pc-notes`). Supports `~` and `$VARS`. One subfolder per addressee. Empty means the backend declines everything and stays idle. |
| `roster` | Roster | Comma-separated usernames this folder can reach (e.g. `sam, dana`). A note addressed to anybody else is declined so another delivery backend can take it. Empty means the backend declines everything. |

Usernames are the same attribution slugs the rest of PersonalClaw uses (`owner_username`),
compared case-insensitively.

## How it works

Each accepted note is written to `<drop_dir>/<addressee>/<ts>-<title>.json` — the whole note,
title and body included, which is the same content an `inbox` or `channel` provider already
handles. The write goes to a `.part` file and is then renamed into place, so a syncing folder
never publishes a half-written note.

## The three things a delivery backend has to get right

These are the contract's real edges, and this app's tests pin each one:

1. **`addresses` is the routing decision, and it is yours.** Core knows the addressee string
   and nothing else about who that is. First acceptance wins, so answering `True` for a
   username you cannot actually reach is a silent black hole — it takes the note out of every
   other backend's reach. This app therefore declines when it is unconfigured rather than
   accepting and failing.
2. **`deliver` must not claim a delivery that did not happen.** Its return value is what the
   registry records as `routed_to` on the note. `True` after a failed write stamps a route
   onto something that went nowhere, which reads as delivered. Every failure path here
   returns `False`.
3. **`delivery_name` is the provider's own name, not the app's.** It is the registry key, the
   string a delivered note records, and the key a *disable* removes. A phantom route that
   outlives its app is worse than a visibly withheld note.

One more, specific to this transport: the addressee arrives from a shared store and the title
is arbitrary text, so neither is allowed to shape a path. Both are slugged before they reach
the filesystem, and `deliver` re-reads the addressee from the note rather than trusting the
preceding `addresses` call.

## Security posture

- **No credentials, no network.** The app declares no network permission and holds no
  account or token. It reads and writes files in the folder you choose; whatever protects
  that folder is the only trust boundary.
- **The note travels whole.** Title and body are written to the folder. That is the same
  trust model as an `inbox` or `channel` app — an app the owner installed, with declared
  permissions — and deliberately not the content-free boundary `personalclaw.push` enforces,
  which exists because a third-party push *service* is a different party.
- **Anyone with the folder can read the notes.** Share it only with the machines and people
  on your roster.

## License

MIT — see `LICENSE`.
