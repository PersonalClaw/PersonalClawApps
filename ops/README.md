# Ops

An on-call first responder. Point it at a folder your monitor writes alarms into and it
files each firing as an incident, ranks the queue by what actually needs a human first, and
walks the shift with you: claim, get the investigation plan out of your own runbook, record
what you found, and write down a proposed fix with its blast radius and its rollback.

**Proposing changes nothing.** Running a fix needs a setting you turned on, an explicit
confirm, and the token that digests the exact plan you read — and it can only ever run a
command your own runbook already declared.

**Ops** is a **tool provider** — it implements the `personalclaw.sdk.tool` `ToolProvider`
contract and its nine tools appear on the agent tool layer.

## Why `tool` and not `agent` or `workflow`

`agent` in this platform means an **ACP agent bundle** (`claude-code-agent`, `codex-agent`)
— a coding CLI you select in the Agents list, not a task an agent performs. `workflow` is a
real `PROVIDER_TYPES` entry but publishes no SDK contract, so an app cannot build against it
without breaking the SDK-only boundary. What this app actually is — a set of capabilities
the agent *calls*, with arguments, over one incident ledger — is exactly the `tool`
contract, per the capability table in
[`docs/app-creation-guide.md`](../docs/app-creation-guide.md).

The shift loop itself is a **declared cron**, not a loop in this bundle: `ops-sweep` runs
every ten minutes and hands the agent a fixed instruction (watch, triage, claim,
investigate, record, propose — and never apply). That is the same shape `research-lab` uses
for its unattended cycles, and it is why this app needs no background thread of its own.
Edit or disable it in Triggers like any other schedule.

## The nine tools

| Tool | What it does |
|---|---|
| `ops_watch` | Sweep the alarm spool, file what is new, return the ranked queue. |
| `ops_queue` | Open incidents worst-first, each with its score broken down by term. |
| `ops_incident` | One incident in full: alarm, runbook, timeline, proposals, tokens. |
| `ops_claim` | Take (or release) ownership. Investigating an unclaimed incident is refused. |
| `ops_investigate` | The plan: your runbook's checks plus the generic first-responder checks. |
| `ops_record` | Append one finding — including the ones that came back clean. |
| `ops_propose_fix` | Write down a fix + blast radius + rollback. Returns a confirm token. |
| `ops_apply_fix` | **The one gate.** Runs a declared runbook action. Four refusals in front. |
| `ops_resolve` | Close as `resolved` or `dismissed`. Reopens by itself if it fires again. |

```
ops_watch()                                    → inc-0a1b2c3d4e5f, score 76
ops_claim(incident="inc-0a1b2c3d4e5f")
ops_investigate(incident=…)                    → the runbook's checks
ops_record(incident=…, finding="consumer exited 137", verdict="root cause")
ops_propose_fix(incident=…, summary=…, blast_radius=…, rollback=…,
                action="restart-worker")       → prop-bbd14f13 + confirm_token
ops_apply_fix(incident=…, proposal="prop-bbd14f13",
              confirm_token="bbd14f13…", confirm=true)
```

Eight of the nine cannot change anything outside the ledger. A test drives all eight with
`subprocess.run` monkeypatched into a tripwire, so that is checked rather than claimed.

## Where alarms come from

There is no vendor integration and no polling client here. Alarms arrive as **JSON files in
a spool folder**, which is the one intake every monitor can already reach: an Alertmanager
webhook receiver, an SNS-to-file bridge, a `curl` in a cron, `webhook-action`, or three
lines of Python. One file per firing.

The reader is deliberately shape-tolerant, because monitors disagree about where the same
fact lives:

```json
{"alertname": "QueueDepthHigh", "severity": "critical",
 "instance": "worker-3", "summary": "queue depth 9000", "runbook": "worker-backlog"}
```

```json
{"alerts": [{"labels": {"alertname": "HighLatency", "severity": "warning",
                        "instance": "api-1"},
             "annotations": {"summary": "p99 3.2s"}, "startsAt": "…"}]}
```

A bare object, a bare list, and Alertmanager's `{"alerts": […]}` are all read; `labels`,
`annotations`, `detail` and `alarm` are searched one level down. `severity` is mapped
through an alias table (`crit`, `sev1`, `error`, `warning`, `p3`, `minor` …); a word this
app does not know becomes `unknown` and is weighted **mid-scale**, not dropped — a monitor
emitting an unfamiliar severity is not evidence the alarm is unimportant. A payload with no
recognisable alarm name is refused rather than filed under a placeholder, and counted out
loud in the sweep report.

Spool files are **only ever read**. This app never deletes or moves them; the monitor owns
them. Nothing is filed twice, two ways at once:

- A spool file already read **with the same bytes** is skipped. A file the monitor rewrote
  in place is read again.
- An alarm's identity is a digest of `source + name + resource + severity` — deliberately
  **not** its message or its timestamp. A second firing therefore bumps one incident's
  count instead of opening a second incident, which is what makes the repeat term of the
  priority score mean anything.

A resolved incident whose alarm fires again **reopens, unclaimed**, keeping its whole
timeline.

## How the queue ranks

Six independent terms, each returned alongside the score so a weight that stopped firing
shows up as a zero instead of quietly leaving the ranking alone:

| Term | Fires when | Worth |
|---|---|---|
| `severity` | critical / high / medium / low / info / unknown | 40 / 25 / 12 / 5 / 0 / 8 |
| `page` | the alarm paged (critical pages by default; `"page": false` overrides) | 20 |
| `unclaimed` | nobody owns it | 10 |
| `age` | 0.2 per minute since first seen | up to 20 |
| `repeats` | 2 per firing after the first | up to 10 |
| `no_runbook` | **no** runbook matched it — so a human has to think | 6 |

A critical, paging, unclaimed, three-hour-old, six-times-fired alarm with no runbook scores
106. A quiet `info` alarm somebody already owns, with a runbook, scores 0. Both numbers are
pinned by a test, and every term is pinned **in both directions** — it must fire on an input
that should trip it and stay silent on one that should not. A weight that cannot trip is not
a rule, and this suite fails if one becomes unreachable.

## Runbooks

A runbook is a JSON file **you** write, in a folder you choose, addressed by its filename
stem:

```json
{
  "title": "Worker queue backlog",
  "match": {"alarm": ["QueueDepth*"], "resource": ["worker-*"], "severity": ["critical"]},
  "checks": ["Is the consumer process running?", "Is the database accepting writes?"],
  "actions": [
    {"name": "restart-worker",
     "argv": ["systemctl", "restart", "personalclaw-worker"],
     "description": "Restart the stuck consumer",
     "blast_radius": "one host; in-flight jobs are retried",
     "rollback": "systemctl start personalclaw-worker"}
  ]
}
```

Matching is by **specificity**: every criterion a runbook states must hold, and the runbook
holding the most of them wins (ties by name, so the choice is deterministic). A runbook with
an empty `match` block is a catch-all worth zero points, so it is only ever chosen when
nothing more specific fits. `severity` patterns go through the same alias table the alarms
do, so a runbook written in its monitor's vocabulary still matches.

An alarm payload **may name a runbook**. That name is held to a one-segment grammar and must
resolve to a file you already wrote — the hint can only ever *select* among your runbooks,
never create one or reach outside the folder.

A runbook that will not load is **skipped and reported** (in the sweep output and in
`personalclaw doctor`), never silently dropped: the others still match and the incident is
still filed.

## The four gates in front of a fix

`ops_propose_fix` writes a plan to the ledger and nothing else. `ops_apply_fix` is the only
path to a side effect anywhere in this bundle, and all of these must hold:

1. **`allow_apply` is on.** Off by default. Off means this app can only ever propose, and
   `doctor` says which way it is set on every run.
2. **`confirm: true`.** No default; nothing runs on an implied yes.
3. **The confirm token matches.** The token is a digest of the proposal's decision-bearing
   content — summary, action, exact argv, blast radius, rollback — and *not* of its id or
   its timestamp. So a plan that was edited needs a fresh confirm, and a human who read one
   plan cannot have a different one applied under the approval they gave.
4. **The action is still declared, unchanged.** The proposal records the argv it was written
   against; if the runbook has been edited since, the apply is refused with both argvs
   shown rather than running the new one under the old confirm.

On top of those four, the tool itself carries `requires_approval=True` and
`RiskLevel.DESTRUCTIVE`, so the host's own approval prompt stands in front of all of them.
A proposal that names no action cannot be applied at all — it is a plan for a human, and the
tool says so.

The unattended cron message ends with **"NEVER call `ops_apply_fix`"**, in those words, and
a test asserts that sentence is still there.

## Security posture

An alarm is the canonical untrusted input for this app: machine-generated text quoting log
lines, hostnames and URLs from wherever the failure happened. Applying the ARCC
input-validation guidance (SAX-04 boundary validation, SAX-06 log injection) to each
untrusted edge:

- **Nothing from an alarm ever becomes a path or an argv element.** The incident id is
  *derived* — `inc-` plus 12 hex of the identity digest — and re-checked against
  `^inc-[0-9a-f]{12}$` on the way back in, which makes `..`, `.git`, dotfiles and
  `-oProxyCommand=…` unrepresentable rather than filtered. The one payload field that names
  something on disk (`runbook`) is held to a one-segment grammar and must match a file that
  already exists.
- **An action's argv is authored, never assembled.** There is no substitution, no format
  string and no interpolation in the runbook module: the list that reaches `subprocess.run`
  is the exact list you committed. A test drives an alarm whose every field is
  `$(curl evil)`;`` `id` ``;`rm -rf /`;`--upload-pack=x` and asserts the argv is still
  `["true"]`.
- **A shell string is refused as a shell string.** `"argv": "systemctl restart worker"` is
  an error naming the reason; only a list is accepted. `shell=False` is passed explicitly,
  stdin is `DEVNULL` (a remediation can never block on a prompt nobody is there to answer),
  and there is a hard timeout. Relative program paths (`./deploy`, `bin/deploy`) are refused
  because what they resolve to depends on a working directory nobody in this flow chose;
  a bare name on `PATH` or an absolute path with no dot-segment is accepted.
- **One module can spawn, and a test proves it is one.** `runbooks.py` is the only file in
  the bundle that imports `subprocess`; an AST test asserts that set is exactly
  `{runbooks.py}`, that `run_action` is referenced from exactly one function in the provider
  (`_apply_fix`), and that the single `shell=` keyword in the bundle is `False`. A separate
  test drives all eight non-apply tools end to end with `subprocess.run` replaced by a
  tripwire.
- **Everything handed back to a model is fenced** with
  `personalclaw.sdk.security.fence_untrusted` — the alarm text, the queue table (its names
  and resources came from payloads), the investigation plan (a runbook is read off disk),
  and a remediation's stdout/stderr. Nothing payload-shaped is quoted OUTSIDE a fence:
  the header of `ops_incident` carries only the ledger's own state, the validated
  runbook name and the derived score, and a test drives every tool with a marker
  planted in the alarm's name, resource and body and asserts it never appears before
  the fence opens. A further test asserts an alarm containing the closing marker cannot
  break out of its own fence, and another asserts the same for command output.
- **Refs and verdicts are logged; bodies are not.** The log records incident ids, action
  names, exit codes, sweep counts and gate refusals. A test plants a card number in an alarm
  summary and a token in a finding and asserts neither reaches the log.
- **Caps, not trust.** 400 spool files per sweep, 256 kB per file, 50 alerts per file, 8 000
  characters of alarm text, 200 timeline entries, 20 proposals, 24 argv elements, 4 000
  characters of command output. A record that will not parse is **counted out loud** in the
  queue and in `doctor` — a silently shorter queue during an incident is the worst failure
  this app could have.

What this app does **not** do: no network (`network: false`, and nothing in the bundle opens
a socket), no credentials, no writes to any tracker or monitor, and no arbitrary command —
only one you declared.

## Install

From the App Store, add this `apps/` directory as a **local source**, then install **Ops**.
(Or `POST /api/apps {"source": ".../ops"}`.) Nothing else is required to install: with no
spool folder and no runbooks it comes up idle and says so. To make it useful, point your
monitor at the spool folder and write your first runbook — `personalclaw doctor` reports
both, plus how the remediation gate is set.

## Settings

| Key | Label | Notes |
|---|---|---|
| `spool_dir` | Alarm spool folder | Where your monitor writes alarms. Empty = `spool/` in this app's data dir. Read-only to this app. |
| `runbooks_dir` | Runbooks folder | One JSON file per runbook. Empty = `runbooks/` in this app's data dir. |
| `allow_apply` | Allow gated remediation | **Off by default.** Off = propose-only. On enables `ops_apply_fix`, still behind the other three gates. |
| `on_call` | On-call name | Recorded as owner when a claim names nobody. Empty records `on-call`. |
| `timeout_secs` | Remediation timeout | Seconds a confirmed remediation may run, 5–900 (default 60). Advanced. |

## Permissions

`storage: true` (the incident ledger), `cron: true` (the ten-minute sweep), `network:
false`. That is the whole declaration — there is no remote and no wire call anywhere in this
bundle.

## Tests

`test_provider.py` — 174 tests: the three identifier grammars and every refusal, alarm
normalisation across the Alertmanager/bare-object/bare-list shapes and the severity alias
table, fingerprint identity, the file-digest and alarm-identity halves of the dedupe, reopen
after resolution, unreadable spool files and unparseable records being counted, every
priority term pinned in both directions plus the caps and the known-bad/known-good pair, the
severity floor, runbook parsing and every argv refusal, match specificity and the catch-all,
the enforced claim→investigate→propose chain, the double-claim refusal, all four gates
including argv drift and replay, a real remediation running the exact declared argv with a
hostile alarm attached, the timeout and not-on-PATH paths, the AST properties behind "no
ungated mutation", fencing (including that no payload text is quoted outside a fence)
and both fence-break cases, the log-hygiene assertions, the whole
tool surface with its risk levels and approval flags, the CLI seams, and the manifest
round-trip.

```
python -m pytest ops -q
```

There is no `test_server.py`: this app declares no `backend`, so it has no server to test.
In this repo only `growth` and `minutes` — the backend+UI apps — ship one.

## Validated / not yet validated

Stated plainly, because the difference matters.

**Validated:**

- 174 tests green under the repo's `tests` job posture (core installed, no vendor SDKs,
  `PERSONALCLAW_SKIP_APP_BACKENDS=1`).
- `app.json` parses against core's own `AppManifest` and round-trips stably.
- SDK-only imports (`personalclaw.sdk.{tool,security,util,cli,settings,manifest}`) — clean
  under the repo's `boundary` AST lint.
- The cross-app rails: `settings-schema-posture`, `prompt-cache-posture`,
  `live-writes-posture`, `quality-declarations`.
- **The measurable clause of this app's atom, end to end against a real process**: a
  simulated alarm is swept in from a spool file, claimed, investigated against a runbook,
  recorded, and a fix is PROPOSED — and applying it is refused four separate ways (setting
  off, no confirm, wrong token, runbook edited) before a confirmed apply runs `true`/`false`
  for real and records the exit code. Replaying the same confirm is refused.
- "No ungated mutation path" as a file-layout property, not a claim: one module imports
  `subprocess`, one function references `run_action`, the other eight tools run green with a
  tripwire in place of `subprocess.run`.

**Not yet validated — the remaining legs, for the owner or a live session:**

- **Local-Store install + a real UI drive.** Adding this directory as a local Store source,
  installing the app in a running gateway, and driving `ops_watch`/`ops_claim` from the chat
  surface has *not* been done. The install/quarantine/scan path, the Store listing, and the
  Settings → Tools rendering of this manifest are therefore unverified in the real UI.
- **The `ops-sweep` cron in a real scheduler.** The cron entry parses as part of the
  manifest and its message is asserted, but no gateway has ever fired it, so the unattended
  loop has never actually run end to end.
- **A real monitor.** Every alarm in the suite is a fixture this repo wrote. No
  Alertmanager, CloudWatch bridge or pager has written into the spool, so the shape
  tolerance is exercised only against payloads shaped by hand from those systems' documented
  formats.
- **A remediation against a real service.** The applied actions in the suite are `true`,
  `false` and a `python3 -c` print. No `systemctl`, `kubectl` or deploy tool has been run
  through this gate, so the executor is proven correct about argv, gating, timeouts and exit
  codes — not about operating anybody's stack.
- **`personalclaw setup` / `doctor` in a real CLI run.** Both seams are unit-tested; neither
  has been rendered by the actual CLI.
- **The exemplar list.** This app is not recorded in ECOSYSTEM-TOOLING's exemplar list —
  that list lives outside this repo and is not this PR's to edit.

None of these are faked or asserted as done anywhere in this bundle.

## License

MIT — see `LICENSE`.
