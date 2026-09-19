# RapidOCR (Offline)

Reads text out of pixels — a scanned PDF, a photographed page, a screenshot — **with no
model bound**. Without it, a user who has not bound a vision model gets an empty ingest
from a scanned document; with it, the page becomes searchable knowledge.

Everything runs on this machine. The ONNX weights ship inside the wheel, so the first OCR
after install needs no download, no API key, no HuggingFace token, and no network at all.

## Install

Install it from the Store. The platform installs the declared dependency
(`rapidocr-onnxruntime`) for you; there is nothing to configure — an OCR engine has no
settings worth guessing at, so it has none.

To install the dependency by hand (a source checkout, an offline mirror):

```bash
pip install 'rapidocr-onnxruntime>=1.2.3,<2'
```

## What you get

- **Works with nothing bound.** The ingestion pipeline prefers a vision model for OCR when
  you have one; when you do not, it falls back to this engine instead of skipping the step.
- **Deterministic.** The same image always produces the same text. There is no sampler, so
  re-ingesting a document does not quietly change what your library says it contains.
- **Offline.** No network call, ever. The engine and its weights are local files.
- **Removable.** Disable or uninstall it and ingestion returns to exactly its previous
  behaviour: no vision model, no OCR, no crash. Nothing in core names this engine.

## Where it plugs in

Core declares the seam and this app fills it:

| core | this app |
| --- | --- |
| `personalclaw.sdk.ocr.OcrProvider` — the contract | `RapidOcrProvider` implements it |
| the `ocr` provider type + its manifest handler | `app.json` declares `"type": "ocr"` |
| `assert_image` — the true-type gate | called before the engine sees any bytes |
| the `ocr` / `engine` pipeline node | resolves whichever engine is registered |

## Security posture

Per ARCC `cnt_eMkU5kkpTaEk65` ("Secure File Uploads"), an image is admitted on what its
**bytes** say, never on its name:

- **True-type detection.** A file called `page.png` whose bytes are a PDF, an HTML document
  or plain text is refused before any decoder touches it. So is a real PNG named `.jpg` —
  the extension and the bytes have to agree, or either one alone becomes a way past the other.
- **Allowlist, not denylist.** PNG, JPEG, GIF, BMP, TIFF and WebP are accepted; anything
  else is refused rather than forwarded to a parser.
- **Ceilings.** 64 MiB per image and 64 images per call, so a pathological input is bounded
  rather than an out-of-memory.

The gate itself lives in core (`personalclaw.ocr.filetype`) and is called from here. One
magic-number table, two enforcement points — a table copied per bundle is a table that
drifts per bundle, and the one that drifts is the one that lets a non-image through.

## Engine, licence, footprint

`docs/ENGINE-LICENCE.md` records the engine id, both upstream licences (the engine's and
the bundled weights'), the source links, every transitive dependency's licence, and the
measured install footprint with the ceiling its test enforces. All of it is measured, not
recalled, and `test_provider.py` fails if the note drifts from what is installed.

Short version: `rapidocr-onnxruntime/1.2.3`, Apache-2.0, weights derived from PaddleOCR's
PP-OCRv3 (also Apache-2.0), **≈ 147 MB** added to a PersonalClaw install — and nothing at
all to a user who does not install it.

## Tests

```bash
pip install 'rapidocr-onnxruntime>=1.2.3,<2' pytest pytest-asyncio
python -m pytest rapidocr -q
```

They run against the **real** engine, deliberately. A stub would leave the only clause that
matters — that OCR genuinely reads pixels with no model bound — unmeasured, so the ground
truth is rendered into the fixture image and asserted back out of the result.
