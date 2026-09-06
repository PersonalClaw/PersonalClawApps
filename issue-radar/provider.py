"""The `issue-radar` tool provider — triage a tracker's open issues, keep the notes locally.

Four tools on the agent tool layer:

* ``triage_issues`` — read a repository's open issues through the local ``gh``/``glab``,
  suggest labels for each from the repository's OWN label set, rank them by how much they
  need a maintainer, and keep the sweep on this machine.
* ``record_investigation`` — the write end: whoever investigated an issue (a spawned
  subagent, or the host agent itself) appends what it found to that issue's local note log.
* ``issue_notes`` — read an issue's notes back off this machine.
* ``radar_status`` — read the last sweep back, per repository.

**How the label suggestions are actually produced, precisely.** Three legs, and the report
always names which one ran:

1. ``label_source="model"`` (default) — N bounded-concurrency model calls through
   ``personalclaw.sdk.model``: a FRESH provider instance per issue, given only that issue's
   fenced text and the repository's label list, torn down after. Context isolation is
   total; process isolation is not, and the report says ``model`` rather than ``subagent``
   so nobody reads more into it than that.
2. ``label_source="plan"`` — no model call. ``triage_issues`` returns one brief per issue
   and the host agent spawns a real subagent per brief, each reporting back through
   ``record_investigation``. Core has a subagent primitive, but the SDK only hands it to an
   app through ``GatewayServices.subagent_mgr`` and a ``ToolProvider`` is constructed with
   its settings dict and nothing else — so this app cannot spawn one itself and does not
   pretend to.
3. ``label_source="rules"`` — the conservative deterministic pass only.

That deterministic pass runs in all three legs: it needs no model, so a sweep is never
empty for want of one.

Imports stay on the SDK surface (``personalclaw.sdk.*``), never a core internal.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import datetime, timezone
from typing import Any

from personalclaw.sdk.security import fence_untrusted
from personalclaw.sdk.tool import RiskLevel, ToolDefinition, ToolProvider, ToolResult

from triage import (
    APP_NAME,
    HOST_GITHUB,
    HOST_GITLAB,
    IssueBrief,
    Note,
    NoteLog,
    RepoRef,
    Suggestion,
    SweepStore,
    build_briefs,
    issues_from_github,
    issues_from_gitlab,
    labels_from_github,
    labels_from_gitlab,
    parse_issue_ref,
    parse_model_suggestions,
    parse_repo_ref,
    render_sweep,
    triage,
)

logger = logging.getLogger("issue_radar")

LABEL_SOURCES = ("model", "plan", "rules")

# The one binary each host is read through. The app opens no socket of its own; whatever
# the user's CLI is already signed in to is exactly what the app can see.
HOST_BINARIES = {HOST_GITHUB: "gh", HOST_GITLAB: "glab"}

_MISSING = {
    HOST_GITHUB: (
        "The `gh` CLI is not on PATH. Install GitHub CLI (https://cli.github.com) and run "
        "`gh auth login` — this app has no network access of its own and reads GitHub only "
        "through `gh`."
    ),
    HOST_GITLAB: (
        "The `glab` CLI is not on PATH. Install GitLab CLI (https://gitlab.com/gitlab-org/cli) "
        "and run `glab auth login` — this app has no network access of its own and reads "
        "GitLab only through `glab`."
    ),
}


class TrackerError(RuntimeError):
    """The CLI was reachable but refused the request (not signed in, no such repo, …)."""


class IssueRadarProvider(ToolProvider):
    """Issue triage with conservative label suggestions and local investigation notes."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = dict(config or {})
        source = str(self._config.get("label_source") or "model")
        self._label_source = source if source in LABEL_SOURCES else "model"
        self._max_issues = self._bounded("max_issues", 30, 1, 200)
        self._stale_days = self._bounded("stale_days", 30, 1, 3650)
        self._concurrency = self._bounded("concurrency", 3, 1, 8)
        self._timeout = self._bounded("timeout_secs", 20, 5, 300)
        self._model_entry = str(self._config.get("model_entry") or "").strip()
        self._notes_impl: NoteLog | None = None
        self._sweeps_impl: SweepStore | None = None

    def _bounded(self, key: str, default: int, low: int, high: int) -> int:
        try:
            value = int(self._config.get(key) or default)
        except (TypeError, ValueError):
            value = default
        return max(low, min(high, value))

    # The two stores open on first use. Constructing a provider is what core does to READ
    # its tool list (Settings → Tools, the manifest round-trip), and that must not mkdir
    # under the user's home; the directories appear the first time a tool actually runs.

    @property
    def _notes(self) -> NoteLog:
        if self._notes_impl is None:
            self._notes_impl = NoteLog()
        return self._notes_impl

    @property
    def _sweeps(self) -> SweepStore:
        if self._sweeps_impl is None:
            self._sweeps_impl = SweepStore()
        return self._sweeps_impl

    # ── Identity ────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return APP_NAME

    @property
    def display_name(self) -> str:
        return "Issue Radar"

    def info(self) -> dict[str, Any]:
        return {
            "label_source": self._label_source,
            "max_issues": self._max_issues,
            "stale_days": self._stale_days,
            "concurrency": self._concurrency,
            "gh_available": shutil.which("gh") is not None,
            "glab_available": shutil.which("glab") is not None,
        }

    # ── Tool surface ────────────────────────────────────────────────────────

    async def list_tools(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name="triage_issues",
                description=(
                    "Triage a repository's open issues. Reads them through the local `gh` "
                    "(GitHub) or `glab` (GitLab) CLI, suggests labels for each from the "
                    "repository's OWN label set with the evidence that justifies each one, "
                    "and ranks the issues by how much they need a maintainer (unlabelled, "
                    "unassigned, stale, security signal). Read-only against the tracker: "
                    "nothing is ever labelled, commented on or closed. The sweep is kept "
                    "locally."
                ),
                provider=self.name,
                parameters={
                    "type": "object",
                    "properties": {
                        "repo": {
                            "type": "string",
                            "description": (
                                "The repository: 'owner/repo' for GitHub, "
                                "'gitlab:group/project' for GitLab, or either host's URL."
                            ),
                        },
                        "label_source": {
                            "type": "string",
                            "enum": list(LABEL_SOURCES),
                            "description": (
                                "'model' (default): one isolated model call per issue. "
                                "'plan': return one brief per issue and spawn your own "
                                "subagent per brief. 'rules': the conservative "
                                "deterministic pass only, no model."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": "How many open issues to read (default 30).",
                        },
                    },
                    "required": ["repo"],
                },
                requires_approval=False,
                risk_level=RiskLevel.SAFE,
                max_output=60_000,
            ),
            ToolDefinition(
                name="record_investigation",
                description=(
                    "Append one investigation note about one issue to the local note log: "
                    "what you found, the next step, and any labels the investigation "
                    "supports. This is how a per-issue subagent reports back after "
                    "triage_issues was called with label_source='plan'."
                ),
                provider=self.name,
                parameters={
                    "type": "object",
                    "properties": {
                        "issue": {
                            "type": "string",
                            "description": (
                                "The issue: 'owner/repo#123', 'gitlab:group/project#12', "
                                "or its issue URL."
                            ),
                        },
                        "note": {
                            "type": "string",
                            "description": "What the investigation found.",
                        },
                        "next_step": {
                            "type": "string",
                            "description": "One sentence: what should happen next.",
                        },
                        "labels": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Labels this investigation supports.",
                        },
                    },
                    "required": ["issue", "note"],
                },
                requires_approval=False,
                risk_level=RiskLevel.SAFE,
            ),
            ToolDefinition(
                name="issue_notes",
                description=(
                    "Read back the locally-kept investigation notes for one issue (or list "
                    "every issue investigated on this machine when `issue` is omitted)."
                ),
                provider=self.name,
                parameters={
                    "type": "object",
                    "properties": {
                        "issue": {
                            "type": "string",
                            "description": "The issue to read. Omit to list all.",
                        },
                    },
                },
                requires_approval=False,
                risk_level=RiskLevel.SAFE,
                max_output=60_000,
            ),
            ToolDefinition(
                name="radar_status",
                description=(
                    "Read back the last triage sweep for a repository — its queue, scores "
                    "and suggestions — without touching the tracker again. Omit `repo` to "
                    "list every repository swept on this machine."
                ),
                provider=self.name,
                parameters={
                    "type": "object",
                    "properties": {
                        "repo": {
                            "type": "string",
                            "description": "The repository to read. Omit to list all.",
                        },
                    },
                },
                requires_approval=False,
                risk_level=RiskLevel.SAFE,
                max_output=60_000,
            ),
        ]

    async def invoke(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        handlers = {
            "triage_issues": self._triage_issues,
            "record_investigation": self._record_investigation,
            "issue_notes": self._issue_notes,
            "radar_status": self._radar_status,
        }
        handler = handlers.get(tool_name)
        if handler is None:
            return ToolResult(
                success=False,
                error=f"Unknown tool: {tool_name!r}",
                recovery_hints=[f"This provider exposes: {', '.join(sorted(handlers))}."],
            )
        try:
            return await handler(dict(arguments or {}))
        except Exception as exc:  # a tool fails legibly; it never takes the turn down
            logger.warning("issue-radar %s failed", tool_name, exc_info=True)
            return ToolResult(success=False, error=f"{tool_name} failed: {exc}")

    # ── triage_issues ───────────────────────────────────────────────────────

    async def _triage_issues(self, args: dict[str, Any]) -> ToolResult:
        try:
            repo = parse_repo_ref(str(args.get("repo") or ""))
        except ValueError as exc:
            return ToolResult(
                success=False,
                error=str(exc),
                recovery_hints=[
                    "Pass 'owner/repo' for GitHub or 'gitlab:group/project' for GitLab.",
                    "A bare 'group/project' is read as GitHub — GitLab needs its prefix.",
                ],
            )

        mode = str(args.get("label_source") or self._label_source)
        if mode not in LABEL_SOURCES:
            return ToolResult(
                success=False,
                error=f"Unknown label_source: {mode!r}",
                recovery_hints=[f"Use one of: {', '.join(LABEL_SOURCES)}."],
            )
        try:
            limit = int(args.get("limit") or self._max_issues)
        except (TypeError, ValueError):
            limit = self._max_issues
        limit = max(1, min(200, limit))

        try:
            raw_issues = await self._fetch_issues(repo, limit)
        except FileNotFoundError:
            binary = HOST_BINARIES[repo.host]
            return ToolResult(
                success=False,
                error=_MISSING[repo.host],
                recovery_hints=[f"Install the CLI, then `{binary} auth login`."],
            )
        except TrackerError as exc:
            return ToolResult(
                success=False,
                error=str(exc),
                recovery_hints=[
                    "Check the repository reference and that the CLI is signed in to that host.",
                    "A private repository needs a token with read access to its issues.",
                ],
            )

        issues = (
            issues_from_github(raw_issues)
            if repo.host == HOST_GITHUB
            else issues_from_gitlab(raw_issues)
        )
        if not issues:
            return ToolResult(
                success=True,
                output=f"{repo}: no open issues. Nothing to triage.",
                metadata={"repo": str(repo), "issues": 0},
            )

        # A failed label lookup is a degrade, not a failure: suggestions fall back to
        # canonical names and the report says the set was unavailable.
        known_labels = await self._fetch_labels(repo)

        triaged = triage(
            issues,
            known_labels,
            now=datetime.now(timezone.utc),
            stale_days=self._stale_days,
        )
        dropped = max(0, len(triaged) - limit)
        triaged = triaged[:limit]

        briefs = build_briefs(repo, triaged, known_labels, fence=fence_untrusted)
        source_label = mode
        if mode == "model":
            source_label = await self._suggest_with_model(triaged, briefs)

        sweep = {
            "repo": str(repo),
            "host": repo.host,
            "swept": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "label_source": source_label,
            "known_labels": known_labels,
            "stale_days": self._stale_days,
            "issues": [
                {
                    "number": item.number,
                    "title": item.issue.title,
                    "url": item.issue.url,
                    "score": item.score,
                    "reasons": item.reasons,
                    "has_labels": item.issue.labels,
                    "suggested": [
                        {"label": s.label, "why": s.why, "source": s.source}
                        for s in item.suggestions
                    ],
                    "model_note": item.model_note,
                }
                for item in triaged
            ],
        }
        sweep_path = self._sweeps.write(repo, sweep)

        report = render_sweep(
            repo,
            triaged,
            label_source=source_label,
            known_labels=known_labels,
            sweep_path=sweep_path,
            dropped=dropped,
        )
        if mode == "plan":
            report += (
                f"\n## Fan-out plan — {len(briefs)} isolated per-issue brief(s)\n\n"
                "Spawn ONE subagent per brief in `metadata.briefs`, heaviest score first. "
                "Give each subagent only its own brief, and have it report what it found "
                f'with `record_investigation(issue="{repo}#<number>", note=…, '
                "next_step=…, labels=[…])`.\n"
            )

        return ToolResult(
            success=True,
            output=report,
            metadata={
                "repo": str(repo),
                "host": repo.host,
                "label_source": source_label,
                "issues_triaged": len(triaged),
                "issues_dropped": dropped,
                "suggestions": sum(len(item.suggestions) for item in triaged),
                "known_labels": known_labels,
                "sweep_path": str(sweep_path),
                "queue": [
                    {
                        "number": item.number,
                        "score": item.score,
                        "suggested": [s.label for s in item.suggestions],
                        "reasons": item.reasons,
                    }
                    for item in triaged
                ],
                # The briefs ride the metadata in every mode, so the host CAN take the
                # subagent leg even when it asked for the model leg.
                "briefs": [
                    {
                        "number": brief.number,
                        "ref": brief.ref,
                        "score": brief.score,
                        "truncated": brief.truncated,
                        "prompt": brief.prompt,
                    }
                    for brief in briefs
                ],
            },
        )

    # ── The tracker edge ────────────────────────────────────────────────────

    async def _fetch_issues(self, repo: RepoRef, limit: int) -> Any:
        """The open-issue list for one repository. Half of this app's whole read surface.

        Fixed argv — never a shell string — and every interpolated value comes off a
        regex-validated ``RepoRef``, so no part of the reference can become a flag or a
        second command. Comments are deliberately NOT requested: they multiply the payload
        by an unbounded factor, and staleness is already answered by the update stamp.
        """
        if repo.host == HOST_GITHUB:
            argv = [
                "gh", "issue", "list",
                "--repo", repo.path,
                "--state", "open",
                "--limit", str(limit),
                "--json", "number,title,body,labels,author,createdAt,updatedAt,url,assignees",
            ]
        else:
            argv = [
                "glab", "issue", "list",
                "--repo", repo.path,
                "--per-page", str(limit),
                "--output", "json",
            ]
        return await self._run_json(argv, repo)

    async def _fetch_labels(self, repo: RepoRef) -> list[str] | None:
        """The repository's own label set, or None when it cannot be read.

        This is what makes a suggestion actionable rather than a rename request, so it is
        worth a second CLI call — but it is not worth failing a sweep over.
        """
        if repo.host == HOST_GITHUB:
            argv = ["gh", "label", "list", "--repo", repo.path, "--limit", "200",
                    "--json", "name"]
        else:
            argv = ["glab", "label", "list", "--repo", repo.path, "--output", "json"]
        try:
            payload = await self._run_json(argv, repo)
        except (OSError, TrackerError) as exc:
            logger.info("label list unavailable for %s: %s", repo, exc)
            return None
        labels = (
            labels_from_github(payload)
            if repo.host == HOST_GITHUB
            else labels_from_gitlab(payload)
        )
        return labels or None

    async def _run_json(self, argv: list[str], repo: RepoRef) -> Any:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise TrackerError(
                f"`{' '.join(argv[:3])}` timed out after {self._timeout}s"
            ) from exc
        if proc.returncode != 0:
            detail = (err or b"").decode("utf-8", "replace").strip()[:600]
            raise TrackerError(
                f"`{' '.join(argv[:3])}` failed for {repo} (exit {proc.returncode}): {detail}"
            )
        text = (out or b"").decode("utf-8", "replace").strip()
        if not text:
            return []
        try:
            return json.loads(text)
        except ValueError as exc:
            raise TrackerError(
                f"`{' '.join(argv[:3])}` returned output that is not JSON: {text[:200]!r}"
            ) from exc

    # ── The model leg of the suggestions ────────────────────────────────────

    async def _suggest_with_model(
        self, triaged: list[Any], briefs: list[IssueBrief]
    ) -> str:
        """One model call per issue, bounded concurrency, a fresh provider each time.

        Degrades rather than fails: with no model provider registered the sweep still
        returns its rule-based suggestions and the label says so, because a sweep that
        reads as "nothing to suggest" when it never asked is worse than an error.
        """
        entry = self._resolve_entry()
        if entry is None:
            return "rules (no model provider is registered — bind one in Settings → Models)"
        if not briefs:
            return f"rules + model:{entry}"

        by_number = {item.number: item for item in triaged}
        sem = asyncio.Semaphore(self._concurrency)

        async def one(brief: IssueBrief) -> tuple[list[Suggestion], str]:
            async with sem:
                item = by_number.get(brief.number)
                have = list(item.issue.labels) if item is not None else []
                return await self._label_one_issue(entry, brief, have)

        results = await asyncio.gather(*(one(b) for b in briefs), return_exceptions=True)
        failed = 0
        for brief, result in zip(briefs, results, strict=True):
            item = by_number.get(brief.number)
            if item is None:
                continue
            if isinstance(result, BaseException):
                failed += 1
                logger.warning("per-issue label call failed for %s: %s", brief.ref, result)
                item.model_note = (
                    f"the model pass for this issue failed: {type(result).__name__}: {result}"
                )[:400]
                continue
            suggestions, leftover = result
            existing = {s.label.lower() for s in item.suggestions}
            item.suggestions.extend(s for s in suggestions if s.label.lower() not in existing)
            if leftover:
                item.model_note = leftover
        label = f"rules + model:{entry} ({len(briefs)} isolated per-issue calls"
        label += f", {failed} failed)" if failed else ")"
        return label

    def _resolve_entry(self) -> str | None:
        """Which registered model entry the per-issue label calls run on.

        A configured ``model_entry`` wins. Otherwise the first registered entry that
        advertises chat — the registry is populated from the user's own Settings → Models,
        so this stays the user's choice of provider, never a vendor this app picked.
        """
        try:
            from personalclaw.sdk.model import Capability, get_default_registry

            registry = get_default_registry()
            entries = registry.list_entries()
            if self._model_entry:
                names = {entry.name for entry in entries}
                if self._model_entry in names:
                    return self._model_entry
                logger.warning(
                    "configured model_entry %r is not registered (have: %s) — falling back",
                    self._model_entry,
                    sorted(names),
                )
            for entry in entries:
                declared = entry.declared_capabilities
                if not declared or Capability.CHAT in declared:
                    return entry.name
        except Exception:  # noqa: BLE001 — no model is a degrade, never a crash
            logger.debug("model registry unavailable", exc_info=True)
        return None

    async def _label_one_issue(
        self, entry: str, brief: IssueBrief, have: list[str]
    ) -> tuple[list[Suggestion], str]:
        """Build a provider, label ONE issue, tear it down. Nothing crosses between issues."""
        from personalclaw.sdk.model import EVENT_TEXT_CHUNK, get_default_registry

        provider = get_default_registry().build(entry)
        chunks: list[str] = []
        try:
            await provider.start()
            async for event in provider.stream(brief.prompt):
                if event.kind == EVENT_TEXT_CHUNK and event.text:
                    chunks.append(event.text)
        finally:
            try:
                await provider.shutdown()
            except Exception:  # noqa: BLE001 — a failed teardown must not lose the answer
                logger.debug("teardown failed for the %s label call", brief.ref, exc_info=True)
        return parse_model_suggestions("".join(chunks), brief, have)

    # ── record_investigation / issue_notes / radar_status ────────────────────

    async def _record_investigation(self, args: dict[str, Any]) -> ToolResult:
        try:
            ref = parse_issue_ref(str(args.get("issue") or ""))
        except ValueError as exc:
            return ToolResult(
                success=False,
                error=str(exc),
                recovery_hints=["Pass 'owner/repo#123', 'gitlab:group/project#12', or a URL."],
            )
        body = str(args.get("note") or "").strip()
        if not body:
            return ToolResult(
                success=False,
                error="note is required",
                recovery_hints=["Say what the investigation found, even if it is 'cannot repro'."],
            )
        labels = [str(name) for name in list(args.get("labels") or []) if str(name).strip()]
        note = Note(note=body, next_step=str(args.get("next_step") or ""), labels=labels)
        self._notes.append(ref, [note])
        path = self._notes.path_for(ref)
        return ToolResult(
            success=True,
            output=f"Recorded an investigation note on {ref} at {path}.",
            metadata={
                "issue": str(ref),
                "notes_path": str(path),
                "labels": [name for name in note.to_line()["labels"]],
            },
        )

    async def _issue_notes(self, args: dict[str, Any]) -> ToolResult:
        raw = str(args.get("issue") or "").strip()
        if not raw:
            issues = self._notes.investigated()
            body = "\n".join(f"- {i}" for i in issues) or (
                "No issue has been investigated on this machine yet."
            )
            return ToolResult(success=True, output=body, metadata={"issues": issues})
        try:
            ref = parse_issue_ref(raw)
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))
        rows = self._notes.read(ref)
        if not rows:
            return ToolResult(
                success=True,
                output=f"No investigation notes kept locally for {ref}.",
                metadata={"issue": str(ref), "notes": 0},
            )
        lines = [f"{len(rows)} note(s) on {ref}:", ""]
        for row in rows:
            lines.append(f"### {row.get('recorded')}")
            lines += ["", str(row.get("note") or ""), ""]
            if row.get("next_step"):
                lines += [f"Next: {row['next_step']}", ""]
            if row.get("labels"):
                lines += ["Labels supported: " + ", ".join(f"`{n}`" for n in row["labels"]), ""]
        return ToolResult(
            success=True,
            output="\n".join(lines),
            metadata={
                "issue": str(ref),
                "notes": len(rows),
                "notes_path": str(self._notes.path_for(ref)),
            },
        )

    async def _radar_status(self, args: dict[str, Any]) -> ToolResult:
        raw = str(args.get("repo") or "").strip()
        if not raw:
            repos = self._sweeps.swept()
            body = "\n".join(f"- {r}" for r in repos) or (
                "No repository has been swept on this machine yet."
            )
            return ToolResult(success=True, output=body, metadata={"repos": repos})
        try:
            repo = parse_repo_ref(raw)
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))
        sweep = self._sweeps.read(repo)
        if sweep is None:
            return ToolResult(
                success=True,
                output=f"{repo} has not been swept on this machine — run triage_issues first.",
                metadata={"repo": str(repo), "issues": 0},
            )
        rows = list(sweep.get("issues") or [])
        lines = [
            f"# Last sweep of {repo}",
            "",
            f"Swept {sweep.get('swept')} · labels from {sweep.get('label_source')} · "
            f"{len(rows)} issue(s).",
            "",
            "| Issue | Score | Suggested | Why |",
            "|---|---|---|---|",
        ]
        for row in rows:
            suggested = ", ".join(f"`{s.get('label')}`" for s in row.get("suggested") or []) or "—"
            why = "; ".join(row.get("reasons") or []) or "—"
            title = str(row.get("title") or "").replace("|", r"\|")[:70]
            lines.append(f"| #{row.get('number')} {title} | {row.get('score')} | {suggested} | {why} |")
        return ToolResult(
            success=True,
            output="\n".join(lines),
            metadata={
                "repo": str(repo),
                "issues": len(rows),
                "swept": sweep.get("swept"),
                "sweep_path": str(self._sweeps.path_for(repo)),
            },
        )


def create_provider(config: dict[str, Any] | None = None) -> IssueRadarProvider:
    """Manifest factory — core calls this with this app's saved settings."""
    return IssueRadarProvider(config)
