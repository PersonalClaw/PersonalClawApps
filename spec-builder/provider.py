"""The `spec-builder` tool provider — a spec-writing front over PersonalClaw's workflow engine.

Eight tools over one closed spec shape: ``spec_open``, ``spec_write``, ``spec_read``,
``spec_list``, ``spec_seed``, ``spec_review``, ``spec_compile``, ``spec_delete``.

**This is a spec front, not a second planner.** It owns the written spec — the problem, the
done-when clauses, the steps, how done-ness is decided — and it compiles that spec into a
workflow DEFINITION in the engine's own format. It does not run anything. There is no
scheduler here, no run journal, no retry policy and no node executor; ``spec_compile`` hands
its definition to core's ``workflow_author`` and the run is started with ``workflow_start``.
Every execution concern stays with the one component that owns the journal.

Every spec body that comes back out of the store is fenced with
``personalclaw.sdk.security.fence_untrusted`` before a model sees it. Most of a spec is the
user's own writing, but ``spec_seed`` folds file content read out of a repository into the
``background`` section — and a spec is exactly where a pasted issue, a quoted design doc or
somebody else's README ends up. The content is data, not instructions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from typing import Any

from personalclaw.sdk.security import fence_untrusted
from personalclaw.sdk.tool import RiskLevel, ToolDefinition, ToolProvider, ToolResult

from specs import (
    GIT_MISSING,
    MAX_SEED_BYTES,
    REQUIRED_SECTIONS,
    SECTIONS,
    SEEDED_SECTION,
    WRITE_MODES,
    GitError,
    Spec,
    SpecMissing,
    SpecRefError,
    SpecStore,
    parse_section,
)
from workflow_spec import NotReady, compile_spec, def_name

logger = logging.getLogger("spec-builder")

_ID_HINT = "A spec id is lowercase letters, digits and hyphens — 'inbox-triage'."
_SECTION_HINT = f"The sections are: {', '.join(SECTIONS)}."


class SpecBuilderProvider(ToolProvider):
    """Written specs that compile into definitions the workflow engine runs."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = dict(config or {})
        self._source_repo = str(self._config.get("source_repo") or "").strip()
        self._timeout = max(5, int(self._config.get("timeout_secs", 20) or 20))
        self._store_impl: SpecStore | None = None

    @property
    def _store(self) -> SpecStore:
        """The spec store, bound on first use.

        Lazy because core constructs a provider just to READ its tool list (Settings -> Tools,
        the manifest round-trip), and that must not mkdir under the user's home. The store
        appears the first time a spec is actually touched.
        """
        if self._store_impl is None:
            self._store_impl = SpecStore(source_repo=self._source_repo, timeout=self._timeout)
        return self._store_impl

    # ── Identity ────────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "spec-builder"

    @property
    def display_name(self) -> str:
        return "Spec Builder"

    def info(self) -> dict[str, Any]:
        # Reports the CONFIGURED repo rather than a resolved one: resolving would bind the
        # store, and binding it creates a directory.
        return {
            "source_repo": self._source_repo or "(none — spec_seed is unavailable)",
            "sections": list(SECTIONS),
            "git_available": shutil.which("git") is not None,
        }

    # ── Tool surface ────────────────────────────────────────────────────────────

    async def list_tools(self) -> list[ToolDefinition]:
        spec_param = {
            "type": "string",
            "description": "The spec id, e.g. 'inbox-triage'.",
        }
        return [
            ToolDefinition(
                name="spec_open",
                description=(
                    "Start a new written spec: a title and, optionally, the one-line intent. "
                    "Returns the spec id and which sections still have to be filled in. Use "
                    "this before spec_write — a spec has to exist to be written into."
                ),
                provider=self.name,
                parameters={
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "What this spec is for, in a phrase.",
                        },
                        "intent": {
                            "type": "string",
                            "description": (
                                "One line on what changes and why. Becomes the compiled "
                                "workflow's description."
                            ),
                        },
                        "spec_id": {
                            "type": "string",
                            "description": (
                                "Override the id derived from the title. Lowercase letters, "
                                "digits and hyphens."
                            ),
                        },
                    },
                    "required": ["title"],
                },
                requires_approval=False,
                risk_level=RiskLevel.CAUTION,
            ),
            ToolDefinition(
                name="spec_write",
                description=(
                    "Write one section of a spec. `outcome` is one `- ` bullet per done-when "
                    "clause; `steps` is one `- <label>: <instruction>` bullet per step — both "
                    "grammars are what the compiler turns into gates and stages, so write them "
                    "that way. Sections: " + ", ".join(SECTIONS) + "."
                ),
                provider=self.name,
                parameters={
                    "type": "object",
                    "properties": {
                        "spec": spec_param,
                        "section": {
                            "type": "string",
                            "enum": list(SECTIONS),
                            "description": "Which section to write.",
                        },
                        "content": {
                            "type": "string",
                            "description": "The markdown for that section.",
                        },
                        "mode": {
                            "type": "string",
                            "enum": list(WRITE_MODES),
                            "description": (
                                "'replace' (default) overwrites the section; 'append' adds to "
                                "the end of it."
                            ),
                        },
                        "title": {
                            "type": "string",
                            "description": "Optionally also update the spec's title.",
                        },
                        "intent": {
                            "type": "string",
                            "description": "Optionally also update the spec's intent line.",
                        },
                    },
                    "required": ["spec", "section", "content"],
                },
                requires_approval=False,
                risk_level=RiskLevel.CAUTION,
            ),
            ToolDefinition(
                name="spec_read",
                description=(
                    "Read a spec back as markdown — every section, plus its readiness verdict. "
                    "Pass `section` to read just one."
                ),
                provider=self.name,
                parameters={
                    "type": "object",
                    "properties": {
                        "spec": spec_param,
                        "section": {
                            "type": "string",
                            "enum": list(SECTIONS),
                            "description": "One section only. Omit for the whole spec.",
                        },
                    },
                    "required": ["spec"],
                },
                requires_approval=False,
                risk_level=RiskLevel.SAFE,
                max_output=80_000,
            ),
            ToolDefinition(
                name="spec_list",
                description=(
                    "Every spec in the store — id, title, how many clauses and steps parsed, "
                    "which sections are filled, and whether it is ready to compile."
                ),
                provider=self.name,
                parameters={"type": "object", "properties": {}},
                requires_approval=False,
                risk_level=RiskLevel.SAFE,
            ),
            ToolDefinition(
                name="spec_seed",
                description=(
                    "Read one file out of the configured source repository at a named revision "
                    "and fold it into the spec's `background` section, so the spec is grounded "
                    "in the code rather than in a guess. Read-only: it never writes to the "
                    "repository. The content is treated as untrusted data."
                ),
                provider=self.name,
                parameters={
                    "type": "object",
                    "properties": {
                        "spec": spec_param,
                        "path": {
                            "type": "string",
                            "description": (
                                "The file, relative to the repository root: " "'src/app/router.py'."
                            ),
                        },
                        "revision": {
                            "type": "string",
                            "description": (
                                "A commit sha, 'HEAD', or 'HEAD~<n>'. Defaults to HEAD. A "
                                "branch name is refused."
                            ),
                        },
                    },
                    "required": ["spec", "path"],
                },
                requires_approval=False,
                # Read-only against the REPOSITORY, but it spawns `git` and writes the
                # fetched content into the spec — not SAFE (SAFE = local read, no exec).
                risk_level=RiskLevel.CAUTION,
                max_output=60_000,
            ),
            ToolDefinition(
                name="spec_review",
                description=(
                    "The structural readiness verdict for a spec: which required sections are "
                    "empty, how many done-when clauses and steps parsed, and which steps have "
                    "no instruction. Costs nothing — it is a count over the sections, not a "
                    "judgement about the prose."
                ),
                provider=self.name,
                parameters={
                    "type": "object",
                    "properties": {"spec": spec_param},
                    "required": ["spec"],
                },
                requires_approval=False,
                risk_level=RiskLevel.SAFE,
            ),
            ToolDefinition(
                name="spec_compile",
                description=(
                    "Compile a ready spec into a workflow DEFINITION for the workflow engine: "
                    "one stage per step, a verify_command gate on the spec's own command, then "
                    "a done-when review and a gate on it. This tool runs nothing — pass the "
                    "returned definition to workflow_author (save=false first to validate), "
                    "then workflow_start it. Refuses an unready spec unless `force` is set."
                ),
                provider=self.name,
                parameters={
                    "type": "object",
                    "properties": {
                        "spec": spec_param,
                        "force": {
                            "type": "boolean",
                            "description": (
                                "Compile a spec that spec_review refused, to see the shape. "
                                "The verdict comes back with it."
                            ),
                        },
                    },
                    "required": ["spec"],
                },
                requires_approval=False,
                risk_level=RiskLevel.SAFE,
                max_output=80_000,
            ),
            ToolDefinition(
                name="spec_delete",
                description=(
                    "Remove a spec from the store. There is no history here — a deleted spec is "
                    "gone, and any workflow definition already saved from it is untouched."
                ),
                provider=self.name,
                parameters={
                    "type": "object",
                    "properties": {"spec": spec_param},
                    "required": ["spec"],
                },
                requires_approval=True,
                risk_level=RiskLevel.DESTRUCTIVE,
            ),
        ]

    # ── Dispatch ────────────────────────────────────────────────────────────────

    async def invoke(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        handlers = {
            "spec_open": self._open,
            "spec_write": self._write,
            "spec_read": self._read,
            "spec_list": self._list,
            "spec_seed": self._seed,
            "spec_review": self._review,
            "spec_compile": self._compile,
            "spec_delete": self._delete,
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
        except SpecRefError as exc:
            return ToolResult(
                success=False, error=str(exc), recovery_hints=[_ID_HINT, _SECTION_HINT]
            )
        except SpecMissing as exc:
            return ToolResult(
                success=False,
                error=str(exc),
                recovery_hints=["Call spec_list to see what specs exist."],
            )
        except GitError as exc:
            hints = (
                ["Install git — spec_seed reads the file through `git show`."]
                if str(exc) == GIT_MISSING
                else [
                    "Check the source repository in Settings, and that the path exists at "
                    "that revision.",
                ]
            )
            return ToolResult(success=False, error=str(exc), recovery_hints=hints)
        except OSError as exc:
            # A store on a full disk, a read-only mount, a path the gateway user cannot
            # write: a legible failure, not a traceback out of the tool layer.
            logger.warning("spec store I/O failed for %s: %s", tool_name, exc)
            return ToolResult(
                success=False,
                error=f"The spec store could not be read or written: {exc}",
                recovery_hints=[
                    "Check that PersonalClaw's app data directory is writable.",
                ],
            )

    # ── Handlers ────────────────────────────────────────────────────────────────

    async def _open(self, args: dict[str, Any]) -> ToolResult:
        spec = await asyncio.to_thread(
            self._store.open_spec,
            str(args.get("title") or ""),
            intent=str(args.get("intent") or ""),
            spec_id=str(args.get("spec_id") or ""),
        )
        readiness = spec.readiness()
        logger.info("spec %s opened", spec.id)
        missing = ", ".join(f"`{name}`" for name in REQUIRED_SECTIONS)
        return ToolResult(
            success=True,
            output=(
                f"Opened spec `{spec.id}`. Fill in {missing} with spec_write, then "
                f"spec_review it.\n\n"
                "- `outcome`: one `- ` bullet per done-when clause.\n"
                "- `steps`: one `- <label>: <instruction>` bullet per step.\n"
                "- `verification`: how done-ness is actually decided.\n"
                f"- `{SEEDED_SECTION}`: grounding material — spec_seed writes here."
            ),
            metadata={"spec": spec.id, "readiness": readiness.to_dict()},
        )

    async def _write(self, args: dict[str, Any]) -> ToolResult:
        content = args.get("content")
        if content is None:
            return ToolResult(
                success=False,
                error="content is required (pass an empty string to clear a section)",
            )
        spec_id = str(args.get("spec") or "")
        result = await asyncio.to_thread(
            self._store.write_section,
            spec_id,
            str(args.get("section") or ""),
            str(content),
            mode=str(args.get("mode") or "replace"),
        )
        title = str(args.get("title") or "")
        intent = str(args.get("intent") or "")
        if title or intent:
            result["meta"] = await asyncio.to_thread(
                self._store.set_meta, spec_id, title=title, intent=intent
            )
        # Shape only: the section name and its size, never a line of what was written.
        logger.info(
            "spec %s section %s %s (%d chars)",
            result["spec"],
            result["section"],
            "unchanged" if result["unchanged"] else result["mode"],
            result["chars"],
        )
        readiness = result["readiness"]
        verdict = (
            "ready to compile"
            if readiness["ready"]
            else "not ready: " + "; ".join(readiness["problems"])
        )
        note = " (already had exactly this content)" if result["unchanged"] else ""
        return ToolResult(
            success=True,
            output=(
                f"Wrote `{result['section']}` on spec `{result['spec']}` "
                f"({result['chars']} chars){note}. Spec is {verdict}."
            ),
            metadata=result,
        )

    async def _read(self, args: dict[str, Any]) -> ToolResult:
        spec = await asyncio.to_thread(self._store.load, str(args.get("spec") or ""))
        section = str(args.get("section") or "")
        if section:
            # Validated here because no store call on this path would otherwise do it.
            body = spec.section(parse_section(section))
            rendered = f"## {section}\n\n{body.strip() or '_(empty)_'}\n"
        else:
            rendered = _render(spec)
        fenced = fence_untrusted(
            rendered,
            source=f"spec: {spec.id}",
            source_type="spec_builder_spec",
            source_id=spec.id,
        )
        readiness = spec.readiness()
        return ToolResult(
            success=True,
            output=f"# `{spec.id}` — {spec.title}\n\n{fenced}",
            metadata={"spec": spec.id, "readiness": readiness.to_dict()},
        )

    async def _list(self, _args: dict[str, Any]) -> ToolResult:
        specs, unreadable = await asyncio.to_thread(self._store.list_specs)
        if not specs:
            hint = f" ({unreadable} record(s) present but unreadable.)" if unreadable else ""
            return ToolResult(
                success=True,
                output=f"No specs yet — start one with spec_open.{hint}",
                metadata={"specs": 0, "unreadable": unreadable},
            )
        rows = ["| spec | title | clauses | steps | ready | updated |", "|---|---|---|---|---|---|"]
        for spec in specs:
            row = spec.summary()
            rows.append(
                f"| `{row['id']}` | {row['title']} | {row['clauses']} | {row['steps']} | "
                f"{'yes' if row['ready'] else 'no'} | {row['updated']} |"
            )
        # The titles are the user's own text, and a seeded spec's title may have been lifted
        # out of fetched material, so the table is fenced like any other spec content.
        table = fence_untrusted(
            "\n".join(rows),
            source="spec store index",
            source_type="spec_builder_index",
            source_id=str(self._store.root),
        )
        tail = f"\n\n{unreadable} record(s) will not parse and were skipped." if unreadable else ""
        return ToolResult(
            success=True,
            output=f"{len(specs)} spec(s):\n\n{table}{tail}",
            metadata={
                "specs": len(specs),
                "unreadable": unreadable,
                "rows": [s.summary() for s in specs],
            },
        )

    async def _seed(self, args: dict[str, Any]) -> ToolResult:
        spec_id = str(args.get("spec") or "")
        path = str(args.get("path") or "")
        revision = str(args.get("revision") or "")
        source = await asyncio.to_thread(self._store.read_source, path, revision=revision)
        heading = f"\n### `{source['path']}` @ {source['resolved']}\n\n"
        written = await asyncio.to_thread(
            self._store.write_section,
            spec_id,
            SEEDED_SECTION,
            heading + source["text"],
            mode="append",
        )
        # The ref and the verdict, never the body: this is content read out of a repository
        # and a log line carrying it would be an injection surface of its own.
        logger.info(
            "spec %s seeded from %s@%s (%d chars, truncated=%s)",
            written["spec"],
            source["path"],
            source["resolved"],
            source["chars"],
            source["truncated"],
        )
        fenced = fence_untrusted(
            source["text"],
            source=f"repository file: {source['path']} @ {source['resolved']}",
            source_type="spec_builder_source",
            source_id=f"{source['resolved']}:{source['path']}",
        )
        cap = (
            f"\n\nTruncated at {MAX_SEED_BYTES} bytes — seed a narrower file if the rest matters."
            if source["truncated"]
            else ""
        )
        return ToolResult(
            success=True,
            output=(
                f"Folded `{source['path']}` @ `{source['resolved']}` into "
                f"`{written['spec']}`'s `{SEEDED_SECTION}` ({source['chars']} chars).\n\n"
                f"{fenced}{cap}"
            ),
            metadata={
                "spec": written["spec"],
                "path": source["path"],
                "revision": source["revision"],
                "resolved": source["resolved"],
                "chars": source["chars"],
                "truncated": source["truncated"],
                "readiness": written["readiness"],
            },
        )

    async def _review(self, args: dict[str, Any]) -> ToolResult:
        spec = await asyncio.to_thread(self._store.load, str(args.get("spec") or ""))
        readiness = spec.readiness()
        logger.info(
            "spec %s reviewed: ready=%s (%d problem(s))",
            spec.id,
            readiness.ready,
            len(readiness.problems),
        )
        if readiness.ready:
            body = (
                f"`{spec.id}` is ready to compile: {readiness.clauses} done-when clause(s), "
                f"{readiness.steps} step(s). spec_compile turns it into a workflow definition."
            )
        else:
            problems = "\n".join(f"- {p}" for p in readiness.problems)
            body = f"`{spec.id}` is not ready to compile:\n\n{problems}"
        return ToolResult(success=True, output=body, metadata=readiness.to_dict())

    async def _compile(self, args: dict[str, Any]) -> ToolResult:
        spec = await asyncio.to_thread(self._store.load, str(args.get("spec") or ""))
        try:
            compiled = await asyncio.to_thread(compile_spec, spec, force=bool(args.get("force")))
        except NotReady as exc:
            problems = "\n".join(f"- {p}" for p in exc.readiness.problems)
            return ToolResult(
                success=False,
                error=str(exc),
                recovery_hints=[
                    "Fill in what spec_review names, or pass force=true to see the shape "
                    "anyway.",
                ],
                metadata=exc.readiness.to_dict(),
                output=f"`{spec.id}` is not ready:\n\n{problems}",
            )
        logger.info(
            "spec %s compiled to definition %s (%d node(s), forced=%s)",
            spec.id,
            compiled["definition"]["name"],
            compiled["nodes"],
            compiled["forced"],
        )
        forced = (
            "\n\n**Compiled with force** — the readiness problems above still stand and the "
            "definition reflects them."
            if compiled["forced"]
            else ""
        )
        body = "\n".join(
            [
                f"Compiled `{spec.id}` into the workflow definition "
                f"`{compiled['definition']['name']}` — {compiled['nodes']} node(s), "
                f"{compiled['clauses']} done-when clause(s).",
                "",
                "This tool ran nothing. To land and start it:",
                "",
                "1. `workflow_author` with save=false to validate, then save=true.",
                "2. `workflow_start` the definition with `cwd` and `verify_command`.",
                "",
                f"The `{SEEDED_SECTION}` section is deliberately not in any prompt — see the "
                "app README on why grounding material stays out of a saved definition.",
                "",
                "```json",
                json.dumps(compiled["definition"], indent=2),
                "```",
                forced,
            ]
        )
        return ToolResult(success=True, output=body, metadata=compiled)

    async def _delete(self, args: dict[str, Any]) -> ToolResult:
        result = await asyncio.to_thread(self._store.delete, str(args.get("spec") or ""))
        logger.info("spec %s deleted", result["spec"])
        leftovers = result["leftover_files"]
        tail = (
            f" The spec directory still holds {len(leftovers)} file(s) this app did not "
            "create, so it was left in place."
            if leftovers
            else ""
        )
        return ToolResult(
            success=True,
            output=(
                f"Deleted spec `{result['spec']}`. Any workflow definition already saved from "
                f"it (`{def_name(result['spec'])}`) is untouched — delete that with "
                f"workflow_delete_def if you want it gone too.{tail}"
            ),
            metadata=result,
        )


def _render(spec: Spec) -> str:
    """The whole spec as markdown, in the section order the vocabulary declares."""
    parts: list[str] = []
    if spec.intent:
        parts.append(f"**Intent:** {spec.intent}\n")
    for name in SECTIONS:
        body = spec.section(name).strip()
        parts.append(f"## {name}\n\n{body or '_(empty)_'}\n")
    return "\n".join(parts)


def create_provider(config: dict[str, Any] | None = None) -> SpecBuilderProvider:
    """Manifest factory — core calls this with this app's saved settings."""
    return SpecBuilderProvider(config)
