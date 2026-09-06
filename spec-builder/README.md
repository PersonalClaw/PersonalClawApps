# Spec Builder

Write a spec — the problem, the done-when clauses, the steps, how done-ness is decided — and
compile it into a **workflow definition PersonalClaw's own workflow engine runs**. The spec is
the artifact you review; the definition is what executes.

**Spec Builder** is a **tool provider** — it implements the `personalclaw.sdk.tool`
`ToolProvider` contract and its eight tools appear on the agent tool layer.

## Why `tool` and not `agent` or `workflow`

`agent` in this platform means an **ACP agent bundle** (`claude-code-agent`, `codex-agent`) —
a coding CLI you select in the Agents list, not a task an agent performs.

`workflow` is the interesting one here, and the answer is not the obvious one. It *is* a real
`PROVIDER_TYPES` entry, and core's own `personalclaw.workflows.defs` docstring says a v2
def-provider is "the registry apps contribute template packs through". But that registry
publishes **no SDK contract**: `WorkflowDefProvider` lives at
`personalclaw.workflows.defs`, its def objects are `personalclaw.workflows.models.WorkflowDef`,
and neither is re-exported under `personalclaw.sdk.*`. A bundle implementing that type would
have to import around the SDK boundary, which this repo's `boundary` job refuses — correctly.
So this app is a `tool` provider that **emits** definitions in the engine's format and hands
them to core's own `workflow_author`. Promoting the def-provider seam into the SDK is core's
call, not this bundle's; when it happens, this app's compiler is already the thing that would
plug into it.

What this app actually is — a set of capabilities the agent *calls*, with arguments, that read
and write the user's specs — is exactly the `tool` contract, per the capability table in
[`docs/app-creation-guide.md`](../docs/app-creation-guide.md).

This bundle contributes no frontend, so there is no `ui/` and no design-system or a11y claim to
make. The authoring surface is the agent tool layer, in chat.

## The eight tools

| Tool | What it does |
|---|---|
| `spec_open` | Start a spec: a title, an optional intent line. Returns the id and what to fill in. |
| `spec_write` | Write one section — replace or append. Reports readiness as it goes. |
| `spec_read` | The spec as markdown, fenced, with its readiness verdict. `section` for one. |
| `spec_list` | Every spec: clauses, steps, filled sections, ready or not. |
| `spec_seed` | Fold one repository file at a named revision into `background`. Read-only. |
| `spec_review` | The structural readiness verdict. Zero model calls — it is a count. |
| `spec_compile` | The workflow definition. Runs nothing. |
| `spec_delete` | Remove a spec. Approval-gated. Any saved definition survives it. |

```
spec_open(title="Triage the inbox", intent="Cut inbox noise.")
spec_write(spec="triage-the-inbox", section="problem",  content="…")
spec_write(spec="triage-the-inbox", section="outcome",  content="- every message is labelled")
spec_write(spec="triage-the-inbox", section="steps",    content="- Label: apply the labels …")
spec_write(spec="triage-the-inbox", section="verification", content="`make triage-check` passes")
spec_review(spec="triage-the-inbox")     → ready, 1 clause, 1 step
spec_compile(spec="triage-the-inbox")    → the definition JSON
workflow_author(...)                     → core saves it
workflow_start(...)                      → core runs it
```

## A spec front, not a second planner

This is the constraint that shaped everything else. PersonalClaw already has a workflow engine
with a node algebra, a run journal, gates, retries, checkpoints and a scheduler. This app
duplicates **none** of it:

- **No run state of any kind.** There is no scheduler here, no journal, no retry policy, no
  node executor, and no notion of a run. `spec_compile` returns a definition and stops.
- **The definition is in the engine's own format** — `sequence` / `stage` / `gate`, node ids,
  `{{inputs.…}}` and `{{nodes.….output.…}}` bindings. A test parses it with core's own
  `WorkflowDef` and runs core's own `validate_node_tree` over it, so "the engine accepts this"
  is an outcome rather than a resemblance. (That test skips when core's engine is not
  importable, so the bundle stays installable standalone.)
- **Nothing here starts anything.** `spec_compile`'s output text names `workflow_author` and
  `workflow_start` because those are the doors, and this app is on the other side of them.

The value it adds is upstream of the engine: a spec worth compiling. `workflow_plan` already
turns a sentence into a draft graph. What it cannot do is hold a spec still while a human
argues with it — which is where a plan actually goes wrong.

## The section vocabulary, and why it is closed

| Section | Required | What it becomes |
|---|---|---|
| `problem` | yes | The shared context block in every emitted stage prompt. |
| `outcome` | yes | One `- ` bullet per done-when clause → the done-when review + its gate. |
| `steps` | yes | One `- <label>: <instruction>` bullet per step → one `stage` each, in order. |
| `verification` | yes | Quoted into the `verify_command` input's help text. |
| `non_goals` | no | An "explicitly out of scope" block in every stage prompt. |
| `constraints` | no | A "holds for every step" block in every stage prompt. |
| `background` | no | Grounding material for *authoring*. Never inlined into a prompt. |

The vocabulary is closed on purpose. An open one turns into a second free-form document store,
and this repo already has one of those (`notes`). A spec is a shape.

The two bullet grammars are load-bearing rather than decorative: they are how the compiler
knows what a clause and a step *are*. A `steps` bullet with no colon keeps its whole body as
the label and gets an empty instruction, which `spec_review` then names — rather than the
parser guessing which half was meant.

## Readiness is structural, and it is one gate

`spec_review` answers whether the spec has the **shape** to compile: which required sections
are empty, how many clauses and steps parsed, which steps have no label or no instruction. It
never judges the prose — that is the reviewer's job, and a model asked to grade a spec it is
about to execute is grading its own homework.

`spec_compile` consults that same verdict rather than re-deriving the conditions, so the two can
never disagree. `force=true` compiles an unready spec anyway — useful for seeing the shape — and
the returned payload carries the verdict, so nothing can lose track of what was overridden.

## The verification command is an input, never a literal

The emitted definition declares two inputs: `cwd` (default `.`) and `verify_command`
(required). The `verify_command` gate runs `{{inputs.verify_command}}`.

That indirection is the point. **This app must never be the thing that decides what command
runs on your machine.** The `verification` section is prose written by whoever wrote the spec; a
sentence like "run `curl … | sh` and see that it works" is *quoted into the input's help text*
and nowhere else. A test asserts that the gate's `command` is the binding and only the binding.

The gate order is deliberate too: the command runs **before** the model answers the done-when
clauses. A model-answered gate placed first is a gate that can be talked past; placed after the
command, the command settles the facts and the review only decides whether the clauses the user
wrote were actually met. A clause the review cannot check is UNMET, not met.

## Grounding a spec in the code

`spec_seed(spec=…, path="src/app/router.py", revision="HEAD~2")` reads that one file out of the
configured **Source repository** and appends it to `background`, with a heading naming the path
and the resolved sha.

It reads through `git show <rev>:./<path>`, not off disk. A spec grounded in "whatever was in
the working tree" cannot be re-derived later, and a dirty tree would make the seeded background
disagree with the sha the spec cites. A test writes uncommitted garbage over the file and
asserts the garbage does not come back.

There is **no default repository**. An app that guesses which checkout to read is an app that
reads the wrong one, so `spec_seed` refuses with that sentence until you set it. Every other
tool works without it.

## Security posture

A spec is exactly where a pasted issue, a quoted design doc or somebody else's README ends up,
and `spec_seed` fetches file content outright. Applying the ARCC input-validation guidance
(SAX-04 boundary validation, SAX-06 log injection) to each untrusted edge:

- **The spec id** becomes a directory name AND the compiled definition's name, so it is held to
  core's own definition-name grammar: lowercase letters, digits and hyphens, starting and ending
  alphanumeric. That makes `..`, `.git` and dotfiles *unrepresentable* rather than filtered.
- **The source path** becomes both a filesystem path and a `git show` argv element, so it is
  validated first: one strict regex per segment, every segment starting with an alphanumeric —
  which makes `..`, `.git`, dotfiles and `-oProxyCommand=…` unrepresentable — plus depth and
  length caps. The resolved target is then re-checked to be inside the repository, which is what
  catches a symlink inside it pointing out (a name-only check cannot). A test drives every
  refusal case with `subprocess.run` monkeypatched into a tripwire, so a path that should be
  refused provably never reaches `git`.
- **The revision** has its own narrower grammar — a commit sha, `HEAD`, or `HEAD~<n>`. A branch
  name, a range, `@{upstream}` and `--upload-pack=…` are all refused rather than passed through,
  because this app only ever reads one committed file.
- **`git` itself** is invoked with a fixed argv list — never a shell string — with a hard timeout
  and `GIT_TERMINAL_PROMPT=0`, so seeding can never block on a credential prompt from a repo
  that happens to have a remote. Every invocation is read-only (`show`, `rev-parse`).
- **Spec content handed back to a model is fenced** with
  `personalclaw.sdk.security.fence_untrusted` — `spec_read`, `spec_seed`'s echo, and the
  `spec_list` table (its titles are the user's own text, and a seeded spec's title may have been
  lifted out of fetched material). A test asserts a spec containing the closing marker cannot
  break out of its own fence.
- **`background` is never inlined into an emitted prompt.** This is the one that is not obvious.
  A workflow definition is **saved and re-run**; folding fetched file content into a stored
  prompt would make an injection *durable* — it would survive every later run of that workflow,
  long after the human who reviewed the spec stopped looking. Background stays in the spec store
  and the emitted prompts tell the run to go read the working tree instead. Two tests assert a
  planted `IGNORE PREVIOUS INSTRUCTIONS` in `background` — one written directly, one seeded out
  of a real git repo — never appears in the compiled definition.
- **Titles and intents are collapsed to one printable line.** They are echoed into logs, the
  listing table and the definition's description; a newline in one would forge a row in every
  surface it is read through.
- **Nothing content-shaped is logged.** The log records a spec id, a section name, a char count,
  a readiness verdict, a seeded path and its resolved sha — never a section body. Two tests
  plant a secret (one in a written section, one in a seeded file) and assert it appears in no log
  record.
- **Caps, not trust.** A section is capped at 40,000 characters, a seed at 200,000 bytes
  (truncation is reported out loud, not silent), a spec at 40 steps and 40 clauses, the store at
  500 specs.

`spec_delete` removes only the record and, if nothing else is in it, the directory — never a
recursive tree walk, so a stray file or symlink under a spec directory cannot widen a delete.
Files this app did not create are reported and left in place.

## Install

From the App Store, add this `apps/` directory as a **local source**, then install
**Spec Builder**. (Or `POST /api/apps {"source": ".../spec-builder"}`.) `git` on `PATH` is
needed only for `spec_seed` — `personalclaw doctor` reports it as a **warning**, not a failure,
because writing and compiling specs works without it.

## Settings

| Key | Label | Notes |
|---|---|---|
| `source_repo` | Source repository | The repo a spec is about. Empty leaves `spec_seed` unavailable; there is no default on purpose. |
| `timeout_secs` | git Timeout | Seconds to wait for a `git` command (minimum 5, default 20). Advanced. |

## Permissions

`storage: true` (the spec store), `network: false`. That is the whole declaration — there is no
remote and no wire call anywhere in this bundle. `spec_seed` reaches a repository through the
local `git` binary, not over a socket.

## Tests

`test_provider.py` — 172 tests: the id, section, source-path and revision grammars and every
refusal, symlink containment on both the store and the repository, store CRUD with the
append/replace/no-op cases and the caps, tolerant load, the two bullet grammars, readiness in
every failing shape, the compiled node sequence and gate order, the command-stays-an-input
assertion, the background-exclusion assertions from both directions, core's own validator over
the emitted definition, real `git` reads at HEAD and a past revision including the dirty-tree
case, missing/hung `git`, the fixed-argv and `GIT_TERMINAL_PROMPT` assertions, the whole
provider surface with its risk levels and approval flags, the fencing and fence-break cases, the
no-content-in-logs cases, the CLI seams, and the manifest round-trip.

```
python -m pytest spec-builder -q
```

Falsified rather than assumed: neutering the per-segment path check and inlining `background`
into the shared context block turned **18 tests red**, including the tripwire and both
injection-durability assertions.

There is no `test_server.py`: this app declares no `backend`, so it has no server to test. In
this repo only `growth` and `minutes` — the backend+UI apps — ship one.

## Validated / not yet validated

Stated plainly, because the difference matters.

**Validated:**

- 172 tests green under the repo's `tests` job posture (core installed, no vendor SDKs,
  `PERSONALCLAW_SKIP_APP_BACKENDS=1`).
- `app.json` parses against core's own `AppManifest` and round-trips stably.
- SDK-only imports (`personalclaw.sdk.{tool,security,util,cli,manifest}`) — clean under the
  repo's `boundary` AST lint.
- The cross-app rails: `settings-schema-posture`, `prompt-cache-posture`,
  `live-writes-posture`, `quality-declarations`.
- **The emitted definition is one core's engine accepts** — parsed with core's `WorkflowDef` and
  passed through core's `validate_node_tree`, zero issues. This is the strongest single claim
  here, and it is an assertion against core's real validator, not a shape that looks right.
- Seeding really reads **real git** — the tests run `git` itself, not a stub, over a temp
  repository: HEAD, `HEAD~1`, the dirty-working-tree case, a missing path, truncation, and a
  symlink refusal.

**Not yet validated — the remaining legs, for the owner or a live session:**

- **Local-Store install + a real UI drive.** Adding this directory as a local Store source,
  installing the app in a running gateway, and driving `spec_open`/`spec_compile` from the chat
  surface has *not* been done. The install/quarantine/scan path, the Store listing, and the
  Settings → Tools rendering of this manifest are therefore unverified in the real UI.
- **A definition compiled here has never actually been RUN.** `workflow_author` has not been
  handed one of these payloads in a live gateway, and no `workflow_start` has executed the
  emitted tree. The validator says the shape is admissible; it does not say a run through it
  behaves well, and the stage prompts in particular have had no contact with a real model.
- **`personalclaw setup` / `doctor` in a real CLI run.** Both seams are unit-tested; neither has
  been rendered by the actual CLI.
- **The exemplar list.** This app is not recorded in ECOSYSTEM-TOOLING's exemplar list — that
  list lives outside this repo and is not this PR's to edit.

None of these are faked or asserted as done anywhere in this bundle.

## License

MIT — see `LICENSE`.
