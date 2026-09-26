# Git Repository

Index the **content of a git repository you own** — source code **and** docs — into your
PersonalClaw knowledge library. Point it at a **local working clone** or a **github.com
URL**; every text file becomes a searchable knowledge item, and a re-poll after a new commit
ingests **only the files that changed** (a commit-SHA cursor), never the whole tree again.

## What this is (and is not)

A standalone PersonalClaw app that contributes a **knowledge source provider**
(`provider.type: "knowledge"`, `capabilities: ["source"]`). Once installed and enabled, the
core **source engine** polls it on a schedule, and the files it emits land in `knowledge.db`
with `provider="git-repo"` attribution — reachable through `knowledge_search` and the
`HybridRetriever` like any other knowledge item.

It is deliberately **not**:

- **`git-sync`** — that is a *sync transport* that moves PersonalClaw's own durability shards
  through a git remote. This app builds a knowledge *index* of a repository's content.
- **`dir-sync` / a watched directory** — the built-in directory source indexes docs only
  (`*.md/*.markdown/*.txt/*.rst/*.org`). This connector indexes **source code** too: its
  default include list spans the common source-code extensions as well as docs, so a `.py`
  or `.ts` file is as searchable as a README.
- **`notes`** — a git markdown notebook that keeps no index of its own.

## Egress discipline

- **Local clones open no network socket.** The connector reads a clone that already exists on
  disk through *local-only* git plumbing (`rev-parse` / `ls-tree` / `show` / `diff`). It never
  runs a network git verb (`clone` / `fetch` / `pull` / `remote` / `ls-remote`).
- **Remote repos route every byte through the core network chokepoint** — `sdk.net.fetch`
  under the engine's `SOURCE` egress policy (host classification, private-IP denial, redirect
  re-check). The app owns no HTTP client and no socket. A private-repo access token is read
  from the credential store and sent in an `Authorization` **header**, never in a URL.
- **Untrusted content is fenced by the standard ingest path.** File text is emitted as a
  `SourceItem`; the engine fences the title and the knowledge pipeline treats source-item
  content as untrusted data when it reaches a model. The connector adds no un-fenced side
  channel — it owns neither the fetch nor a model call.

It imports only the PersonalClaw **SDK** (`personalclaw.sdk.knowledge`, `personalclaw.sdk.net`,
`personalclaw.sdk.credentials`, `personalclaw.sdk.util`), never core internals, so core can
evolve without breaking it.

## Install & use

1. From the **App Store**, add the `apps/` directory as a **local source**, then install
   **Git Repository** (or [from a shell](../docs/third-party-install.md#installing-from-a-shell)) and
   **enable** it.
2. Open the app's **Settings** and set **Repository** to the repository you index most — an
   absolute path to a local clone on this machine (e.g. `/Users/you/code/myproject`) or a
   `https://github.com/<owner>/<repo>` URL. Optionally set a **Ref**, **Include/Exclude**
   globs, and — for a private GitHub repo — a **token credential** name (a token you saved
   under Settings → Credentials). These are the **defaults** every source inherits; you can
   leave them empty and configure each source instead.
3. Go to **Knowledge → Sources → Add source**, choose **Git Repository**, give it a name, and
   save. Leave the **Spec** as `{}` to index the repository from Settings, or put any of the
   keys below in it to override Settings for that one source. The engine begins polling it;
   the first poll ingests the tree, and each later poll ingests only what a new commit
   changed.

## Settings, and the per-source spec

The **same six keys** are the app's settings and a source's spec. Settings are the per-install
defaults; a source's spec overrides them for that source alone. An empty value in a spec means
*inherit*, and a key the table does not list is **refused** when you save the source — so a
typo is an error message rather than a source that quietly indexes the wrong repository.

| Key | Label | Notes |
|---|---|---|
| `repo` | Repository | Absolute local clone path **or** `https://github.com/<owner>/<repo>`. |
| `ref` | Ref | Branch/tag/commit to index. Default `HEAD`. |
| `include` | Include globs | Comma-separated filename globs. Empty = built-in default (source **and** docs). |
| `exclude` | Exclude globs | Comma-separated globs to skip (path or basename). VCS/build/dep dirs are always skipped. |
| `max_files` | Max files | Per-poll ceiling, 1–5000. Clamped, so a spec cannot exceed it. |
| `token_credential` | Access token credential | Credential name for a **private** GitHub repo's token (header auth). |

## Several repositories from one install

Add one source per repository and give each its own `repo` in the spec:

```json
{ "repo": "/Users/you/code/api" }
{ "repo": "https://github.com/you/web", "ref": "develop", "include": "*.ts, *.tsx, *.md" }
```

Each source keeps its **own commit cursor**, so a commit in one repository re-indexes only
that repository's changed files and leaves the others untouched.

This works because the engine now hands `poll` the source row's validated spec (AECO-2) — a
`KnowledgeSourceProvider` an app ships has no knowledge-store handle of its own, so before
that delivery existed the repository could only be a per-install setting and one install could
watch exactly one repository.

## Tests

```
python -m pytest git-repo -q
```

The tests run real `git` against local fixture repos and drive the remote path through a
canned GitHub backend — **no network, no credentials, no gateway**.

## License

MIT — see `LICENSE`.
