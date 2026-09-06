"""The `code-review` tool provider — deep PR review, one isolated review per changed file.

Three tools on the agent tool layer:

* ``review_pr`` — fetch a PR's diff with ``gh``, weight every changed file by blast
  radius, then fan out ONE isolated review per file and keep the findings locally.
* ``record_finding`` — the write end of the fan-out: whoever reviewed a file (a spawned
  subagent, or the host agent itself) appends its verdict to the same local log.
* ``review_findings`` — read a PR's findings back off this machine.

**How the fan-out is actually implemented, precisely.** Core has a real subagent
primitive (``SubagentManager``), but the SDK only ever hands it to an app through
``GatewayServices.subagent_mgr``, and a ``ToolProvider`` is constructed with its settings
dict and nothing else — no services object, no session. So this app cannot spawn a core
subagent, and does not pretend to. It gives the fan-out two honest legs instead:

1. ``fanout="model"`` (default) — N bounded-concurrency model calls through
   ``personalclaw.sdk.model``: a FRESH provider instance per file, built from the
   registry, given only that file's diff, torn down after. Context isolation is total;
   process isolation is not, and the report says ``model`` rather than ``subagent`` so
   nobody reads more into it than that.
2. ``fanout="plan"`` — no model call at all. ``review_pr`` returns the per-file briefs
   and the host agent spawns one real subagent per brief with its own spawn surface, each
   reporting back through ``record_finding``. This is the leg that gets genuinely isolated
   per-file subagents, and it is why ``record_finding`` exists as a separate tool.

``fanout="static"`` runs the deterministic pass only. That pass runs in all three modes —
it needs no model, so a review is never empty for want of one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from typing import Any

from personalclaw.sdk.security import fence_untrusted
from personalclaw.sdk.tool import RiskLevel, ToolDefinition, ToolProvider, ToolResult

from review import (
    Finding,
    FindingsLog,
    PrRef,
    ReviewBrief,
    blast_radius,
    build_briefs,
    parse_diff,
    parse_pr_ref,
    render_report,
    static_findings,
)

logger = logging.getLogger("code_review")

FANOUT_MODES = ("model", "plan", "static")
VALID_SEVERITIES = ("high", "medium", "low")

_GH_MISSING = (
    "The `gh` CLI is not on PATH. Install GitHub CLI (https://cli.github.com) and run "
    "`gh auth login` — this app has no network access of its own and reads GitHub only "
    "through `gh`."
)


class GhError(RuntimeError):
    """`gh` was reachable but refused the request (not authenticated, no such PR, …)."""


class CodeReviewProvider(ToolProvider):
    """Deep PR review with a per-file, blast-radius-weighted fan-out."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = dict(config or {})
        self._timeout = max(5, int(self._config.get("timeout_secs", 20) or 20))
        mode = str(self._config.get("fanout") or "model")
        self._fanout = mode if mode in FANOUT_MODES else "model"
        self._concurrency = max(1, min(8, int(self._config.get("concurrency", 3) or 3)))
        self._max_files = max(1, min(200, int(self._config.get("max_files", 40) or 40)))
        self._model_entry = str(self._config.get("model_entry") or "").strip()
        self._log_impl: FindingsLog | None = None

    @property
    def _log(self) -> FindingsLog:
        """The findings log, opened on first use.

        Lazy on purpose: constructing a provider is what core does to READ its tool list
        (Settings → Tools, the manifest round-trip), and that must not mkdir under the
        user's home. The directory appears the first time a review is actually run.
        """
        if self._log_impl is None:
            self._log_impl = FindingsLog()
        return self._log_impl

    # ── Identity ────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "code-review"

    @property
    def display_name(self) -> str:
        return "Code Review"

    def info(self) -> dict[str, Any]:
        return {
            "fanout": self._fanout,
            "concurrency": self._concurrency,
            "max_files": self._max_files,
            "gh_available": shutil.which("gh") is not None,
        }

    # ── Tool surface ────────────────────────────────────────────────────────

    async def list_tools(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name="review_pr",
                description=(
                    "Deep-review a GitHub pull request. Fetches the diff with the local `gh` "
                    "CLI, scores every changed file by blast radius (churn, path criticality, "
                    "fan-in within the changed set, add/delete/rename), then reviews each file "
                    "in ISOLATION — one review per file, heaviest first, each seeing only its "
                    "own diff. Findings are appended to a local JSONL log; nothing is ever "
                    "posted back to the PR. Read-only against GitHub."
                ),
                provider=self.name,
                parameters={
                    "type": "object",
                    "properties": {
                        "pr": {
                            "type": "string",
                            "description": "The PR: 'owner/repo#123' or its github.com/.../pull/123 URL.",
                        },
                        "fanout": {
                            "type": "string",
                            "enum": list(FANOUT_MODES),
                            "description": (
                                "'model' (default): review each file with its own model call. "
                                "'plan': return one review brief per file and spawn your own "
                                "subagent per brief, each reporting via record_finding. "
                                "'static': the deterministic pass only, no model."
                            ),
                        },
                        "max_files": {
                            "type": "integer",
                            "description": "Cap the fan-out to the N heaviest files (default 40).",
                        },
                    },
                    "required": ["pr"],
                },
                requires_approval=False,
                risk_level=RiskLevel.SAFE,
                max_output=60_000,
            ),
            ToolDefinition(
                name="record_finding",
                description=(
                    "Append one review finding for one file of one PR to the local findings "
                    "log. This is how a per-file subagent reports back after review_pr was "
                    "called with fanout='plan'."
                ),
                provider=self.name,
                parameters={
                    "type": "object",
                    "properties": {
                        "pr": {"type": "string", "description": "The PR the finding belongs to."},
                        "file": {"type": "string", "description": "Path of the reviewed file."},
                        "severity": {"type": "string", "enum": list(VALID_SEVERITIES)},
                        "summary": {"type": "string", "description": "One sentence: what is wrong."},
                        "evidence": {
                            "type": "string",
                            "description": "The line or snippet the finding points at.",
                        },
                    },
                    "required": ["pr", "file", "severity", "summary"],
                },
                requires_approval=False,
                risk_level=RiskLevel.SAFE,
            ),
            ToolDefinition(
                name="review_findings",
                description=(
                    "Read back the locally-kept findings for a PR (or list every PR reviewed "
                    "on this machine when `pr` is omitted)."
                ),
                provider=self.name,
                parameters={
                    "type": "object",
                    "properties": {
                        "pr": {"type": "string", "description": "The PR to read. Omit to list all."},
                        "severity": {
                            "type": "string",
                            "enum": list(VALID_SEVERITIES),
                            "description": "Only findings at this severity.",
                        },
                    },
                },
                requires_approval=False,
                risk_level=RiskLevel.SAFE,
                max_output=60_000,
            ),
        ]

    async def invoke(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        if tool_name == "review_pr":
            return await self._review_pr(arguments)
        if tool_name == "record_finding":
            return await self._record_finding(arguments)
        if tool_name == "review_findings":
            return await self._review_findings(arguments)
        return ToolResult(
            success=False,
            error=f"Unknown tool: {tool_name!r}",
            recovery_hints=[
                "This provider exposes: review_pr, record_finding, review_findings.",
            ],
        )

    # ── review_pr ───────────────────────────────────────────────────────────

    async def _review_pr(self, args: dict[str, Any]) -> ToolResult:
        try:
            ref = parse_pr_ref(str(args.get("pr") or ""))
        except ValueError as exc:
            return ToolResult(
                success=False, error=str(exc),
                recovery_hints=["Pass 'owner/repo#123' or the pull-request URL."],
            )

        mode = str(args.get("fanout") or self._fanout)
        if mode not in FANOUT_MODES:
            return ToolResult(
                success=False, error=f"Unknown fanout mode: {mode!r}",
                recovery_hints=[f"Use one of: {', '.join(FANOUT_MODES)}."],
            )
        try:
            cap = int(args.get("max_files") or self._max_files)
        except (TypeError, ValueError):
            cap = self._max_files
        cap = max(1, min(200, cap))

        try:
            diff = await self._gh_diff(ref)
        except FileNotFoundError:
            return ToolResult(success=False, error=_GH_MISSING, recovery_hints=[
                "Install the GitHub CLI, then `gh auth login`.",
                "Verify with `gh pr diff <number> --repo owner/repo`.",
            ])
        except GhError as exc:
            return ToolResult(success=False, error=str(exc), recovery_hints=[
                "Check the PR reference and that `gh auth status` is signed in to that host.",
                "A private repo needs a token with `repo` scope.",
            ])

        files = blast_radius(parse_diff(diff))
        if not files:
            return ToolResult(
                success=False,
                error=f"{ref}: `gh` returned a diff with no changed files.",
                recovery_hints=[
                    "Confirm the PR has commits: `gh pr view <number> --repo owner/repo`.",
                ],
            )
        dropped = max(0, len(files) - cap)
        files = files[:cap]

        findings: list[Finding] = static_findings(files)
        briefs = build_briefs(files, fence=fence_untrusted)
        fanout_label = mode
        if mode == "model":
            model_findings, fanout_label = await self._fanout_model(briefs)
            findings.extend(model_findings)

        written = self._log.append(ref, findings)
        report = render_report(
            ref, files, findings, fanout=fanout_label, log_path=self._log.path_for(ref),
        )
        if dropped:
            report += (f"\n\n{dropped} lower-weight file(s) were left out of the fan-out by "
                       f"max_files={cap}.")
        if mode == "plan":
            report += (
                f"\n\n## Fan-out plan — {len(briefs)} isolated per-file review(s)\n\n"
                "Spawn ONE subagent per brief in `metadata.briefs`, heaviest first. Give each "
                "subagent only its own brief, and have it report every defect with "
                f'`record_finding(pr="{ref}", file=…, severity=…, summary=…, evidence=…)`.'
            )

        return ToolResult(
            success=True,
            output=report,
            metadata={
                "pr": str(ref),
                "fanout": fanout_label,
                "files_reviewed": len(files),
                "files_dropped": dropped,
                "findings": len(findings),
                "findings_written": written,
                "findings_path": str(self._log.path_for(ref)),
                "weights": {f.path: f.weight for f in files},
                # The briefs ride the metadata in every mode, so the host CAN take the
                # subagent leg even when it asked for the model leg.
                "briefs": [
                    {"file": b.path, "weight": b.weight, "depth": b.depth,
                     "truncated": b.truncated, "prompt": b.prompt}
                    for b in briefs
                ],
            },
        )

    # ── The `gh` edge ───────────────────────────────────────────────────────

    async def _gh_diff(self, ref: PrRef) -> str:
        """``gh pr diff`` for one PR. The whole network surface of this app.

        Fixed argv — never a shell string, and every interpolated value comes off a
        regex-validated ``PrRef``, so no part of the reference can become a flag or a
        second command (ARCC SAX-04: validate at the boundary, then pass structurally).
        """
        argv = [
            "gh", "pr", "diff", str(ref.number),
            "--repo", f"{ref.owner}/{ref.repo}",
            "--patch",
        ]
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
            raise GhError(f"`gh pr diff` timed out after {self._timeout}s") from exc
        if proc.returncode != 0:
            detail = (err or b"").decode("utf-8", "replace").strip()[:600]
            raise GhError(f"`gh pr diff` failed for {ref} (exit {proc.returncode}): {detail}")
        return (out or b"").decode("utf-8", "replace")

    # ── The model leg of the fan-out ────────────────────────────────────────

    async def _fanout_model(self, briefs: list[ReviewBrief]) -> tuple[list[Finding], str]:
        """One model call per brief, bounded concurrency, a fresh provider each time.

        Degrades rather than fails: with no model provider registered the review still
        returns its static findings and the label says so, because a review that reads as
        "no findings" when it never ran is the one outcome worse than an error.
        """
        entry = self._resolve_entry()
        if entry is None:
            return [], "static (no model provider is registered — bind one in Settings → Models)"
        if not briefs:
            return [], f"model:{entry}"

        sem = asyncio.Semaphore(self._concurrency)

        async def one(brief: ReviewBrief) -> list[Finding]:
            async with sem:
                return await self._review_one_file(entry, brief)

        results = await asyncio.gather(*(one(b) for b in briefs), return_exceptions=True)
        findings: list[Finding] = []
        failed = 0
        for brief, result in zip(briefs, results, strict=True):
            if isinstance(result, BaseException):
                failed += 1
                logger.warning("per-file review failed for %s: %s", brief.path, result)
                findings.append(Finding(
                    file=brief.path, severity="low", weight=brief.weight, source="model",
                    summary="This file was NOT reviewed — its isolated review call failed",
                    evidence=f"{type(result).__name__}: {result}"[:200],
                ))
                continue
            findings.extend(result)
        label = f"model:{entry} ({len(briefs)} isolated per-file calls"
        label += f", {failed} failed)" if failed else ")"
        return findings, label

    def _resolve_entry(self) -> str | None:
        """Which registered model entry the per-file reviews run on.

        A configured ``model_entry`` wins. Otherwise the first registered entry that
        advertises chat — the registry is populated from the user's own Settings → Models,
        so this stays the user's choice of provider, never a vendor this app picked.
        """
        try:
            from personalclaw.sdk.model import Capability, get_default_registry

            registry = get_default_registry()
            entries = registry.list_entries()
            if self._model_entry:
                names = {e.name for e in entries}
                if self._model_entry in names:
                    return self._model_entry
                logger.warning(
                    "configured model_entry %r is not registered (have: %s) — falling back",
                    self._model_entry, sorted(names),
                )
            for entry in entries:
                if not entry.declared_capabilities or Capability.CHAT in entry.declared_capabilities:
                    return entry.name
        except Exception:  # noqa: BLE001 — no model is a degrade, never a crash
            logger.debug("model registry unavailable", exc_info=True)
        return None

    async def _review_one_file(self, entry: str, brief: ReviewBrief) -> list[Finding]:
        """Build a provider, review ONE file, tear it down. Nothing crosses between files."""
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
            except Exception:  # noqa: BLE001 — a failed teardown must not lose the findings
                logger.debug("teardown failed for the %s review", brief.path, exc_info=True)
        return parse_model_findings("".join(chunks), brief)

    # ── record_finding / review_findings ────────────────────────────────────

    async def _record_finding(self, args: dict[str, Any]) -> ToolResult:
        try:
            ref = parse_pr_ref(str(args.get("pr") or ""))
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))
        path = str(args.get("file") or "").strip()
        summary = str(args.get("summary") or "").strip()
        severity = str(args.get("severity") or "").strip().lower()
        if not path or not summary:
            return ToolResult(
                success=False, error="file and summary are both required",
                recovery_hints=["Name the reviewed file and say in one sentence what is wrong."],
            )
        if severity not in VALID_SEVERITIES:
            return ToolResult(
                success=False,
                error=f"severity must be one of {VALID_SEVERITIES}, got {severity!r}",
            )
        finding = Finding(
            file=path, severity=severity, summary=summary,
            evidence=str(args.get("evidence") or ""), source="subagent",
        )
        self._log.append(ref, [finding])
        return ToolResult(
            success=True,
            output=f"Recorded {severity} finding on {path} for {ref}.",
            metadata={"pr": str(ref), "findings_path": str(self._log.path_for(ref))},
        )

    async def _review_findings(self, args: dict[str, Any]) -> ToolResult:
        raw = str(args.get("pr") or "").strip()
        if not raw:
            prs = self._log.reviewed_prs()
            body = "\n".join(f"- {p}" for p in prs) or "No PR has been reviewed on this machine yet."
            return ToolResult(success=True, output=body, metadata={"prs": prs})
        try:
            ref = parse_pr_ref(raw)
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))
        rows = self._log.read(ref)
        wanted = str(args.get("severity") or "").strip().lower()
        if wanted in VALID_SEVERITIES:
            rows = [r for r in rows if r.get("severity") == wanted]
        if not rows:
            return ToolResult(
                success=True,
                output=f"No findings kept locally for {ref}.",
                metadata={"pr": str(ref), "findings": 0},
            )
        lines = [f"{len(rows)} finding(s) for {ref}:", ""]
        for r in rows:
            lines.append(f"- **{r.get('severity')}** `{r.get('file')}` — {r.get('summary')}")
            if r.get("evidence"):
                lines.append(f"  - evidence: `{r['evidence']}`")
        return ToolResult(
            success=True,
            output="\n".join(lines),
            metadata={"pr": str(ref), "findings": len(rows),
                      "findings_path": str(self._log.path_for(ref))},
        )


def parse_model_findings(text: str, brief: ReviewBrief) -> list[Finding]:
    """Read a per-file review's output into findings.

    The brief asks for one JSON object per line, but a model that answers in prose must not
    silently become "no findings" — so an unparseable non-empty answer is kept verbatim as
    one low finding rather than discarded. Module-level so the parser is testable without
    a provider.
    """
    findings: list[Finding] = []
    leftover: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip().strip("`").strip()
        if not line:
            continue
        if line.startswith("{") and line.endswith("}"):
            try:
                obj = json.loads(line)
            except ValueError:
                leftover.append(line)
                continue
            severity = str(obj.get("severity") or "low").lower()
            summary = str(obj.get("summary") or "").strip()
            if not summary:
                continue
            findings.append(Finding(
                file=brief.path, weight=brief.weight, source="model",
                severity=severity if severity in VALID_SEVERITIES else "low",
                summary=summary, evidence=str(obj.get("evidence") or ""),
            ))
        else:
            leftover.append(line)
    if not findings and leftover:
        findings.append(Finding(
            file=brief.path, weight=brief.weight, source="model", severity="low",
            summary="Unstructured review output kept verbatim rather than dropped",
            evidence=" ".join(leftover)[:400],
        ))
    return findings


def create_provider(config: dict[str, Any] | None = None) -> CodeReviewProvider:
    """Manifest factory — core calls this with this app's saved settings."""
    return CodeReviewProvider(config)
