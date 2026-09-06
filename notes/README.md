# Notes

A git-backed markdown notebook. Your notes are plain `.md` files in a git repository on
your machine — so every version is recoverable, the whole notebook is portable, and it
stays readable in any editor with this app uninstalled and PersonalClaw shut down.

**Notes** is a **tool provider** — it implements the `personalclaw.sdk.tool` `ToolProvider`
contract and its seven tools appear on the agent tool layer.

## Why `tool` and not `agent` or `workflow`

`agent` in this platform means an **ACP agent bundle** (`claude-code-agent`, `codex-agent`)
— a coding CLI you select in the Agents list, not a task an agent performs. `workflow` is a
real `PROVIDER_TYPES` entry but publishes no SDK contract, so an app cannot build against it
without breaking the SDK-only boundary. What this app actually is — a set of capabilities
the agent *calls*, with arguments, that read and write the user's notes — is exactly the
`tool` contract, per the capability table in
[`docs/app-creation-guide.md`](../docs/app-creation-guide.md).

The word "editor" in *notebook editor* is about **what this app owns** (drafting, revision,
history) versus what Knowledge owns (indexed recall). It is not a claim about a UI: this
bundle contributes no frontend, so there is no `ui/` and no design-system or a11y claim to
make. The editing surface is the agent tool layer, in chat, which is where the rest of this
product's writing already happens.

## An editor, not a second knowledge store

This is the design constraint that shaped everything else. PersonalClaw already has a
knowledge library with an index, embeddings, dedup and consolidation behind
`knowledge_search` / `knowledge_create`. This app does **not** duplicate any of it:

- **No index, no embeddings, no database, no sidecar metadata.** The notebook is markdown
  files and a `.git` directory. A test asserts nothing else ever appears in it.
- **`note_search` is a literal, case-insensitive line match** — a grep over the files on
  disk. Its own description and its no-results message both say so and point at
  `knowledge_search` for meaning-based recall, so the agent is told the boundary at the
  moment it would otherwise guess wrong.
- **Nothing here writes to the knowledge library.** When something in a note should become
  durable knowledge, the agent hands it to core's `knowledge_create`. The notebook keeps
  the draft and its history; Knowledge keeps the retrievable fact.

The two are different objects on purpose: a Knowledge `note` is a library item, and a
notebook note is a *file you own* — diffable, greppable, syncable, and recoverable version
by version.

## The seven tools

| Tool | What it does |
|---|---|
| `note_write` | Create, replace or append to a note and commit that one note. |
| `note_read` | Read a note — current, or at a past revision. |
| `note_list` | Every note with title, size and last-modified, newest first. |
| `note_search` | Matching lines across the notebook (literal by default, `regex` opt-in). |
| `note_history` | Commits for one note, or for the whole notebook. Returns the shas. |
| `note_restore` | Bring a past version back **as a new commit** — never a rewrite. |
| `note_delete` | Remove a note. Approval-gated; a committed note stays in history. |

```
note_write(ref="ideas/tempo", content="# Tempo\n\n…")
note_write(ref="journal/2026-09", content="- shipped the notebook", mode="append")
note_history(ref="ideas/tempo")            → sha per commit
note_read(ref="ideas/tempo", revision=…)   → that version
note_restore(ref="ideas/tempo", revision=…)
```

`ref` is relative to the notebook, `/` between folders, and `.md` is optional —
`note_write(ref="ideas/tempo")` writes `ideas/tempo.md`.

## Where the notes live, and why they survive a reinstall

By default the notebook is `<app data dir>/notebook` — i.e.
`~/.personalclaw/apps/notes/data/notebook`. Two facts make that durable:

1. **Nothing about a note lives in the bundle.** The bundle is code. The notes are a
   directory somewhere else entirely, with their own git history.
2. **Core's uninstall is a deactivation that keeps an app's `data/`.** Reinstalling
   re-binds the same notebook, with every commit still in it.

Point `notebook_path` anywhere else and the independence only gets stronger — the notebook
then has nothing to do with PersonalClaw's home at all.

**Syncing is not this app's job.** There is no remote, no push, no `network` permission:
`git-sync` already exists for putting a directory on a remote, and rebuilding that here
would be a second, worse copy of it.

## Adopting an existing repo

If `notebook_path` points at a folder that is **already inside a git worktree** (a folder of
your dotfiles repo, say), the notebook **adopts that repository** instead of nesting a new
one inside it — nested repos are how people lose history they thought they had. In that
case every note commit is made with an explicit pathspec, so a note commit can never sweep
up unrelated changes you had staged. A test asserts exactly that.

Commits are made under a fixed identity (`PersonalClaw Notes
<notes@personalclaw.local>`) passed with `git -c`, so the notebook never reads from or
writes to your ambient git config.

## Security posture

Notes are user data, and a note is exactly where pasted web text, a quoted email or a
snippet from an issue ends up. Applying the ARCC input-validation guidance (SAX-04 boundary
validation, SAX-06 log injection) to each untrusted edge:

- **The note reference** becomes both a filename and a `git` argv element, so it is
  validated first: one strict regex per path segment, every segment starting with an
  alphanumeric — which makes `..`, `.git`, dotfiles and `-oProxyCommand=…` *unrepresentable*
  rather than filtered — plus depth and length caps. The resolved target is then re-checked
  to be inside the notebook, which is what catches a symlink pointing out of it (a name-only
  check cannot). A parametrized test drives every refusal case with `subprocess.run`
  monkeypatched into a tripwire, so a reference that should be refused provably never
  reaches `git`.
- **The revision** has its own narrower grammar — a commit sha, `HEAD`, or `HEAD~<n>`. A
  branch name, a range, `@{upstream}` and `--upload-pack=…` are all refused rather than
  passed through, because this app only ever reads a note's own past.
- **`git` itself** is invoked with a fixed argv list — never a shell string — with pathspecs
  after `--`, a hard timeout, and `GIT_TERMINAL_PROMPT=0` so a notebook operation can never
  block on a credential prompt.
- **Commit subjects** are stripped to one printable line. A message reaching `git` as an
  argv element cannot inject a command, but a newline in it would forge what looks like a
  second commit in every log the notebook is read through.
- **Note bodies handed back to a model are fenced** with
  `personalclaw.sdk.security.fence_untrusted` — `note_read`, `note_search` results, and the
  `note_list` table (its titles are lifted out of the notes). A test asserts a note
  containing the closing marker cannot break out of its own fence. Commit subjects are not
  fenced: they are either this app's own text or a message the agent itself passed in, and
  they are sanitized at write time.
- **Nothing content-shaped is logged.** The log records a note's ref, whether it was
  created/updated/unchanged, and the short sha — never a note's body.
- **Caps, not trust.** A single note is capped at 1 MB (a note is prose, not a payload),
  search reads at most 400 kB of any file, `note_list` stops at 2,000 notes, and a `.md`
  file whose name this app cannot address is **counted out loud** rather than silently
  omitted.

`regex=True` on `note_search` is opt-in and runs the caller's own pattern in-process with no
backtracking guard, over the user's own files. That is why it is not the default.

## Install

From the App Store, add this `apps/` directory as a **local source**, then install
**Notes**. (Or `POST /api/apps {"source": ".../notes"}`.) You need `git` on `PATH` —
`personalclaw doctor` reports it, and reports it as a **failure**, not a warning, because
without git there is no note history and the app's central promise is void.

## Settings

| Key | Label | Notes |
|---|---|---|
| `notebook_path` | Notebook folder | Empty = this app's data dir. Point it inside an existing repo and that repo is adopted. |
| `max_results` | Search results | Default cap on `note_search` lines, 1–200 (default 20). Advanced. |
| `timeout_secs` | git Timeout | Seconds to wait for a `git` command (minimum 5, default 20). Advanced. |

## Permissions

`storage: true` (the notebook), `network: false`. That is the whole declaration — there is
no remote and no wire call anywhere in this bundle.

## Tests

`test_provider.py` — 109 tests: the reference and revision grammars and every refusal,
symlink containment, write/append/no-op-rewrite, revision reads, history, restore-as-a-new-
commit, both delete paths, listing (including the unaddressable-file count), literal and
regex search with their caps, repo adoption with a pathspec-scoped commit, the
survives-a-rebind case, the "markdown and git and nothing else" assertion, missing/hung
`git`, the whole provider surface with its risk levels and approval flags, the fencing and
fence-break cases, the CLI seams, and the manifest round-trip.

```
python -m pytest notes -q
```

There is no `test_server.py`: this app declares no `backend`, so it has no server to test.
In this repo only `growth` and `minutes` — the backend+UI apps — ship one.

## Validated / not yet validated

Stated plainly, because the difference matters.

**Validated:**

- 109 tests green under the repo's `tests` job posture (core installed, no vendor SDKs,
  `PERSONALCLAW_SKIP_APP_BACKENDS=1`).
- `app.json` parses against core's own `AppManifest` and round-trips stably.
- SDK-only imports (`personalclaw.sdk.{tool,security,util,cli,manifest}`) — clean under the
  repo's `boundary` AST lint.
- The cross-app rails: `settings-schema-posture`, `prompt-cache-posture`,
  `live-writes-posture`.
- Notes really are versioned in **real git** — the tests run `git` itself, not a stub, over
  a temp notebook: commits, per-note history, reading an old revision, restore, and a
  delete that leaves the content recoverable.
- Repo adoption, including that a note commit does not carry a user's unrelated staged file.

**Not yet validated — the remaining legs, for the owner or a live session:**

- **Local-Store install + a real UI drive.** Adding this directory as a local Store source,
  installing the app in a running gateway, and driving `note_write`/`note_read` from the
  chat surface has *not* been done. The install/quarantine/scan path, the Store listing, and
  the Settings → Tools rendering of this manifest are therefore unverified in the real UI.
- **A real uninstall/reinstall cycle.** The reinstall test rebinds a new `Notebook` over the
  same root, which is what a reinstalled app does — but no gateway has actually deactivated
  and re-activated this app. The claim rests on core's documented "uninstall keeps `data/`"
  behaviour, not on an observed cycle.
- **`personalclaw setup` / `doctor` in a real CLI run.** Both seams are unit-tested; neither
  has been rendered by the actual CLI.
- **The exemplar list.** This app is not recorded in ECOSYSTEM-TOOLING's exemplar list —
  that list lives outside this repo and is not this PR's to edit.

None of these are faked or asserted as done anywhere in this bundle.

## License

MIT — see `LICENSE`.
