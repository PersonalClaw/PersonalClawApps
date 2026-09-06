# Research Lab

**Ask one question, walk away, come back to a report.**

Research Lab turns a question into a tree of sub-questions and works that tree down over
many unattended cycles: each cycle takes a few open sub-questions, hands each one to its
own subagent, records what came back with its sources, and grafts on whatever new
questions the work turned up. When the tree is answered — or the cycle budget runs out —
it synthesises everything into one markdown report on your machine.

**Research Lab** is a **tool provider** — it implements the `personalclaw.sdk.tool`
`ToolProvider` contract and its five tools appear on the agent tool layer.

## Why `tool` and not `agent` or `workflow`

`agent` in this platform means an **ACP agent bundle** (`claude-code-agent`,
`codex-agent`) — a coding CLI you select in the Agents list, not a task an agent performs.
`workflow` is a real `PROVIDER_TYPES` entry but publishes no SDK contract, so an app cannot
build against it without breaking the SDK-only boundary. What this app actually is — a
capability the agent *calls*, with arguments, that returns a report — is exactly the `tool`
contract, per the capability table in
[`docs/app-creation-guide.md`](../docs/app-creation-guide.md).

The multi-cycle half is not a provider type at all. It is a **declared cron**
(`crons[]` in `app.json`), which is how an app gets an unattended, headless, auto-approved
turn without inventing a scheduler of its own.

## The app is the ledger, not the researcher

This is the part to read before anything else.

The app stores campaigns and decides what to work next. It does **no fetching, no
summarising and no model calls** — the agent already does all three, better, and a
`ToolProvider` is constructed with its settings dict and nothing else (no services object,
no session, no model handle). So this app **cannot spawn a subagent itself, and does not
claim to.** What it does instead is publish the *worklist* and the *write-back tool*, and
the host agent — woken by the cron — spawns one subagent per sub-question and reports each
finding through `research_record`. That is the same seam `code-review` calls its `plan`
fan-out, and it is the only leg in this repo that gets genuinely isolated per-item
subagents.

Which is why the manifest declares `storage` + `cron` and nothing else. No `network`, no
`agent`, no `api`. If you were expecting a web-search provider, that is a different app;
this one composes with whichever one you have.

## The five tools

| Tool | What it does |
|---|---|
| `research_open` | Open a campaign: a question, optional starting sub-questions, a cycle budget. |
| `research_list` | Every campaign with its status, cycles used and progress. |
| `research_next` | Close the open cycle, hand back the next worklist. Reports `done` when the tree is answered or the budget is spent. |
| `research_record` | One sub-question's finding + sources, plus any follow-up questions it raised. The write end of the fan-out. |
| `research_report` | Synthesise findings, open questions and sources into `report.md`. |

```
research_open(question="Does local-first sync beat a cloud broker?",
              sub_questions=["What do local-first users lose?", "What does a broker cost?"],
              cycle_budget=4)
research_next(campaign="does-local-first-sync-beat-a", breadth=2)
research_record(campaign="does-local-first-sync-beat-a", node="q1",
                finding="…", sources=["https://…"], follow_ups=["…"])
research_report(campaign="does-local-first-sync-beat-a")
```

## The unattended loop

`app.json` declares one cron, `advance-campaigns`, which runs hourly and does exactly one
cycle per tick:

1. `research_next` — if it says `done`, call `research_report` once and stop.
2. Otherwise research each sub-question in the worklist **in its own subagent, in
   parallel**, so one slow question does not stall the cycle.
3. `research_record` per answered sub-question, with sources and any follow-ups.
4. Stop. The next tick runs the next cycle.

App crons are headless and auto-approved (`delivery: none`, `persistent_session: false`),
so nothing waits for you and no cycle drags the previous one's context along. Disable or
uninstall the app and the cron goes with it.

### Why it terminates

A loop that can always find more to do never stops, so the app refuses to let a campaign
grow without limit:

- **Cycle budget** (default 5, per campaign) — a spent budget ends the campaign as
  `exhausted`, and the report says which questions are still open rather than pretending
  they were answered.
- **Depth cap** (3) — a follow-up question three levels below the root is dropped rather
  than grafted.
- **Duplicate drop** — the same sub-question arriving from two branches is asked once.
- **Node ceiling** (200) plus length caps on questions, findings and sources.

`research_next` reporting `done` is a **success**, not an error. That matters: a failure
would read as transient and keep the cron retrying a finished campaign forever.

## Campaigns kept locally — literally

A campaign lives at
`~/.personalclaw/apps/research-lab/data/campaigns/<id>/campaign.json`, with its
`report.md` beside it. Plain JSON and plain markdown, written through the SDK's
`atomic_write`, readable and greppable, and still yours if you uninstall the app. Nothing
is posted anywhere: the app has no network permission with which to post it.

## Security posture

- **The campaign id is validated as a directory name before it is joined onto a path.**
  Tool arguments arrive from a model, so `../escape`, `/etc/passwd`, uppercase and spaces
  are all refused by regex — tested from every direction.
- **Bounded writes.** An unattended agent is the only writer, so every field it can grow
  has a ceiling: question 400 chars, finding 8 000, source 400, 20 sources, 200 nodes,
  5 follow-ups per finding. A runaway cycle meets a cap, not the disk.
- **A refusal is never a guess.** With no campaign id given, the app resolves the single
  open campaign and *refuses* when more than one is open, rather than picking.
- **An unreadable campaign file is skipped, not fatal** — the other campaigns keep
  working, and `personalclaw doctor` is where the skipped file becomes visible.
- **No shell, no subprocess, no network.** The whole app is stdlib plus
  `personalclaw.sdk.{tool,util,cli}`.

## Install

From the dashboard: **Store → Add source → local path**, point it at this directory, then
install and enable it. Or from a shell against a running gateway — the gateway takes the
owner token as a `?token=` query parameter (`personalclaw token` prints a URL carrying it),
not an `Authorization` header:

```bash
curl -X POST "$PERSONALCLAW_URL/api/apps?token=$PERSONALCLAW_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"source": "'"$PWD"'", "confirm": true}'
curl -X POST "$PERSONALCLAW_URL/api/apps/research-lab/enable?token=$PERSONALCLAW_TOKEN"
```

Enabling the app registers the provider and reconciles the cron; disabling it removes both.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `default_cycle_budget` | 5 | Cycles a new campaign may run unattended. |
| `cycle_breadth` (advanced) | 3 | Sub-questions one cycle hands out to subagents. |

A `research_open` call may override the budget; a `research_next` call may override the
breadth.

## Permissions

| Permission | Why |
|---|---|
| `storage` | campaigns and reports live under the app's own data dir |
| `cron` | the `advance-campaigns` job is what makes "unattended" real |
| `network` | declared **false** — the app opens no connection of its own |

## Tests

```bash
pytest research-lab
```

30 tests, no network, no gateway, and no pytest plugins required — `asyncio.run` rather
than `pytest.mark.asyncio`, so a bare `pytest` runs them. The one to read first is
`test_a_campaign_runs_multiple_unattended_cycles_and_synthesises_a_report`: it drives the
loop the way the cron's prompt does, then asserts the report on disk.

There is no `test_server.py`: this app declares no `backend`, so it has no server to test.
In this repo only `growth` and `minutes` — the backend+UI apps — ship one.

## Validated / not yet validated

Stated plainly, because the difference matters.

**Validated:**

- 30 tests green under the repo's `tests` job posture (core installed, no vendor SDKs,
  `PERSONALCLAW_SKIP_APP_BACKENDS=1`).
- `app.json` parses against core's own `AppManifest` and round-trips stably.
- SDK-only imports (`personalclaw.sdk.{tool,util,cli}`) — clean under the repo's
  `boundary` AST lint.
- The cross-app rails: `settings-schema-posture`, `prompt-cache-posture`,
  `live-writes-posture`, `quality-declarations`.
- A whole campaign end to end: open → several unattended cycles → a synthesised
  `report.md` on a real filesystem, plus the budget-exhaustion, depth-cap, duplicate-drop
  and path-refusal rails.

**Not yet validated — the remaining legs, for the owner or a live session:**

- **Local-Store install + a real UI drive.** In the core repo the local-source install path
  (`add_local_source` → `available_catalog` → `install` → `enable`) was exercised headless
  against this bundle before it moved here; nobody has clicked it in a running dashboard,
  so the install/quarantine/scan path and the Settings → Tools rendering of this manifest
  are unverified in the real UI.
- **The cron actually firing.** `reconcile_app_crons` was confirmed to accept this app
  (the `cron` permission is granted, the entry has a schedule and a message), but no clock
  tick has driven a real cycle. The multi-cycle walk is exercised by calling the tools in
  the order the cron's prompt calls them, not by the scheduler.
- **Real subagents on real research.** No host has spawned per-sub-question subagents from
  a worklist, and no live source has been fetched. Every finding in every test is a fixture
  string — the app never had a model or a network to get a real one.

None of these are faked or asserted as done anywhere in this bundle.

## License

MIT — see `LICENSE`.
