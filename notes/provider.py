"""The `notes` tool provider — a git-backed markdown notebook on the agent tool layer.

Seven tools, all of them operating on one directory of `.md` files with a git repository
under it: ``note_write``, ``note_read``, ``note_list``, ``note_search``, ``note_history``,
``note_restore``, ``note_delete``.

**This is an editor, not a knowledge store.** It builds no index, no embeddings and no
database, and it never writes to the knowledge library — when something in a note should
become durable knowledge, the agent hands it to core's own ``knowledge_create``. That
boundary is the whole design: the notebook owns *drafting and history*, Knowledge owns
*retrieval*, and nothing here duplicates the second.

Every note body that comes back out of the notebook is fenced with
``personalclaw.sdk.security.fence_untrusted`` before a model sees it. Notes are the user's
own writing, but a note is exactly where pasted web text, a quoted email or a code snippet
from an issue ends up, so the content is treated as data rather than instructions.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from typing import Any

from personalclaw.sdk.security import fence_untrusted
from personalclaw.sdk.tool import RiskLevel, ToolDefinition, ToolProvider, ToolResult

from notebook import (
    GIT_MISSING,
    WRITE_MODES,
    GitError,
    Notebook,
    NoteMissing,
    NoteRefError,
)

logger = logging.getLogger("notes")

_REF_HINT = "A note reference looks like 'ideas/tempo.md' — relative, '/' between folders."


class NotesProvider(ToolProvider):
    """Markdown notes in a git repository the user can read without this app."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = dict(config or {})
        self._path = str(self._config.get("notebook_path") or "").strip()
        self._timeout = max(5, int(self._config.get("timeout_secs", 20) or 20))
        self._max_results = max(1, min(200, int(self._config.get("max_results", 20) or 20)))
        self._book_impl: Notebook | None = None

    @property
    def _book(self) -> Notebook:
        """The notebook, bound on first use.

        Lazy for the same reason the findings log in `code-review` is: core constructs a
        provider just to READ its tool list (Settings → Tools, the manifest round-trip),
        and that must not mkdir under the user's home. The notebook appears the first time
        a note is actually touched.
        """
        if self._book_impl is None:
            self._book_impl = Notebook(self._path or None, timeout=self._timeout)
        return self._book_impl

    # ── Identity ────────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "notes"

    @property
    def display_name(self) -> str:
        return "Notes"

    def info(self) -> dict[str, Any]:
        # Deliberately reports the CONFIGURED path rather than the resolved one: resolving
        # it would bind the notebook, and binding it creates a directory.
        return {
            "notebook_path": self._path or "(this app's data dir)",
            "max_results": self._max_results,
            "git_available": shutil.which("git") is not None,
        }

    # ── Tool surface ────────────────────────────────────────────────────────────

    async def list_tools(self) -> list[ToolDefinition]:
        ref_param = {
            "type": "string",
            "description": "The note, relative to the notebook: 'ideas/tempo.md'. '.md' is optional.",
        }
        return [
            ToolDefinition(
                name="note_write",
                description=(
                    "Create, replace or append to a markdown note in the user's notebook and "
                    "commit that one note to git. Notes are plain files in a git repository, "
                    "so every version is recoverable. Use this for drafting and journaling; "
                    "use knowledge_create when something should become indexed knowledge."
                ),
                provider=self.name,
                parameters={
                    "type": "object",
                    "properties": {
                        "ref": ref_param,
                        "content": {"type": "string", "description": "The markdown to write."},
                        "mode": {
                            "type": "string",
                            "enum": list(WRITE_MODES),
                            "description": (
                                "'replace' (default) overwrites the note; 'append' adds to the "
                                "end of an existing one."
                            ),
                        },
                        "message": {
                            "type": "string",
                            "description": "Commit subject. Defaults to 'Add/Update <ref>'.",
                        },
                    },
                    "required": ["ref", "content"],
                },
                requires_approval=False,
                risk_level=RiskLevel.CAUTION,
            ),
            ToolDefinition(
                name="note_read",
                description=(
                    "Read one note. Pass `revision` (a sha from note_history, 'HEAD' or "
                    "'HEAD~2') to read a past version instead of the current one."
                ),
                provider=self.name,
                parameters={
                    "type": "object",
                    "properties": {
                        "ref": ref_param,
                        "revision": {
                            "type": "string",
                            "description": "A commit sha, 'HEAD', or 'HEAD~<n>'. Omit for current.",
                        },
                    },
                    "required": ["ref"],
                },
                requires_approval=False,
                risk_level=RiskLevel.SAFE,
                max_output=60_000,
            ),
            ToolDefinition(
                name="note_list",
                description=(
                    "List every note in the notebook — reference, title, size, last modified — "
                    "most recently changed first."
                ),
                provider=self.name,
                parameters={"type": "object", "properties": {}},
                requires_approval=False,
                risk_level=RiskLevel.SAFE,
                max_output=60_000,
            ),
            ToolDefinition(
                name="note_search",
                description=(
                    "Find matching LINES across the notebook. This is a literal, "
                    "case-insensitive text match over the files on disk — not semantic "
                    "retrieval. For meaning-based recall over the user's library, use "
                    "knowledge_search instead."
                ),
                provider=self.name,
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "The text to look for."},
                        "regex": {
                            "type": "boolean",
                            "description": "Treat the query as a regular expression (default false).",
                        },
                        "limit": {"type": "integer", "description": "Maximum matching lines."},
                    },
                    "required": ["query"],
                },
                requires_approval=False,
                risk_level=RiskLevel.SAFE,
                max_output=60_000,
            ),
            ToolDefinition(
                name="note_history",
                description=(
                    "The git history of one note, or of the whole notebook when `ref` is "
                    "omitted. Returns the shas note_read and note_restore take."
                ),
                provider=self.name,
                parameters={
                    "type": "object",
                    "properties": {
                        "ref": {
                            "type": "string",
                            "description": "The note. Omit for the whole notebook's history.",
                        },
                        "limit": {"type": "integer", "description": "How many commits (default 10)."},
                    },
                },
                requires_approval=False,
                risk_level=RiskLevel.SAFE,
            ),
            ToolDefinition(
                name="note_restore",
                description=(
                    "Bring a past version of a note back as a NEW commit. History is never "
                    "rewritten, so the restore is itself undoable."
                ),
                provider=self.name,
                parameters={
                    "type": "object",
                    "properties": {
                        "ref": ref_param,
                        "revision": {
                            "type": "string",
                            "description": "The revision to restore: a sha, 'HEAD', or 'HEAD~<n>'.",
                        },
                    },
                    "required": ["ref", "revision"],
                },
                requires_approval=False,
                risk_level=RiskLevel.CAUTION,
            ),
            ToolDefinition(
                name="note_delete",
                description=(
                    "Remove a note from the notebook. A committed note stays in git history "
                    "and can be restored; a note that was never committed cannot."
                ),
                provider=self.name,
                parameters={
                    "type": "object",
                    "properties": {"ref": ref_param},
                    "required": ["ref"],
                },
                requires_approval=True,
                risk_level=RiskLevel.DESTRUCTIVE,
            ),
        ]

    async def invoke(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        handlers = {
            "note_write": self._write,
            "note_read": self._read,
            "note_list": self._list,
            "note_search": self._search,
            "note_history": self._history,
            "note_restore": self._restore,
            "note_delete": self._delete,
        }
        handler = handlers.get(tool_name)
        if handler is None:
            return ToolResult(
                success=False,
                error=f"Unknown tool: {tool_name!r}",
                recovery_hints=[f"This provider exposes: {', '.join(sorted(handlers))}."],
            )
        try:
            return await handler(arguments)
        except NoteRefError as exc:
            return ToolResult(success=False, error=str(exc), recovery_hints=[_REF_HINT])
        except NoteMissing as exc:
            return ToolResult(
                success=False, error=str(exc),
                recovery_hints=["Call note_list to see what is in the notebook."],
            )
        except GitError as exc:
            hints = ["Install git — the notebook is a git repository."] if str(exc) == GIT_MISSING \
                else ["Check the notebook path in Settings, and that it is a git worktree."]
            return ToolResult(success=False, error=str(exc), recovery_hints=hints)
        except OSError as exc:
            # A notebook on a full disk, a read-only mount, a path the gateway user cannot
            # write: a legible failure, not a traceback out of the tool layer.
            logger.warning("notebook I/O failed for %s: %s", tool_name, exc)
            return ToolResult(
                success=False,
                error=f"The notebook could not be read or written: {exc}",
                recovery_hints=[
                    "Check that the notebook path exists and is writable by PersonalClaw.",
                ],
            )

    # ── Handlers ────────────────────────────────────────────────────────────────

    async def _write(self, args: dict[str, Any]) -> ToolResult:
        ref = str(args.get("ref") or "")
        content = args.get("content")
        if content is None:
            return ToolResult(
                success=False, error="content is required (pass an empty string for an empty note)",
            )
        mode = str(args.get("mode") or "replace")
        result = await asyncio.to_thread(
            self._book.write, ref, str(content), mode=mode,
            message=str(args.get("message") or ""),
        )
        logger.info(
            "note %s %s (commit %s)", result["ref"],
            "unchanged" if result["unchanged"] else ("created" if result["created"] else "updated"),
            result["commit"] or "-",
        )
        if result["unchanged"]:
            body = f"`{result['ref']}` already had exactly this content — nothing committed."
        else:
            verb = "Created" if result["created"] else "Updated"
            body = (f"{verb} `{result['ref']}` ({result['bytes']} bytes) and committed it as "
                    f"`{result['commit']}`.")
        return ToolResult(success=True, output=body, metadata=result)

    async def _read(self, args: dict[str, Any]) -> ToolResult:
        note = await asyncio.to_thread(
            self._book.read, str(args.get("ref") or ""),
            revision=str(args.get("revision") or ""),
        )
        fenced = fence_untrusted(
            note["content"],
            source=f"personal note: {note['ref']} ({note['revision']})",
            source_type="notes_note",
            source_id=note["ref"],
        )
        header = f"# `{note['ref']}` — {note['revision']}\n\n"
        return ToolResult(
            success=True,
            output=header + fenced,
            metadata={"ref": note["ref"], "revision": note["revision"],
                      "bytes": len(note["content"].encode("utf-8"))},
        )

    async def _list(self, _args: dict[str, Any]) -> ToolResult:
        notes, skipped = await asyncio.to_thread(self._book.list_notes)
        if not notes:
            hint = f" ({skipped} file(s) present but not addressable by this app.)" if skipped else ""
            return ToolResult(
                success=True,
                output=f"The notebook is empty — write the first note with note_write.{hint}",
                metadata={"notes": 0, "unaddressable_files": skipped,
                          "root": str(self._book.root)},
            )
        rows = ["| note | title | size | modified |", "|---|---|---|---|"]
        rows += [
            f"| `{n.ref}` | {n.title} | {n.bytes} B | {n.to_dict()['modified']} |" for n in notes
        ]
        # The titles are lifted out of the notes themselves, so the table is note content
        # and is fenced like any other.
        table = fence_untrusted(
            "\n".join(rows), source="personal notebook index",
            source_type="notes_index", source_id=str(self._book.root),
        )
        tail = f"\n\n{skipped} file(s) in the notebook are not addressable by this app." if skipped else ""
        return ToolResult(
            success=True,
            output=f"{len(notes)} note(s) in `{self._book.root}`:\n\n{table}{tail}",
            metadata={"notes": len(notes), "unaddressable_files": skipped,
                      "root": str(self._book.root),
                      "refs": [n.ref for n in notes]},
        )

    async def _search(self, args: dict[str, Any]) -> ToolResult:
        limit = args.get("limit")
        try:
            cap = int(limit) if limit is not None else self._max_results
        except (TypeError, ValueError):
            cap = self._max_results
        hits, capped = await asyncio.to_thread(
            self._book.search, str(args.get("query") or ""),
            regex=bool(args.get("regex")), limit=cap,
        )
        if not hits:
            return ToolResult(
                success=True,
                output="No note line matched. This is a literal text match — try "
                       "knowledge_search for meaning-based recall.",
                metadata={"hits": 0},
            )
        lines = [f"{h.ref}:{h.line}: {h.text}" for h in hits]
        fenced = fence_untrusted(
            "\n".join(lines), source="personal notebook search results",
            source_type="notes_search", source_id=str(self._book.root),
        )
        tail = f"\n\nStopped at the {cap}-line limit; narrow the query for the rest." if capped else ""
        return ToolResult(
            success=True,
            output=f"{len(hits)} matching line(s):\n\n{fenced}{tail}",
            metadata={"hits": len(hits), "capped": capped,
                      "results": [h.to_dict() for h in hits]},
        )

    async def _history(self, args: dict[str, Any]) -> ToolResult:
        limit = args.get("limit")
        try:
            count = int(limit) if limit is not None else 10
        except (TypeError, ValueError):
            count = 10
        ref = str(args.get("ref") or "")
        commits = await asyncio.to_thread(self._book.history, ref, limit=count)
        scope = f"`{ref}`" if ref else "the notebook"
        if not commits:
            return ToolResult(
                success=True,
                output=f"No commits recorded for {scope} yet.",
                metadata={"commits": 0, "ref": ref},
            )
        rows = [f"- `{c.sha}` {c.when} — {c.subject}" for c in commits]
        return ToolResult(
            success=True,
            output=f"{len(commits)} commit(s) for {scope}:\n\n" + "\n".join(rows),
            metadata={"commits": len(commits), "ref": ref,
                      "history": [c.to_dict() for c in commits]},
        )

    async def _restore(self, args: dict[str, Any]) -> ToolResult:
        result = await asyncio.to_thread(
            self._book.restore, str(args.get("ref") or ""), str(args.get("revision") or ""),
        )
        if result["unchanged"]:
            body = (f"`{result['ref']}` is already identical to {result['restored_from']} — "
                    "nothing committed.")
        else:
            body = (f"Restored `{result['ref']}` from {result['restored_from']} as a new commit "
                    f"`{result['commit']}`. The version you replaced is still in history.")
        return ToolResult(success=True, output=body, metadata=result)

    async def _delete(self, args: dict[str, Any]) -> ToolResult:
        result = await asyncio.to_thread(self._book.delete, str(args.get("ref") or ""))
        if result["recoverable"]:
            body = (f"Deleted `{result['ref']}` (commit `{result['commit']}`). It is still in git "
                    "history — note_history on the notebook will show the deleting commit, and "
                    "note_read with the sha before it returns the content.")
        else:
            body = (f"Deleted `{result['ref']}`. It had never been committed, so there is no "
                    "version to restore.")
        logger.info("note %s deleted (recoverable=%s)", result["ref"], result["recoverable"])
        return ToolResult(success=True, output=body, metadata=result)


def create_provider(config: dict[str, Any] | None = None) -> NotesProvider:
    """Manifest factory — core calls this with this app's saved settings."""
    return NotesProvider(config)
