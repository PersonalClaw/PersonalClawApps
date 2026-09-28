"""The watched-source-github trigger source.

Watches GitHub repositories and emits typed events, ``new_release`` and ``new_issue``, as
``SourceEvent``s through ``TriggerSourceProvider`` from ``personalclaw.sdk.trigger_source``.
Core namespaces them (``app:watched-source-github:new_release``) and matches them against
``kind: event`` triggers, which is why the app keeps this name: a trigger bound to one of these
events names the app.

- **Push contract, self-owned loop.** Core never polls a source. ``start`` spawns this
  provider's own ``asyncio`` task and returns immediately (blocking there would stall app
  enable), and ``stop`` cancels it and is idempotent.
- **First observation plants a high-water mark.** The first poll of a repository records what
  exists and emits nothing, so a freshly watched repository never floods its history as "new".
- **Declared events.** ``events`` is the vocabulary a user picks from when authoring a
  trigger, and only names declared there are emitted.
- **Every request goes through the egress guard.** GitHub is reached with
  ``personalclaw.sdk.net.fetch`` under the connector policy plus the operator's Security →
  Network egress settings: public hosts only, the resolved address pinned, every redirect
  re-checked, and each request recorded in the security event log. This app opens no socket of
  its own. A GitHub Enterprise server on a private network is reachable only once its host is
  allowed there.
- **Degrade to silence.** A network, API or egress failure logs and skips the round: a watcher
  must never take the trigger bus down with it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from personalclaw.sdk.trigger_source import SourceEvent, TriggerSourceProvider
from watched_source_github_repos import parse_repos

logger = logging.getLogger("watched_source_github")

DEFAULT_API_BASE = "https://api.github.com"
DEFAULT_POLL_SECS = 300
MIN_POLL_SECS = 60


class GithubApiError(Exception):
    """GitHub answered with a status this source cannot use."""


class WatchedSourceGithubProvider(TriggerSourceProvider):
    name = "watched-source-github"
    display_name = "GitHub Repo Watcher"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = dict(config or {})
        self._timeout = int(self._config.get("timeout_secs", 20))
        self._token = str(self._config.get("token", "") or "").strip()
        self._api_base = str(
            self._config.get("api_base", DEFAULT_API_BASE) or DEFAULT_API_BASE
        ).rstrip("/")
        self._poll_secs = max(
            MIN_POLL_SECS,
            int(self._config.get("poll_interval_secs", DEFAULT_POLL_SECS)),
        )
        self._repos, skipped = parse_repos(self._config.get("repos", ""))
        for entry in skipped:
            logger.warning("not watching %r: a repository is written owner/name", entry)
        self._task: asyncio.Task[None] | None = None
        # Per-repo high-water marks: latest seen release id / issue timestamp.
        self._seen_release: dict[str, str] = {}
        self._seen_issue_at: dict[str, str] = {}

    # ── Contract ──────────────────────────────────────────────────────────

    @property
    def events(self) -> tuple[str, ...]:
        return ("new_release", "new_issue")

    async def start(self, emit: Callable[[SourceEvent], None]) -> None:
        """Spawn the watch loop and return — never block the enable path."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.get_running_loop().create_task(self._watch(emit))

    async def stop(self) -> None:
        """Cancel the loop and release it. Idempotent."""
        task, self._task = self._task, None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # ── Watch loop ────────────────────────────────────────────────────────

    async def _watch(self, emit: Callable[[SourceEvent], None]) -> None:
        while True:
            for repo in self._repos:
                try:
                    for event in await self._poll_repo(repo):
                        emit(event)
                except Exception as exc:  # noqa: BLE001 — degrade to silence, never die
                    logger.warning("watch %s failed (%s) — skipping round", repo, exc)
            await asyncio.sleep(self._poll_secs)

    async def _poll_repo(self, repo: str) -> list[SourceEvent]:
        """One poll of one repository: its latest release, then its new issues."""
        events: list[SourceEvent] = []
        events.extend(await self._poll_release(repo))
        events.extend(await self._poll_issues(repo))
        return events

    async def _poll_release(self, repo: str) -> list[SourceEvent]:
        data = await self._get(f"/repos/{repo}/releases/latest")
        if not isinstance(data, dict) or "id" not in data:
            return []
        release_id = str(data["id"])
        previous, self._seen_release[repo] = self._seen_release.get(repo), release_id
        if previous is None or previous == release_id:
            return []  # first observation, or nothing new
        tag = str(data.get("tag_name", "") or "")
        title = str(data.get("name", "") or tag)
        return [
            SourceEvent(
                event="new_release",
                key=release_id,
                text=f"{repo} released {tag}: {title}",
                meta={"repo": repo, "tag": tag, "url": str(data.get("html_url", ""))},
            )
        ]

    async def _poll_issues(self, repo: str) -> list[SourceEvent]:
        since = self._seen_issue_at.get(repo)
        query = "?state=open&sort=created&direction=desc&per_page=20"
        if since:
            query += f"&since={since}"
        data = await self._get(f"/repos/{repo}/issues{query}")
        if not isinstance(data, list):
            return []
        newest = since or ""
        events: list[SourceEvent] = []
        for issue in data:
            if not isinstance(issue, dict) or "pull_request" in issue:
                continue  # the issues API interleaves PRs; watch issues only
            created = str(issue.get("created_at", "") or "")
            if created > newest:
                newest = created
            if since is None or created <= since:
                continue  # first observation plants the mark; older rows skipped
            number = str(issue.get("number", "") or "")
            events.append(
                SourceEvent(
                    event="new_issue",
                    key=f"{repo}#{number}",
                    text=f"{repo}#{number}: {str(issue.get('title', '') or '')}",
                    meta={
                        "repo": repo,
                        "number": number,
                        "author": str((issue.get("user") or {}).get("login", "")),
                        "url": str(issue.get("html_url", "") or ""),
                    },
                )
            )
        if newest:
            self._seen_issue_at[repo] = newest
        return events

    async def _get(self, path: str) -> Any:
        """One GET through the egress guard: the parsed JSON, or ``None`` for a 404.

        A 404 is a normal state (a repository with no release yet). Any other status that is
        not 200 raises, and the watch loop skips the round.
        """
        from personalclaw.sdk.net import CONNECTOR, egress_policy_for, fetch

        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "personalclaw-watched-source-github",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        policy = egress_policy_for(CONNECTOR).with_overrides(
            timeout_s=float(self._timeout)
        )
        response = await fetch(
            f"{self._api_base}{path}", policy=policy, method="GET", headers=headers
        )
        if response.status == 404:
            return None
        if response.status != 200:
            raise GithubApiError(f"GitHub answered HTTP {response.status}")
        return json.loads(response.text)


def create_provider(
    config: dict[str, Any] | None = None,
) -> WatchedSourceGithubProvider:
    """Manifest factory — core calls this with this app's saved settings."""
    return WatchedSourceGithubProvider(config)
