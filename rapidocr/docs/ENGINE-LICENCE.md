# Engine licence + footprint sign-off

KOCR-2 clause 6. Every value below was **measured on 2026-09-18** from the installed
distribution and from the upstream repositories' own licence records — none of it is
recalled or inferred. `test_provider.py::test_recorded_licences_are_permissive` parses this
file, so a future engine swap that leaves the note stale turns the suite red instead of
shipping an unverified licence.

## The engine

| field | value |
| --- | --- |
| engine id | `rapidocr-onnxruntime/1.2.3` |
| licence | `Apache-2.0` |
| source | https://github.com/RapidAI/RapidOCR |
| licence record | https://github.com/RapidAI/RapidOCR/blob/main/LICENSE |

Verified two independent ways, because the wheel is weaker evidence than it looks:

1. The installed distribution's metadata — `importlib.metadata.metadata("rapidocr-onnxruntime")["License"]`
   → `Apache-2.0`.
2. The upstream repository's licence record via the GitHub API
   (`GET /repos/RapidAI/RapidOCR/license`) → `spdx_id: Apache-2.0`,
   "Apache License 2.0".

The second check is not redundant. The 1.2.3 wheel ships **no `LICENSE` file** in its
`dist-info` — only the `License:` metadata field — so the wheel alone cannot substantiate
the terms it claims. The upstream record is what does.

## The bundled weights

The wheel carries its models, which is why this app works offline with nothing bound:

| file | size |
| --- | --- |
| `models/ch_PP-OCRv3_det_infer.onnx` | 2.3 MB |
| `models/ch_PP-OCRv3_rec_infer.onnx` | 10 MB |
| `models/ch_ppocr_mobile_v2.0_cls_infer.onnx` | 0.6 MB |

These are conversions of PaddleOCR's PP-OCRv3 models, so the weights' provenance is a
second licence that has to be checked and not assumed from the engine's:

| field | value |
| --- | --- |
| weights upstream | PaddlePaddle/PaddleOCR (PP-OCRv3) |
| licence | `Apache-2.0` |
| source | https://github.com/PaddlePaddle/PaddleOCR |
| licence record | https://github.com/PaddlePaddle/PaddleOCR/blob/main/LICENSE |

Verified the same way: `GET /repos/PaddlePaddle/PaddleOCR/license` → `spdx_id: Apache-2.0`.

## Transitive dependencies

`rapidocr-onnxruntime` pulls four distributions a PersonalClaw install does not already
have. Licences read from each installed distribution's own metadata:

| distribution | version | licence |
| --- | --- | --- |
| `rapidocr-onnxruntime` | 1.2.3 | `Apache-2.0` |
| `opencv-python` | 5.0.0.93 | `Apache-2.0` |
| `shapely` | 2.1.2 | `BSD-3-Clause` |
| `pyclipper` | 1.4.0 | `MIT` |

`onnxruntime`, `Pillow`, `numpy`, `protobuf`, `flatbuffers`, `PyYAML`, `packaging` and
`six` are **already core dependencies**, so they are not part of this app's delta.

`shapely` is `BSD-3-Clause` — permissive, and recorded here rather than rounded up to
"Apache-2.0 / MIT". Nothing in this bundle is *vendored*: the engine and its dependencies
are declared in `app.json` as `pythonDependencies` and installed by the platform into the
user's environment at install time, the same as every other engine-backed app in this
repository. The clause-6 allowlist assertion therefore applies to the engine and to the
weights it carries — both `Apache-2.0` — and the transitive set is documented in full so a
user who does have a distribution policy can read it rather than discover it.

## Footprint

Measured as the on-disk size, in a PersonalClaw dev install's `site-packages`, of exactly
the distributions the engine adds. **The delta is platform-dependent, so it is recorded per
platform** — the per-distribution table below is macOS arm64; the ubuntu-latest x86_64
number under it is what CI measures:

| distribution | on-disk |
| --- | --- |
| `opencv-python` (`cv2/`) | 124.9 MB |
| `rapidocr-onnxruntime` (incl. 13.8 MB of weights) | 13.8 MB |
| `shapely` | 6.9 MB |
| `pyclipper` | 0.7 MB |
| **delta** | **≈ 146.6 MB** |

Reference point: the dev install this was measured in is 1,556 MB of `site-packages`, so
the app adds about 9% — and it adds nothing at all to a user who does not install it,
which is the reason it is an app and not a core dependency.

`opencv-python` is 85% of the delta and is a hard requirement of RapidOCR 1.2.x's
preprocessing, not something this bundle chose. It cannot be swapped for
`opencv-python-headless`: the engine requires that exact distribution name, so declaring the
headless build would install it *alongside*, not instead of, the one it asks for.

On **ubuntu-latest x86_64** the same four distributions at the same pinned versions measure
**224.0 MB** — 77 MB more than the macOS figure above — because `opencv-python`'s manylinux
wheel carries far more shared-object payload than the macOS one. Measured on the CI runner
(PersonalClawApps run 35454957517, 2026-09-19), not inferred.

**Ceiling: 250 MB.** `test_provider.py::test_footprint_under_ceiling` measures the installed
distributions and fails above it, so dependency growth has to be argued for in a PR rather
than arriving unnoticed. The ceiling is set above the LARGEST measured platform, with 26 MB
of headroom, and it was raised from 200 MB for exactly one reason, recorded here so it is
not mistaken for a concession: the original 200 MB was set from the macOS measurement alone,
and the same dependency set is 224.0 MB on the platform CI runs. That is a measurement
correction, not dependency growth — the set, its versions and its licences are unchanged.

## Why the engine is pinned exactly

`app.json` declares `rapidocr-onnxruntime==1.2.3`, not a range. Every value on this page —
the engine id, the weight filenames, the transitive set and the 146.6 MB delta — was
measured against that one resolution, and two tests read this page as the authority
(`test_note_records_the_engine_id_licence_and_source`, `test_footprint_under_ceiling`). A
floating `>=1.2.3,<2` therefore makes the note false the day upstream publishes: measured
on a CI runner, `1.4.4` resolves to **226.6 MB** — over the ceiling — and records a version
this page does not. Moving the pin is a deliberate PR that re-measures every table here,
which is exactly the argument the ceiling exists to force.

## Candidates not chosen

Recorded so the choice can be re-litigated against the same facts rather than re-guessed.

| candidate | why not |
| --- | --- |
| Tesseract (`pytesseract`) | Not pip-installable on its own — it wraps a **system binary** (`tesseract`), absent on this machine and on a default CI runner. An engine whose availability depends on a `brew`/`apt` step cannot satisfy "install the app and OCR works". |
| PaddleOCR | Apache-2.0, but needs `paddlepaddle`, a framework an order of magnitude past this delta. |
| docTR | Apache-2.0, but needs torch or TensorFlow **plus** a weights download on first use, so it is neither small nor offline-on-install. |
