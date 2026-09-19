"""RapidOCR — a deterministic, fully offline OCR engine as a removable provider app.

The engine is RapidOCR's ONNXRuntime build. Three properties decided it over the other
candidates, and each is the reason a clause of KOCR-2 is satisfiable rather than argued:

* **No model binding, and no download.** The PP-OCRv3 detection/recognition/classification
  weights ship INSIDE the wheel (13.8 MB of ``.onnx``), so the first OCR on a fresh install
  works with no network, no HuggingFace token, and no model bound in Settings → Models.
  An engine that fetched weights on first use would make "works offline" conditional on a
  past online moment, which is not the same claim.
* **Deterministic.** Detection and recognition are argmax over a fixed graph — there is no
  sampler and no temperature — so the same bytes give the same text. ``test_provider.py``
  holds that to account by OCR'ing one image twice and comparing.
* **Permissive throughout.** Apache-2.0 for the engine AND for the upstream the weights
  derive from. See ``docs/ENGINE-LICENCE.md`` for the ids, links and the footprint
  measurement; the test asserts the recorded licence is on the permissive allowlist, so the
  note cannot drift from what ships.

Core owns everything that is not the engine: the :class:`OcrProvider` contract, the
registry that makes this app's provider the active one while it is enabled, and the
true-type gate below. This file imports core ONLY through ``personalclaw.sdk.ocr``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Sequence
from typing import Any

from personalclaw.sdk.ocr import (
    OcrError,
    OcrProvider,
    OcrRejected,
    OcrResult,
    TrueTypeRejected,
    assert_image,
)

logger = logging.getLogger(__name__)

#: Upper bound on images handed to the engine in one call. Core's rasterizer already caps
#: pages, but this provider is a public seam any caller can reach, so it carries its own
#: ceiling rather than trusting the one upstream of it.
MAX_IMAGES = 64


class RapidOcrProvider(OcrProvider):
    """Offline OCR over RapidOCR's ONNXRuntime build.

    The engine object is built once and reused: loading 13.8 MB of ONNX per page would make
    a 40-page scan pay the load cost forty times. Construction is guarded by a lock because
    the ingestion executor may drive several items concurrently, and two threads racing to
    build the engine would load the graphs twice for no benefit.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config or {}
        self._engine: Any = None
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return "rapidocr"

    @property
    def engine_id(self) -> str:
        """``rapidocr-onnxruntime/<version>``, read from the INSTALLED distribution.

        Not a hardcoded string: the version recorded on an OCR'd item has to be the version
        that actually did the reading, or the record cannot be used to re-verify the text
        later. An unresolvable version reports ``unknown`` rather than a plausible guess.
        """
        try:
            from importlib.metadata import version

            return f"rapidocr-onnxruntime/{version('rapidocr-onnxruntime')}"
        except Exception:
            return "rapidocr-onnxruntime/unknown"

    @property
    def deterministic(self) -> bool:
        return True

    def available(self) -> bool:
        """Live probe: the engine imports AND its bundled weights are on disk.

        Both halves matter. ``import rapidocr_onnxruntime`` succeeding only proves the
        package is present; a wheel installed without its model payload (a trimmed image, a
        partial copy) imports fine and then fails at first use. So the ``.onnx`` files are
        checked for real — this must never degrade to a truthiness test on the module object,
        because answering True here turns a missing optional dependency into a failed ingest
        instead of the graceful skip the platform promises.
        """
        try:
            import rapidocr_onnxruntime
        except Exception:
            return False
        models = os.path.join(os.path.dirname(rapidocr_onnxruntime.__file__), "models")
        try:
            return any(f.endswith(".onnx") for f in os.listdir(models))
        except OSError:
            return False

    def _get_engine(self) -> Any:
        if self._engine is None:
            with self._lock:
                if self._engine is None:
                    from rapidocr_onnxruntime import RapidOCR

                    self._engine = RapidOCR()
        return self._engine

    async def recognize(self, image_paths: Sequence[str]) -> OcrResult:
        """OCR each image in order and return the concatenated text.

        Every input passes core's :func:`assert_image` gate FIRST — the bytes decide what a
        file is, never its extension (ARCC ``cnt_eMkU5kkpTaEk65``). A rejected input raises
        :class:`OcrRejected` before any decoder is handed the bytes, which is the whole
        point of doing it here rather than letting the engine discover it.
        """
        paths = list(image_paths)
        if not paths:
            return OcrResult(text="", engine=self.engine_id, pages=[])
        if len(paths) > MAX_IMAGES:
            raise OcrRejected(
                f"{len(paths)} images exceeds the {MAX_IMAGES}-image ceiling for one OCR call"
            )
        for path in paths:
            try:
                assert_image(path)
            except TrueTypeRejected as exc:
                # Re-raised as the seam's own rejection type so a caller handles one
                # exception family, not core's gate type plus the provider's.
                raise OcrRejected(str(exc)) from exc
        if not self.available():
            raise OcrError("rapidocr-onnxruntime is not installed (or its models are missing)")
        loop = asyncio.get_running_loop()
        pages = await loop.run_in_executor(None, self._recognize_sync, paths)
        return OcrResult(
            text="\n".join(p for p in pages if p),
            engine=self.engine_id,
            pages=pages,
            metadata={"deterministic": True, "offline": True},
        )

    def _recognize_sync(self, paths: list[str]) -> list[str]:
        """Blocking recognition, one page at a time, in page order.

        A page the engine reads as blank contributes an EMPTY string rather than being
        dropped, so ``pages`` stays index-aligned with the input — a caller mapping text
        back to page numbers would otherwise be silently off by every blank page.
        """
        engine = self._get_engine()
        pages: list[str] = []
        for path in paths:
            try:
                result, _elapsed = engine(path)
            except Exception as exc:  # noqa: BLE001 - one bad page must not lose the rest
                raise OcrError(f"OCR failed on {os.path.basename(path)}: {exc}") from exc
            # RapidOCR returns [[box, text, score], ...] in reading order, or None for a
            # page with nothing recognized.
            pages.append(" ".join(str(line[1]) for line in (result or [])))
        return pages


def create_provider(config: dict[str, Any] | None = None) -> RapidOcrProvider:
    """Factory core's ``ocr`` type handler calls when this app is enabled."""
    return RapidOcrProvider(config)
