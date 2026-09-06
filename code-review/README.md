# Code Review

Deep-review a GitHub pull request one file at a time. It fetches the diff with your own
`gh`, scores every changed file by blast radius, then reviews each file **in isolation** —
heaviest first, each review seeing only its own diff — and appends what it finds to a JSONL
log on your machine. Nothing is ever posted back to the PR.

**Code Review** is a **tool provider** — it implements the `personalclaw.sdk.tool`
`ToolProvider` contract and its three tools appear on the agent tool layer.

## Why `tool` and not `agent` or `workflow`

`agent` in this platform means an **ACP agent bundle** (`claude-code-agent`,
`codex-agent`) — a coding CLI you select in the Agents list, not a task an agent performs.
`workflow` is a real `PROVIDER_TYPES` entry but publishes no SDK contract, so an app
cannot build against it without breaking the SDK-only boundary. What this app actually is
— a capability the agent *calls*, with arguments, that returns a report — is exactly the
`tool` contract, per the capability table in
[`docs/app-creation-guide.md`](../docs/app-creation-guide.md).

## The three tools

| Tool | What it does |
|---|---|
| `review_pr` | Fetch the PR diff, weight every file, fan out one isolated review per file, keep the findings locally. |
| `record_finding` | Append one finding for one file. The write end of the fan-out — how a per-file subagent reports back. |
| `review_findings` | Read a PR's findings back off this machine, optionally filtered by severity. |

```
review_pr(pr="PersonalClaw/PersonalClaw#123")
review_pr(pr="https://github.com/PersonalClaw/PersonalClawApps/pull/40", fanout="plan")
review_findings(pr="PersonalClaw/PersonalClaw#123", severity="high")
```

## The per-file fan-out — what it really is

This is the part to read honestly rather than skim.

Core **does** have a real subagent primitive (`SubagentManager`), and it is even
re-exported on the SDK — but only on `personalclaw.sdk.channel`, and an instance of it only
ever reaches an app through `GatewayServices.subagent_mgr`. A `ToolProvider` is constructed
with its settings dict and nothing else: no services object, no session, no manager. So
**this app cannot spawn a core subagent, and does not claim to.** There is likewise no
one-shot model helper on the SDK (`one_shot_completion` lives in `personalclaw.llm_helpers`,
off-surface), so the fan-out is built from what the SDK does publish. Three modes:

- **`fanout="model"` (default).** N model calls through `personalclaw.sdk.model`, bounded
  by `concurrency`. A **fresh provider instance per file**, built from the registry, given
  only that file's brief, torn down afterwards. Context isolation is total — a test asserts
  no brief can contain another file's path. Process isolation is *not*: these are model
  calls in this process, and the report labels them `model:<entry>`, never `subagent`.
- **`fanout="plan"`.** No model call. `review_pr` returns one review brief per file in
  `metadata.briefs` and the host agent spawns **one real subagent per brief** with its own
  spawn surface, each reporting through `record_finding`. This is the leg that gets
  genuinely isolated per-file subagents; it is why `record_finding` is a separate tool.
- **`fanout="static"`.** The deterministic diff pass only.

The deterministic pass runs in **all three** modes, so a review is never silently empty for
want of a model. Every brief is also emitted in `metadata.briefs` in every mode, so a host
that asked for `model` can still take the subagent leg.

Whatever the mode, the report names it. A review that ran with no model provider bound says
`static (no model provider is registered)` rather than "no findings".

## Blast-radius weighting

Every changed file gets a 0–100 weight, and the weight buys two things: **order** (heaviest
reviewed first) and **context budget** (`deep` 24k chars of diff, `normal` 10k, `skim` 2.5k).
Five cheap signals, no repo checkout and no language server:

1. **Churn**, log-scaled (+0…35). 40 lines and 4,000 lines are both "big"; a linear term
   would let one vendored blob eat the whole budget.
2. **Path criticality** (±). `auth` / `crypt` / `secret` / `credential` / `payment` /
   `migration` raise it; `docs/` / `*.md` / `example` / `fixture` / `*.lock` lower it.
3. **Fan-in inside the changed set** (+7 per importer, capped +20). How many *other changed
   files* import this one, matched on Python and JS import syntax. Scoped to the PR on
   purpose: the app never clones the repo, so this is a **floor** on real fan-in, not an
   estimate of it — and it is labelled that way in the weight's reasons.
4. **Status.** A deletion of something others import is the highest-reach change in a PR and
   the easiest to wave through (+12, +8 more with fan-in); a brand-new file reaches nothing
   yet (−6).
5. **Nothing to review.** Binary and generated/vendored/lockfile paths cap at 5.

Every weight ships the reasons that produced it, in the report table and in the brief the
reviewer sees. A number with no explanation is not a judgement anyone can argue with.

## Findings kept locally — literally

`FindingsLog` is the only writer in the bundle. It appends to

```
~/.personalclaw/apps/code-review/data/findings/<owner>__<repo>__<number>.jsonl
```

one JSON object per line: `file`, `severity`, `summary`, `evidence`, `weight`, `source`
(`static` / `model` / `subagent`), `ts`. There is no uploader, no reporter, no
`gh pr review`, no `gh pr comment` — the app's entire GitHub surface is one read:
`gh pr diff <n> --repo <owner>/<repo> --patch`. The manifest declares
`network: false`, so it opens no socket of its own either. Findings accumulate; a second
review of the same PR appends rather than overwrites, and `review_findings` reads them back.

## Security posture

Applying the ARCC input-validation guidance (SAX-04 boundary validation, SAX-06 log
injection) to the three untrusted edges:

- **The PR reference** becomes both a `gh` argument and a filename, so it is validated
  against one strict regex before either. `gh` is invoked with a fixed argv list — never a
  shell string — and the findings path is re-checked to be inside the app's own directory.
  A test asserts `acme/widget#42; rm -rf /` and `../../etc/passwd` never reach `gh`.
- **The diff** is attacker-authored text about to be read by a model, so every brief fences
  it with `personalclaw.sdk.security.fence_untrusted` — the model reads it as quoted data,
  not as instructions.
- **The evidence field** is attacker-authored text landing in a line-delimited log, so
  control characters, CR and LF are stripped before it is written. A newline in a diff line
  must not be able to forge a second finding.

## Install

From the App Store, add this `apps/` directory as a **local source**, then install
**Code Review**. (Or `POST /api/apps {"source": ".../code-review"}`.) You need the
[GitHub CLI](https://cli.github.com) on `PATH` and `gh auth login` done —
`personalclaw doctor` reports both.

## Settings

| Key | Label | Notes |
|---|---|---|
| `fanout` | Per-file fan-out | `model` (default) / `plan` / `static`. See above. |
| `concurrency` | Concurrent per-file reviews | 1–8, default 3. Advanced. |
| `max_files` | Files per review | Cap the fan-out to the N heaviest, default 40. Dropped files still appear in the weight table, named as skipped. Advanced. |
| `model_entry` | Model entry | Which configured model runs the per-file reviews. Empty = the first chat-capable provider from Settings → Models. Advanced. |
| `timeout_secs` | gh Timeout | Seconds to wait for `gh pr diff`. Advanced. |

## Permissions

`storage: true` (the findings log), `network: false`. That is the whole declaration —
GitHub is reached only through your already-authenticated `gh`.

## Tests

`test_provider.py` covers the pipeline over a committed fixture diff
(`fixtures/sample_pr.diff`): reference parsing and its refusals, diff parsing, the weight
model, the isolation contract on the briefs, the deterministic rules, the findings log
(including the injection cases), the model fan-out against a fake registry entry, and every
`review_pr` failure path. No live `gh` call, no network, no credentials, no gateway.

```
python -m pytest code-review -q
```

There is no `test_server.py`: this app declares no `backend`, so it has no server to test.
In this repo only `growth` and `minutes` — the backend+UI apps — ship one.

## Validated / not yet validated

Stated plainly, because the difference matters.

**Validated:**

- 88 tests green under the repo's `tests` job posture (core installed, no vendor SDKs,
  `PERSONALCLAW_SKIP_APP_BACKENDS=1`).
- `app.json` parses against core's own `AppManifest` and round-trips stably.
- SDK-only imports (`personalclaw.sdk.{tool,model,security,util,cli,manifest}`) — clean
  under the repo's `boundary` AST lint.
- The cross-app rails: `settings-schema-posture`, `prompt-cache-posture`,
  `live-writes-posture`.
- The whole review pipeline end to end over the fixture diff, in all three fan-out modes,
  with findings written to and read back from a real JSONL file.

**Not yet validated — the remaining legs, for the owner or a live session:**

- **Local-Store install + a real UI drive.** Adding this directory as a local Store source,
  installing the app in a running gateway, and driving `review_pr` from the chat surface has
  *not* been done. The install/quarantine/scan path and the Settings → Tools rendering of
  this manifest are therefore unverified in the real UI.
- **A real PR reviewed end to end.** No live `gh pr diff` has been run through this app.
  The `gh` edge is exercised only against a fixture, so `gh`'s real output shape, auth
  failures, and large-PR behaviour are unconfirmed.
- **The `plan` leg with real spawned subagents.** `fanout="plan"` emits the briefs and the
  contract for a host to spawn against; no host has actually spawned per-file subagents from
  them yet. The `model` leg is exercised only against a fake registry entry — no real model
  provider has run a per-file review.

None of these are faked or asserted as done anywhere in this bundle.

## License

MIT — see `LICENSE`.
