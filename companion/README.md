# Companion

An opt-in day companion. Reminders that actually go off, a watchlist that speaks up when a
folder really changes, and a day plan you can ask for. Three surfaces, each switched on
separately, and **all three off out of the box** — installing this app arms nothing at all.

**Companion** contributes **two** providers from one bundle: a **tool provider**
(`personalclaw.sdk.tool`, six tools on the agent tool layer) and a **trigger store**
(`personalclaw.sdk.triggers`, the rows those tools produce). The tool answers *"change my
day"*; the store answers *"which automations exist"*.

## Why `tool` + `trigger` and not the others

The tool half is the same reasoning every app in this suite reached: `agent` in this platform
means an **ACP agent bundle** (`claude-code-agent`, `codex-agent`) — a coding CLI you pick in
the Agents list, not a task an agent performs. `workflow` is a real `PROVIDER_TYPES` entry but
publishes no SDK contract, so an app cannot build against it without breaking the SDK-only
boundary. What this app's six calls are — capabilities the agent invokes, with arguments, that
read and write the user's own day — is exactly the `tool` contract.

The **`trigger`** half is what makes this app more than a notepad, and it is the reason the
bundle declares two providers instead of one. `trigger` (`TriggerStoreProvider`) contributes a
**store of trigger rows**: it answers "which automations exist" and core does every bit of the
firing, under all of its own gates. Its sibling `trigger_source` is the wrong contract here —
that one contributes a live *observer* that pushes events onto the bus, and this app observes
nothing; it holds definitions.

That choice is also what makes the app's central promise **structural rather than a policy**:

> Every surface is opt-in, and disabling it removes all its triggers.

There is no companion row anywhere in PersonalClaw's own `triggers.json`. Core re-reads this
app's store on each pass, so a store nobody reads serves nothing. Switch a surface off, disable
the app, or uninstall it, and its automations are gone from the Automations page — with no
migration, no cleanup step, and nothing left behind to fire.

There is no `ui/` in this bundle, so no design-system or a11y claim is made. The companion's
surface is the chat tool layer plus the platform's own Automations page and notification
inbox — the surfaces the user already has. A desktop *window* already exists as a separate
app: [`menu-bar-companion`](../menu-bar-companion) is a macOS status-bar client for a
PersonalClaw you already run. The two are complementary and deliberately not merged — that one
is a client installed on *your* Mac, this one is a gateway-side app that owns items and rows.

## The six tools

| Tool | Risk | Approval | What it does |
|---|---|---|---|
| `companion_status` | safe | — | Which surfaces are on, how many automations each contributes, which timezone they use. |
| `companion_remind` | caution | — | Add a reminder: `at` for a one-shot, `cron` for a recurring one. |
| `companion_watch` | caution | — | Watch a file, folder or `folder/*.ext` glob for real content changes. |
| `companion_list` | safe | — | Every reminder and watch, with its id and whether it is armed, paused or delivered. |
| `companion_day_plan` | safe | — | Today: overdue, due before midnight, recurring, watched. |
| `companion_dismiss` | destructive | **yes** | Remove one reminder or watch — its automation goes with it. |

```
> remind me to call the dentist tomorrow at 9
Reminder `4f2a91cc` is set at 2026-09-07 09:00 (Europe/Berlin). It will raise a
notification — nothing else runs.

> what's my day look like
# 2026-09-07 — Europe/Berlin
**Due today**
- 09:00 — Call the dentist
**Watching**
- `/Users/me/work/inbox/*.md` (drafts)
```

## Three surfaces, and what "off" means

| Surface | Setting | Contributes | Off means |
|---|---|---|---|
| Reminders | `reminders` (default off) | one `clock` trigger per pending reminder | `companion_remind` declines; every reminder automation disappears; the reminders themselves are **kept**, so switching back on restores them |
| Watchlist | `watchlist` (default off) | one `file` trigger per watched path | `companion_watch` declines; every watch automation disappears; the watches are kept |
| Day brief | `day_brief` (default empty) | one `clock` trigger at that time of day | no daily nudge; `companion_day_plan` still works on demand |

`companion_list` reports a surface-off item as **`surface off`** rather than hiding it, because
"you have four reminders and none of them are armed" is a thing a person needs to be told.

## What a companion automation is allowed to do: notify, and nothing else

Every row this app serves fires the platform's `notify` action, and `capabilities` is frozen at
`{"providers": ["notify"]}`. That is a deliberate ceiling, not a stage on the way to something
bigger.

A reminder that fired `run-prompt` with the user's own text would be **untrusted text becoming
a durable, scheduled instruction with nobody in the loop** — the exact shape the platform's
input-handling rules exist to prevent. So the day brief *nudges* rather than drafts: it raises a
notification at the hour you chose, and the plan itself is rendered by `companion_day_plan`, in
a session you are present for. That is smaller than "the companion writes your morning brief",
and it is stated here rather than dressed up.

The ceiling is enforced by shape, not by a check that could be forgotten:

**The store persists ITEMS, never rows.** A reminder on disk is a title, a note, and a time. The
trigger row — its `kind`, its `workflow` action, its `capabilities` — is synthesised in code on
every read, by one function that has no parameter for an action. So there is no field anywhere
in the file where an action could be written, and no hand edit, stale sync copy or agent tool
call can turn a companion row into an LLM run. A test drives exactly that: it injects
`workflow: {provider: "run-prompt"}`, `capabilities: [run-prompt, invoke-agent]`,
`kind: "webhook"` and a top-level `triggers` array into the store file, and the rows that come
back out are still `clock`/`notify`/`["notify"]`.

## Timezones, said out loud

Core's arm path treats a `clock` trigger with no `spec.timezone` as **UTC**. On an unconfigured
machine that means an 08:30 reminder fires at 08:30 UTC — 10:30 in Berlin. So:

- the `timezone` setting takes an IANA name and an unknown one is **refused at authoring**
  rather than silently falling back;
- empty reads the machine's own zone from the `/etc/localtime` symlink (`time.tzname` is not
  used — it yields `CEST`, which `ZoneInfo` cannot take, and a value that looks right and
  resolves to nothing is worse than an empty one);
- if neither is available, `personalclaw doctor` reports it as a **warning** with the
  consequence spelled out, and every row is authored in UTC honestly rather than in a guess.

A naive `at` ("2026-09-07T09:00") is read in that zone, once, at authoring time — core stores
`spec.at` as an epoch, so leaving the ambiguity for fire time would resolve it when nobody is
watching.

## Security posture

Every value in this store arrives from outside — typed by a person, written by an agent, or
read back from a file that may have been edited. Applying the ARCC input-validation guidance
(SAX-04 boundary validation, SAX-06 log injection) edge by edge:

- **A watch path becomes a filesystem glob core walks**, so it is validated per *segment*
  before it is ever a path. Each segment is `[one optional dot]alphanumeric…`, which makes `.`,
  `..` and a `-oProxyCommand=` lookalike **unrepresentable** rather than filtered — while
  `~/.config/nvim`, which people really do want to watch, still works. A glob (`*`, `?`) is
  allowed only in the final segment and `**` never, because a recursive glob on a hand-typed
  path turns a watchlist into a filesystem crawl. The result must be absolute; depth and length
  are capped. `~` is expanded and **`$VARS` deliberately are not** — resolving an environment
  variable out of a typed pattern is a way to make this app read a value the user did not type.
- **PersonalClaw's own config dir is excluded**, for a behavioural reason rather than a secrecy
  one: the platform writes there continuously, so a watch on it would fire on the platform's own
  bookkeeping every pass — an automation that can never be quiet. A sibling directory with the
  same *prefix* is not excluded; a test pins both directions.
- **A cron expression is persisted and re-read forever**, so it is parsed field by field against
  a closed grammar rather than stored as free text: `30 8 * * 1-5;id`, `$(id) 8 * * *` and
  `30 8 * * 'MON'` are all refused. A 15-minute floor mirrors core's own
  `MIN_CLOCK_INTERVAL_SECS`, and it targets the two spellings that produce a runaway by accident
  (`* * * * *` and `*/n` under fifteen). A hand-written minute list is a deliberate choice and is
  not second-guessed.
- **`$` is excluded from every string that becomes a `notify` template.** `notify` renders
  `title_template`/`body_template` through core's `$EVENT`/`$CONTEXT`/`$<payload-key>`
  substituter, so a reminder titled `$CONTEXT` would expand into whatever the fire payload
  holds. The exclusion happens at row *synthesis*, not at storage — so the user's own text stays
  intact everywhere a human reads it, and expansion is impossible everywhere a machine does.
  Pinned in both directions: `"$CONTEXT then $EVENT"` → `"CONTEXT then EVENT"`, and
  `"Call the dentist"` → unchanged.
- **A reminder's `note` never enters a trigger row at all.** Only the one-line title reaches the
  notification. The note is where a pasted paragraph from a web page or a ticket ends up, and
  keeping it structurally out of the automation payload is stronger than fencing it there.
- **Everything handed back to a model is fenced** with
  `personalclaw.sdk.security.fence_untrusted` — the `companion_list` tables, the day plan, and
  the echoed watch path. A test asserts a title containing the closing marker cannot break out
  of its own fence.
- **Nothing content-shaped is logged.** The log carries an item id, whether a reminder is
  one-shot or recurring, a watch's *depth* in segments, and retirement verdicts — never a title,
  a note, or a path. A test greps every `logger.` line in `provider.py` for exactly that.
- **The store file is written `0600`** and atomically (`personalclaw.sdk.util.atomic_write`):
  these are the user's reminders and the paths they care about, on a machine that may have other
  accounts on it.
- **Caps, not trust.** 200 reminders, 50 watches, 200-character titles, 4 kB notes, 400-character
  paths, 24 segments deep, a 5-year horizon on a one-shot. Each one **refuses** rather than
  silently dropping.

The one thing this app cannot defend against is a `notify` body a person chooses to be misled
by: a reminder is text the user (or their agent) wrote, delivered back to them. It is capped to
one printable line so it cannot forge a second notification, and that is the whole of the claim.

## Staying out of quarantine — the trigger-store contract

Core verifies an app-served store rather than trusting it. When it fires one of these rows it
writes the row's next schedule **back here**, then re-reads the row to check the timestamp
moved; a store that accepts the write and serves the old value is **quarantined** (its rows stop
arming for the rest of the process) because it would otherwise fire every tick forever. Three
consequences this bundle implements and tests:

- `upsert` really persists the runtime rollups and `get` really overlays them — including
  `run_count`, `last_fired_at`, `state`, the health fields, and `enabled`, so a pause the user
  set on the Automations page **sticks** across a re-read.
- A write-back can only touch those rollups. The title, the note, the path and the schedule are
  the user's; a store that let a fire path edit them would let an automation silently retitle a
  reminder.
- `delete` really removes, because core checks afterwards. That includes the **day brief**, which
  is the one row with no item behind it (it is driven by a setting this app cannot write) — so a
  retired id is remembered, and the brief's row id carries its time
  (`companion:day-brief:0830`) so changing the time in Settings mints a fresh one rather than
  making the retirement permanent.

A one-shot reminder carries `delete_after_run` and *also* stops being served once its runtime
record shows a fire. The second guard is gated on `run_count`/`last_fired_at` rather than on
`next_fire_at`, because core persists the next fire time **before** it executes — gating on that
would cancel the very fire about to happen. Both directions are tested.

Rows are namespaced `companion:*`, because a row whose id also exists in the owner's local
`triggers.json` is not armed (the local row wins). `author` is left empty, which core reads as
the local owner's: this is a single-machine personal store, not a team one.

## Install

From the App Store, add this `apps/` directory as a **local source**, then install
**Companion**. (Or `POST /api/apps {"source": ".../companion"}`.) There is nothing else to
install — no binary, no credential, no network. After installing, turn on the surface you want:
nothing happens until you do.

## Settings

| Key | Label | Notes |
|---|---|---|
| `reminders` | Reminders | Default **off**. On, reminders can be added and each pending one is an automation. |
| `watchlist` | Watchlist | Default **off**. On, watches can be added and each one is an automation. |
| `day_brief` | Day brief at | 24-hour `HH:MM`, or empty for off. `08:30:00` is refused rather than truncated to something you did not type. |
| `timezone` | Timezone | IANA name. Empty reads `/etc/localtime`, else UTC. An unknown name is refused. |

Nothing folds behind **Advanced**: all four are first-run decisions, and the repo's
settings-schema rail only asks tuning-class names (`timeout_secs`, `*_endpoint`, `base_url`,
`*_bin`) to fold. Nothing is `required` either — an app that refused to mount without
configuration could never be configured.

## Permissions

`storage: true` (the items and the file-watch state), `network: false`. That is the whole
declaration, and it is backed by a test: the bundle's sources are AST-scanned for `socket`,
`ssl`, `http`, `urllib`, `requests`, `httpx`, `aiohttp`, `smtplib`, `ftplib` and `subprocess`,
and there are none. This app shells out to nothing and opens no connection.

## Tests

`test_provider.py` — 162 tests: the watch-path grammar and every refusal (traversal, empty
segments, `**`, a glob outside the last segment, an option-lookalike segment, a forged log line,
a NUL, an unexpanded variable, over-length, over-depth, PersonalClaw's own home and the sibling
that is not it), the cron grammar and its floor on both sides, `at` in three zones plus the
horizon, the `$`-exclusion in both directions, the zone readers (symlink shapes, missing
symlink, and the CLI copy agreeing with the provider's), the shipped all-off default, each
surface serving only its own rows, all-off keeping the items, a refused setting degrading instead
of blocking the mount, every row parsed by **core's own `parse_trigger`** with zero error issues,
the notify-only freeze, a hand-injected action in the store file being ignored, the write-back
round trip and the exact read-back check core makes, a write-back that cannot edit the user's
fields, a pause that survives, delete for a reminder, a watch and the brief, the pre- vs
post-execution one-shot retirement, malformed-store robustness, `0600`, the no-mkdir-on-construct
rule, the caps, the day-plan buckets under a frozen clock, the zone deciding where today ends,
the whole tool surface with its risk levels and approval flags, fencing and a fence-break, the
manifest round-trip and both provider declarations, and the CLI seams.

```
python -m pytest companion -q
```

There is no `test_server.py`: this app declares no `backend`, so it has no server to test. In
this repo only `growth` and `minutes` — the backend+UI apps — ship one.

## Validated / not yet validated

Stated plainly, because the difference matters.

**Validated:**

- 162 tests green under the repo's `tests` job posture (core installed, no vendor SDKs,
  `PERSONALCLAW_SKIP_APP_BACKENDS=1`).
- `app.json` parses against core's own `AppManifest` and round-trips stably, with both providers
  (`tool` + `trigger`) resolving to their declared implementations.
- SDK-only imports (`personalclaw.sdk.{tool,triggers,security,util,cli,settings,manifest}`) —
  clean under the repo's `boundary` AST lint.
- The cross-app rails: `settings-schema-posture`, `prompt-cache-posture`,
  `live-writes-posture`, `quality-declarations`.
- Every row this app can produce is accepted by **core's real `parse_trigger`** with no error
  issues — a `cron` reminder, an `at` reminder, a `file` watch, and the day brief.
- The `TriggerStoreProvider` write-back contract, driven exactly as core drives it: upsert,
  read back, compare `next_fire_at`; delete, read back, expect `None`.

**Not yet validated — the remaining legs, for the owner or a live session:**

- **Local-Store install + a real UI drive.** Adding this directory as a local Store source,
  installing it in a running gateway, and driving `companion_remind` from chat has *not* been
  done. The install/quarantine/scan path, the Store listing, and the Settings → Tools rendering
  of this manifest are unverified in the real UI.
- **A real fire.** No reminder has actually gone off. The rows are proven to be *acceptable* to
  core's parser and the write-back contract is exercised against the store, but no gateway tick
  has armed one of these rows, dispatched `notify`, and raised a notification. The claim that a
  reminder fires rests on core's own `arm`/`dispatch` behaviour, not on an observed fire.
- **A real `file` trigger poll.** Same shape: the row is a valid `file` trigger and `base_dir` is
  a real writable directory, but core's `file_poll` has never walked one of these globs.
- **A real disable/uninstall cycle.** The "disabling removes every trigger" claim is proven
  *structurally* (rows exist only in this store, surfaces gate what it serves, and the tests
  drive all-off → zero rows) and rests on core's documented "a provider that is not registered
  is not read" behaviour — but no gateway has actually deactivated this app and re-read the
  Automations page.
- **`personalclaw setup` / `doctor` in a real CLI run.** Both seams are unit-tested; neither has
  been rendered by the actual CLI.
- **The exemplar list.** This app is not recorded in ECOSYSTEM-TOOLING's exemplar list — that
  list lives outside this repo and is owned elsewhere.

None of these are faked or asserted as done anywhere in this bundle.

## License

MIT — see `LICENSE`.
