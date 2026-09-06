# Docs & Slides

Turn a brief into a real PowerPoint deck (`.pptx`) or a compiled document (`.docx`/`.pdf`)
you can open and send. Write the brief as ordinary markdown; the app builds a declarative
document model from it and asks the writers **core already ships** to render the file. It
vendors no file-format library, contains no OOXML vocabulary, opens no network connection,
and writes only inside its own data directory.

**Docs & Slides** is a **tool provider** — it implements the `personalclaw.sdk.tool`
`ToolProvider` contract and its three tools appear on the agent tool layer.

## Requires a core with `personalclaw.sdk.documents`

This app is a front over core's document-generation seam, and that seam reaches the app
boundary through **`personalclaw.sdk.documents`** — a module that lands in core alongside
this bundle (PersonalClaw core PR, `feature-pep15-docs-slides`). On a core without it,
`provider.py` fails to import and the app will not register. **Core merges first.**

The alternative was vendoring `python-pptx`/`python-docx` in this bundle. That is the thing
not to do: Knowledge reads `.pptx`/`.docx` back through core's parsers, so a second
renderer in an app would drift from the one that reads it, and "the model writes the source,
code renders the file" stops being true the moment an app learns a binary format.

## Why `tool` and not `agent` or `workflow`

`agent` in this platform means an **ACP agent bundle** (`claude-code-agent`, `codex-agent`)
— a coding CLI you pick in the Agents list, not a task an agent performs. `workflow` is a
real `PROVIDER_TYPES` entry but publishes no SDK contract, so an app cannot build against it
without breaking the SDK-only boundary. What this app is — a capability the agent *calls*,
with arguments, that returns a file path — is exactly the `tool` contract, per the
capability table in [`docs/app-creation-guide.md`](../docs/app-creation-guide.md).

## The three tools

| Tool | What it does |
|---|---|
| `deck_from_brief` | Markdown brief → a real `.pptx`. Each `## ` heading starts a slide; the bullets under it become that slide's bullets. |
| `document_from_brief` | Markdown brief → a compiled `.docx` (or `.pdf`). Headings, paragraphs, bullet and numbered lists, tables, fenced code, `---` page breaks. |
| `docs_slides_formats` | What this build can actually render, right now — check before promising the user a format. |

```
deck_from_brief(brief="# Q3 review\n\n## Where we are\n- Nine apps shipped\n")
document_from_brief(brief="# Spec\n\n## Scope\n- One reviewer per app\n", format="docx")
docs_slides_formats()
```

## A deck and a document are different models

They are two tools, not one tool with a format switch, because the same markdown means
different things in each. In a deck a `## ` heading is a **slide boundary** and the bullets
under it are that slide's body; in a document the same heading is a section heading in one
continuous flow. A single `format="pptx"|"docx"` parameter would silently change what the
author's headings mean — so `document_from_brief` refuses `pptx` outright rather than
guessing.

## Formats are reported, never promised

`available_formats()` reports what is renderable **in this process**: a writer whose
optional library is missing never registers, so the list cannot claim a format that would
fail on use. Three consequences, each pinned by a test:

- the `format` enum on `document_from_brief` is *derived* from that list, so a model never
  sees a choice this build cannot honour;
- an unavailable format is refused **before** any render, leaving nothing on disk;
- `personalclaw doctor` reports `ok` / `warn` / `fail` by which halves render — and it is
  reporting the **host's** renderers, not this bundle's, which is why the probe exists at
  all.

## Output containment — the absence is the mechanism

There is no `path`, `dir` or `output_dir` argument on any tool. Files land in
`<app data dir>/out/`, which `permissions.storage` already grants, so the widest write this
app can perform is the one it was granted. A caller-supplied `filename` is reduced to a
bare stem — `Path(...).name` first (so `../../../../tmp/pwned` loses its directories before
anything else looks at it), then leading dots, then a character filter, then an 80-character
cap. A test asserts both halves: the written path's parent *is* the app's out dir, and the
traversal target does not exist.

## Install

From the App Store, add this `apps/` directory as a **local source**, then install
**Docs & Slides**. (Or `POST /api/apps {"source": ".../docs-slides"}`.) Nothing to
configure — `personalclaw doctor` reports which formats your build can render.

## Settings

| Key | Label | Notes |
|---|---|---|
| `max_brief_chars` | Brief size cap | Largest brief this app will render, default 200 000. Past the cap it refuses **with the number** rather than rendering a file nothing can open. Advanced. |

## Permissions

`storage: true` (the rendered files), `network: false`. That is the whole declaration.

## Tests

`test_provider.py` (39 tests) proves the two file-producing tools by **rendering and then
reading back**: the `.pptx` is parsed with core's own `pptx_parser` and the `.docx` with its
`docx_parser`, so "openable" is measured, not inferred from a byte count — a test that only
checked for a non-empty file would pass on 30 KB of unopenable zip. Also covered: the
manifest against core's `AppManifest`, the derived format enum, every refusal path (empty
brief, over-cap brief, deck format as a document, unknown format, no writer registered),
filename containment including traversal, and all three `doctor` verdicts.

```
python -m pytest docs-slides -q
```

There is no `test_server.py`: this app declares no `backend`, so it has no server to test.
In this repo only `growth` and `minutes` — the backend+UI apps — ship one.

## Validated / not yet validated

Stated plainly, because the difference matters.

**Validated:**

- 39 tests green under the repo's `tests` job posture (core installed, no vendor SDKs,
  `PERSONALCLAW_SKIP_APP_BACKENDS=1`).
- `app.json` parses and validates against core's own `AppManifest`.
- SDK-only imports (`personalclaw.sdk.{documents,tool,util,cli,manifest}`) — clean under
  the repo's `boundary` AST lint.
- The cross-app rails: `settings-schema-posture`, `prompt-cache-posture`,
  `live-writes-posture`, `quality-declarations`.
- A brief rendered to a `.pptx` whose slides, titles and bullets read back correctly
  through core's parser, and to a `.docx` whose title, headings and bullets do the same.
- **Local-Store install + registration + a real tool call, headless.** The exact Store path
  (`apps.source.resolve` → `apps.app_manager.install`, `origin="local"`) in an isolated
  `PERSONALCLAW_HOME`: installs, then `providers.loader.load_all_extensions()` registers
  `DocsSlidesProvider` on the tool layer and all three tools appear in
  `tool_providers.registry.list_all_tools()`. All three were then invoked through the
  registered provider — `docs_slides_formats` reported `decks: pptx / documents: docx, pdf`,
  and the deck and document it wrote into the app's own data dir read back through core's
  parsers with the right slide titles and headings.

**Not yet validated — the remaining legs, for the owner or a live session:**

- **The browser.** The headless run above proves install → register → invoke, but no one has
  driven `deck_from_brief` from the chat surface or looked at how this manifest renders in
  Settings → Tools.
- **Registry listing.** This app is not in `PersonalClaw/registry`'s `app-registry.json`;
  listing requires a published repo the validator can reach.
- **`.pdf` in a viewer.** A rendered PDF has a valid `%PDF-` header and `%%EOF` trailer, but
  none has been opened in a reader. Only the `.docx` half of `document_from_brief` is
  round-trip-proven through a parser.
- **Real-world briefs.** Every brief exercised here is hand-written. No model-authored brief
  has been rendered through this app.

None of these are faked or asserted as done anywhere in this bundle.

## License

MIT — see `LICENSE`.
