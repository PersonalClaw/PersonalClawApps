"""The docs-slides tool provider.

A FRONT over the document-generation seam core already ships
(``personalclaw.sdk.documents``): a brief written as markdown becomes a declarative
model, and the writers core registers render that model into a real file. This bundle
vendors no file-format library and contains no OOXML vocabulary — a second renderer
would drift from the one Knowledge reads back, and "the model writes the source, code
renders the file" is why an app never has to learn a binary format.

Three refusals shape the surface:

* **Formats are reported, never promised.** ``available_formats()`` is what THIS build
  can render right now — a writer whose optional library is missing never registers —
  so ``document_from_brief`` refuses an unavailable format up front instead of failing
  mid-render, and ``docs_slides_formats`` exists so the model can check before it
  promises the user a format.
* **Output cannot leave the app's own data directory.** There is no caller-supplied
  directory argument: the manifest declares ``storage`` and ``network: false``, so the
  widest write this app can perform is the one it was granted. A ``filename`` is reduced
  to a bare stem, so no argument can walk out of that directory.
* **A deck and a document are different MODELS, not two renderings of one.** The same
  markdown shapes differently for each, so the two tools are separate rather than one
  tool with a format switch that silently changes what the headings mean.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from personalclaw.sdk.documents import (
    available_formats,
    deck_from_markdown,
    document_from_markdown,
    get_writer,
)
from personalclaw.sdk.tool import RiskLevel, ToolDefinition, ToolProvider, ToolResult
from personalclaw.sdk.util import app_data_dir

logger = logging.getLogger("docs_slides")

APP_NAME = "docs-slides"

#: Formats ``document_from_brief`` will consider, in preference order — the first one
#: this build can render is the default.
DOCUMENT_FORMATS = ("docx", "pdf")
#: The deck format. Separate from DOCUMENT_FORMATS because it takes a different model.
DECK_FORMAT = "pptx"

_UNSAFE_STEM_CHARS = re.compile(r"[^A-Za-z0-9._ -]+")


def safe_stem(raw: str, fallback: str) -> str:
    """Reduce *raw* to a bare, safe filename stem.

    ``Path(...).name`` runs FIRST so a caller-supplied ``../../.ssh/authorized_keys``
    loses its directories before anything else looks at it. Leading dots go NEXT, before
    the extension is dropped — otherwise ``.hidden`` reads as "empty stem, extension
    hidden" and collapses to the fallback, losing the name the caller actually asked for.
    """
    name = Path(str(raw or "")).name.lstrip(".")
    if "." in name:
        name = name.rsplit(".", 1)[0]
    name = _UNSAFE_STEM_CHARS.sub("-", name).strip(" -.")
    name = re.sub(r"\s+", "-", name)
    return name[:80] or fallback


def title_of(brief: str, given: str) -> str:
    """The explicit title, else the brief's first markdown heading, else a default."""
    if given.strip():
        return given.strip()
    for line in (brief or "").splitlines():
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
            if heading:
                return heading
    return "Untitled"


class DocsSlidesProvider(ToolProvider):
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = dict(config or {})
        self._max_brief_chars = int(self._config.get("max_brief_chars", 200_000))

    @property
    def name(self) -> str:
        return APP_NAME

    @property
    def display_name(self) -> str:
        return "Docs & Slides"

    @property
    def out_dir(self) -> Path:
        """The one directory this app writes to. Created on demand."""
        d = app_data_dir(APP_NAME) / "out"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def deck_formats(self) -> list[str]:
        return [DECK_FORMAT] if DECK_FORMAT in available_formats() else []

    def document_formats(self) -> list[str]:
        renderable = available_formats()
        return [f for f in DOCUMENT_FORMATS if f in renderable]

    async def list_tools(self) -> list[ToolDefinition]:
        doc_formats = self.document_formats()
        doc_note = (
            ", ".join("." + f for f in doc_formats)
            if doc_formats
            else "no document format is renderable in this build"
        )
        return [
            ToolDefinition(
                name="deck_from_brief",
                description=(
                    "Turn a brief into a real PowerPoint deck (.pptx) the user can open. "
                    "Write the brief as markdown: each '## ' heading starts a slide, the "
                    "bullets under it become that slide's bullets, and the '# ' heading is "
                    "the title slide. Returns the path of the file written."
                ),
                provider=APP_NAME,
                parameters={
                    "type": "object",
                    "properties": {
                        "brief": {
                            "type": "string",
                            "description": (
                                "The deck as markdown — '## ' per slide, '- ' bullets under it."
                            ),
                        },
                        "title": {
                            "type": "string",
                            "description": "Deck title. Defaults to the brief's first heading.",
                        },
                        "filename": {
                            "type": "string",
                            "description": "Optional file stem. The extension is always .pptx.",
                        },
                    },
                    "required": ["brief"],
                },
                requires_approval=False,
                risk_level=RiskLevel.CAUTION,
            ),
            ToolDefinition(
                name="document_from_brief",
                description=(
                    f"Turn a brief into a real compiled document the user can open ({doc_note}). "
                    "Write the brief as ordinary markdown — headings, paragraphs, bullet and "
                    "numbered lists, tables, fenced code, '---' for a page break. Returns the "
                    "path of the file written."
                ),
                provider=APP_NAME,
                parameters={
                    "type": "object",
                    "properties": {
                        "brief": {
                            "type": "string",
                            "description": "The document body as markdown.",
                        },
                        "title": {
                            "type": "string",
                            "description": "Document title. Defaults to the first heading.",
                        },
                        "format": {
                            "type": "string",
                            "enum": list(doc_formats),
                            "description": (
                                "Output format. Call docs_slides_formats to see what this "
                                "build can render."
                            ),
                        },
                        "filename": {
                            "type": "string",
                            "description": "Optional file stem. The extension follows the format.",
                        },
                    },
                    "required": ["brief"],
                },
                requires_approval=False,
                risk_level=RiskLevel.CAUTION,
            ),
            ToolDefinition(
                name="docs_slides_formats",
                description=(
                    "List the deck and document formats this build can actually render right "
                    "now. Check before promising the user a format."
                ),
                provider=APP_NAME,
                parameters={"type": "object", "properties": {}},
                requires_approval=False,
                risk_level=RiskLevel.SAFE,
            ),
        ]

    async def invoke(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        args = dict(arguments or {})
        if tool_name == "docs_slides_formats":
            return self._report_formats()
        if tool_name == "deck_from_brief":
            return self._render(args, kind="deck", fmt=DECK_FORMAT)
        if tool_name == "document_from_brief":
            requested = str(args.get("format") or "").strip().lower()
            available = self.document_formats()
            fmt = requested or (available[0] if available else DOCUMENT_FORMATS[0])
            return self._render(args, kind="document", fmt=fmt)
        return ToolResult(
            success=False,
            error=f"unknown tool: {tool_name}",
            recovery_hints=[
                "docs-slides exposes deck_from_brief, document_from_brief and "
                "docs_slides_formats.",
            ],
        )

    # -- internals ---------------------------------------------------------------

    def _report_formats(self) -> ToolResult:
        decks, docs = self.deck_formats(), self.document_formats()
        lines = [
            f"decks: {', '.join(decks) or 'none renderable in this build'}",
            f"documents: {', '.join(docs) or 'none renderable in this build'}",
        ]
        return ToolResult(
            success=True,
            output="\n".join(lines),
            metadata={"deck_formats": decks, "document_formats": docs},
        )

    def _render(self, args: dict[str, Any], *, kind: str, fmt: str) -> ToolResult:
        brief = str(args.get("brief") or "")
        if not brief.strip():
            return ToolResult(
                success=False,
                error="brief is empty",
                recovery_hints=[
                    "Pass the content as markdown in `brief` — '## ' per slide for a deck."
                ],
            )
        if len(brief) > self._max_brief_chars:
            return ToolResult(
                success=False,
                error=f"brief is {len(brief)} characters; the cap is {self._max_brief_chars}",
                recovery_hints=["Split the brief and render one file per part."],
            )
        allowed = (DECK_FORMAT,) if kind == "deck" else DOCUMENT_FORMATS
        if fmt not in allowed:
            return ToolResult(
                success=False,
                error=f"{fmt!r} is not a {kind} format",
                recovery_hints=[f"Use one of: {', '.join(allowed)}."],
            )
        writer = get_writer(fmt)
        if writer is None:
            return ToolResult(
                success=False,
                error=f"this build cannot render {fmt}",
                recovery_hints=[
                    "Call docs_slides_formats to see what is renderable, and offer one of those."
                ],
            )

        title = title_of(brief, str(args.get("title") or ""))
        try:
            model: Any = (
                deck_from_markdown(brief, title=title)
                if kind == "deck"
                else document_from_markdown(brief, title=title)
            )
            data = writer(model)
        except Exception as exc:  # noqa: BLE001 — a render fault is ours, not the caller's
            logger.warning("docs-slides %s render failed", fmt, exc_info=True)
            return ToolResult(
                success=False,
                error=f"rendering {fmt} failed: {exc}",
                recovery_hints=[
                    "Simplify the brief's markdown (plain headings, bullets, tables) and retry."
                ],
            )

        stem = safe_stem(str(args.get("filename") or ""), safe_stem(title, kind))
        path = self.out_dir / f"{stem}.{fmt}"
        path.write_bytes(data)
        units = (
            f"{len(model.slides)} slides" if kind == "deck" else f"{len(model.blocks)} blocks"
        )
        logger.info("docs-slides wrote %s (%s, %d bytes)", path.name, units, len(data))
        return ToolResult(
            success=True,
            output=f"Wrote {path} ({units}, {len(data)} bytes). Open it to review.",
            metadata={
                "path": str(path),
                "format": fmt,
                "title": title,
                "bytes": len(data),
            },
        )


def create_provider(config: dict[str, Any] | None = None) -> DocsSlidesProvider:
    """Manifest factory — core calls this with this app's saved settings."""
    return DocsSlidesProvider(config)
