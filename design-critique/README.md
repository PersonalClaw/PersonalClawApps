# Design Critique

The review a colleague would give you, before you spend their attention.

Point it at a URL or a screenshot and it returns findings, not adjectives: what is wrong,
the evidence that says so, the WCAG clause where one applies, and the change that clears
it. Then it hands you the checklist for the half of a critique no measurement can do.

**Design Critique** is a **tool provider** — it implements the `personalclaw.sdk.tool`
`ToolProvider` contract and its three tools appear on the agent tool layer.

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**Design Critique** — the install runs through the security scanner and lifecycle exactly
like any other app. (Or `POST /api/apps {"source": ".../apps/design-critique"}`.) Nothing
to configure and no credential to supply — `personalclaw doctor` reports whether the image
decoder is available and restates the egress posture.

## The three tools

| Tool | What it does |
|---|---|
| `design_critique_page` | Fetch a URL, report accessibility and craft findings from its markup, and say what the headless renderer sees. |
| `design_critique_image` | Measure a screenshot's pixels: contrast, palette, color-vision safety, density, gutters, alignment. |
| `design_critique_rubric` | Return the checklist for the visual pass — the judgment no measurement can make. |

```
design_critique_page(url="https://example.com/pricing")
design_critique_page(url="https://example.com/signup", kinds="a11y", render=false)
design_critique_image(path="~/Desktop/checkout.png")
design_critique_rubric(surface="flow")
```

Both review tools accept `kinds` (`all` / `a11y` / `heuristic`) and `max_findings`. Every
finding carries `id`, `kind`, `severity` (`blocker` / `major` / `minor`), `title`, `detail`,
`fix`, `guideline` and `evidence` — in `metadata.findings` as well as in the rendered
report, so a host can render the structure instead of re-parsing prose.

## Why it is split in two

Judgment about a design divides cleanly into what a machine can **measure** and what it
must **look at**. Pretending otherwise is how design tooling ends up producing confident
nonsense, so the split is in the tool surface itself.

**Measured from markup** (23 rules): document language, page title, viewport zoom locking,
`main` landmark, duplicate ids, missing `alt`, filename-as-`alt`, untitled `iframe`,
autoplaying media, unlabeled form controls, placeholder-as-label, nameless links and
buttons, vague link text, positive `tabindex`, `target=_blank` without `rel=noopener`,
missing or duplicated `h1`, skipped heading levels, over-long headings, header-less data
tables, declared color pairs under 4.5:1, type below 12px, typeface count, type-scale
sprawl, missing meta description.

**Measured from pixels** (9 rules): capture width against real device and breakpoint
widths, rendered contrast against the dominant surface, pure black on pure white, palette
sprawl, full-saturation colors, red–green color-vision collapse, near-empty and
over-dense canvases, gutter imbalance, ragged left edges.

**Not measured, and not faked.** Whether the hierarchy matches the user's goal, whether the
copy earns its space, whether an affordance reads as clickable, whether the unhappy path
was designed at all. `design_critique_rubric` returns those as an explicit procedure —
three of them, for a `screenshot`, a `flow` and a live `page` — so the visual pass is the
same every run instead of improvised. A report with no findings says so plainly and tells
you it is **not** a pass.

## The color-vision check, since it is the least obvious one

Deuteranopia is modelled with the standard channel-mixing approximation, whose red/green
block is near-singular — that near-collapse *is* the condition. The check is then a
**ratio**, never an absolute simulated distance: a pair is flagged when it starts more than
120 apart in RGB and keeps less than 62% of that separation after the transform. Measured
on the canonical pairs, the classic unsafe red/green (`#d62728` vs `#2ca02c`) keeps 52% and
is flagged; the classic safe blue/orange (`#1f77b4` vs `#ff7f0e`) keeps 87% and is not.
Both directions are pinned by a test, because a check that fires on every two-color
palette would be worse than no check.

## What it cannot see, stated rather than implied

- **Markup checks read the static HTML.** Computed style, and anything the browser builds
  after load, are outside a source-level parse. When the static document is nearly empty
  and the rendered page is not, the report says the page is a client-rendered shell — which
  bounds every other markup finding in that run.
- **Contrast from markup is contrast that was DECLARED, in one rule.** A `color` in one
  rule and a `background` in another may never meet on screen; pairing them across rules
  would manufacture failures. Values that only resolve at render time (`currentColor`,
  `var(--x)`, `hsl()`, gradients) produce no finding at all. For what the user actually
  sees, review a screenshot — that path measures rendered pixels.
- **Pixel checks describe one capture.** A capture more than three times taller than it is
  wide is a whole-page scroll, not one viewport, so the composition checks that assume a
  single screen are skipped and the report says they were.
- **This is not an axe-core replacement.** It runs no browser, so it cannot check anything
  that needs a computed accessibility tree — focus visibility, live regions, actual
  reading order after CSS.

## Security posture

- **The URL** is the one attacker-influenced input that reaches the network. It goes
  through core's guarded egress chokepoint (`personalclaw.sdk.net.fetch` with
  `egress_policy_for(CONNECTOR)`), so a private, denied or non-http(s) target is refused by
  Settings → Security → Network rather than by this app's own judgment. A test asserts the
  bundle contains no `aiohttp` / `requests` / `httpx` / `urllib.request` import that could
  route around it, and a non-http scheme is refused before any fetch is attempted.
- **The screenshot path** reads exactly the one file named. It refuses a directory or
  device node, anything over 40 MB, and any image whose header declares more than 80M
  pixels — the pixel-count check happens *before* decoding, so a decompression bomb never
  gets decoded.
- **Nothing is written.** The bundle has no writer at all: no findings store, no cache, no
  temp file. The manifest declares `storage: false`, and a test greps the source to keep
  that declaration true.
- **Nothing is posted anywhere.** There is no reporter and no upload path. The review is
  the tool's return value.

## Settings

| Key | Label | Notes |
|---|---|---|
| `timeout_secs` | Fetch Timeout | Seconds `design_critique_page` waits for the page, 1–120, default 20. Advanced. |

## Permissions

`network: true` (the page fetch), `storage: false`. That is the whole declaration.

## Tests

`test_provider.py` covers the provider contract, the manifest against core's own
`AppManifest`, the color maths against the WCAG anchors, all 23 markup rules on a
deliberately broken page **and their silence on a correct one**, the pixel rules against
images the test draws, every refusal path, and the whole `design_critique_page` pipeline
over the two fetch seams. No network, no credentials, no gateway.

```
python -m pytest design-critique -q
```

## Design notes

### Why `tool` and not `agent` or `workflow`

`agent` in this platform means an **ACP agent bundle** (`claude-code-agent`,
`codex-agent`) — a coding CLI you select in the Agents list, not a task an agent performs.
`workflow` is a real `PROVIDER_TYPES` entry but publishes no SDK contract, so an app cannot
build against it without breaking the SDK-only boundary. What this app actually is — a
capability the agent *calls*, with arguments, that returns a report — is exactly the `tool`
contract, per the capability table in
[`docs/app-creation-guide.md`](../docs/app-creation-guide.md).

### Why there is no `test_server.py`

There is no `test_server.py`: this app declares no `backend`, so it has no server to test.
In this repo only `growth` and `minutes` — the backend+UI apps — ship one.

### Validated / not yet validated

Stated plainly, because the difference matters.

**Validated:**

- The full suite green under the repo's `tests` job posture (core installed, no vendor
  SDKs, `PERSONALCLAW_SKIP_APP_BACKENDS=1`).
- `app.json` parses against core's own `AppManifest` with no errors.
- SDK-only imports (`personalclaw.sdk.{tool,net,cli}`) — clean under the repo's `boundary`
  AST lint.
- The cross-app rails: `manifest-validate`, `quality-declarations`,
  `settings-schema-posture`, `prompt-cache-posture`, `live-writes-posture`, `dco`.
- Both analysis engines end to end: a URL (over the fetch seams, against markup the test
  controls) and a screenshot (against images the test draws) each yield structured
  accessibility **and** craft findings.

**Not yet validated — the remaining legs, for the owner or a live session:**

- **Local-Store install + a real UI drive.** Adding this directory as a local Store source,
  installing the app in a running gateway, and driving the tools from the chat surface has
  *not* been done. The install/quarantine/scan path and the Settings → Tools rendering of
  this manifest are therefore unverified in the real UI.
- **A real live URL.** No real `fetch` has run through this app. The egress seam is
  exercised only against markup supplied by the test, so real-world HTML shapes, redirects,
  auth walls, compression and size limits are unconfirmed.
- **The headless-render pass against a real browser.** `web_fetch(render=True)` is stubbed
  in the tests; Playwright has not run, so the client-rendered-shell detection is proven on
  its arithmetic rather than on a real SPA.
- **The pixel rules against real product screenshots.** They are proven against synthetic
  canvases with known colors and geometry. The thresholds (palette size, density bands,
  the 6-left-edge alignment ceiling) have not been calibrated against a corpus of real UI,
  so expect to tune them.

None of these are faked or asserted as done anywhere in this bundle.

## License

MIT — see `LICENSE`.
