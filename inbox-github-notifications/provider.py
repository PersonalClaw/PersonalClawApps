"""The inbox-github-notifications inbox provider.

Surfaces your GitHub **notifications** (review requests, mentions, CI failures, issue
activity) as PersonalClaw inbox items, via ``MessageSourceProvider`` from
``personalclaw.sdk.inbox``.

- **Read-only source.** GitHub notifications are not a conversation surface, so
  ``send_reply`` / ``add_reaction`` honestly return ``False`` instead of pretending. A source
  that can only read says so.
- **Checkpoint = high-water mark.** ``poll`` keys one checkpoint under ``"github"``
  (notifications belong to the account, not to a channel) holding the newest ``updated_at``
  seen, and the next poll passes it as ``?since=`` so GitHub does the filtering.
- **Every request goes through the egress guard.** GitHub is reached with
  ``personalclaw.sdk.net.fetch`` under the connector policy plus the operator's Security →
  Network egress settings: public hosts only, the resolved address pinned, every redirect
  re-checked, and each request recorded in the security event log. This app opens no socket of
  its own.
- **Degrade to empty.** No token, network down, an API error or an egress refusal: ``poll``
  logs a warning and returns no rows with the checkpoints unchanged. An inbox source must never
  take the inbox service down with it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from personalclaw.sdk.inbox import IncomingMessage, MessageSourceProvider

logger = logging.getLogger("inbox_github_notifications")

_CHECKPOINT_KEY = "github"
DEFAULT_API_BASE = "https://api.github.com"


class GithubApiError(Exception):
    """GitHub answered with a status this source cannot use."""


def _iso_to_epoch(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class InboxGithubNotificationsProvider(MessageSourceProvider):
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = dict(config or {})
        self._timeout = int(self._config.get("timeout_secs", 20))
        self._token = str(self._config.get("token", "") or "").strip()
        self._api_base = str(
            self._config.get("api_base", DEFAULT_API_BASE) or DEFAULT_API_BASE
        ).rstrip("/")

    # ── Identity ──────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        """The key this provider registers under."""
        return "inbox-github-notifications"

    @property
    def display_name(self) -> str:
        return "GitHub Notifications"

    @property
    def source_name(self) -> str:
        return "inbox-github-notifications"

    # ── Fetch ─────────────────────────────────────────────────────────────

    async def _fetch_notifications(self, since: str) -> list[dict[str, Any]]:
        """One GET /notifications through the egress guard. Raises on a refusal, a transport
        failure or a status that is not 200: ``poll`` is the degrade boundary, not this helper.
        """
        from personalclaw.sdk.net import CONNECTOR, egress_policy_for, fetch

        params = {"all": "false", "per_page": "50"}
        if since:
            params["since"] = since
        policy = egress_policy_for(CONNECTOR).with_overrides(
            timeout_s=float(self._timeout)
        )
        response = await fetch(
            f"{self._api_base}/notifications?{urlencode(params)}",
            policy=policy,
            method="GET",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "personalclaw-inbox-github-notifications",
            },
        )
        if response.status != 200:
            raise GithubApiError(f"GitHub answered HTTP {response.status}")
        payload = json.loads(response.text)
        return payload if isinstance(payload, list) else []

    @staticmethod
    def _to_row(thread: dict[str, Any]) -> IncomingMessage:
        repo = thread.get("repository") or {}
        subject = thread.get("subject") or {}
        repo_name = str(repo.get("full_name", "") or "unknown/unknown")
        reason = str(thread.get("reason", "") or "subscribed")
        subject_type = str(subject.get("type", "") or "Thread")
        title = str(subject.get("title", "") or "(no title)")
        updated_at = str(thread.get("updated_at", "") or "")
        # The subject URL tail (issue/PR number) is the closest thing a
        # notification thread has to a thread id.
        subject_url = str(subject.get("url", "") or "")
        thread_tail = subject_url.rsplit("/", 1)[-1] if subject_url else None
        return IncomingMessage(
            id=str(thread.get("id", "") or ""),
            channel_id=repo_name,
            channel_name=repo_name,
            thread_id=thread_tail,
            text=f"[{reason}] {subject_type}: {title}",
            sender_id=repo_name,
            sender_name=repo_name,
            timestamp=_iso_to_epoch(updated_at),
            is_dm=False,
            kind="mention" if reason == "mention" else "message",
        )

    # ── Contract ──────────────────────────────────────────────────────────

    async def poll(
        self, watched_channels: list[str], checkpoints: dict[str, str], user_id: str
    ) -> tuple[list[IncomingMessage], dict[str, str]]:
        """Fetch notifications newer than the checkpoint; never raise.

        ``watched_channels``, when non-empty, is a repo allow-list (``owner/name``); anything
        else is dropped client-side.
        """
        if not self._token:
            logger.warning(
                "github notifications: no token configured — returning empty"
            )
            return [], dict(checkpoints)
        since = str(checkpoints.get(_CHECKPOINT_KEY, "") or "")
        try:
            threads = await self._fetch_notifications(since)
        except (
            Exception
        ) as exc:  # noqa: BLE001 — the degrade boundary: never take the inbox down
            logger.warning(
                "github notifications: poll failed (%s) — returning empty", exc
            )
            return [], dict(checkpoints)

        rows: list[IncomingMessage] = []
        newest = since
        allowed = {c.strip() for c in watched_channels if c and c.strip()}
        for thread in threads:
            row = self._to_row(thread)
            updated_at = str(thread.get("updated_at", "") or "")
            if updated_at > newest:
                newest = updated_at
            if allowed and row.channel_id not in allowed:
                continue
            rows.append(row)

        new_checkpoints = dict(checkpoints)
        # First-ever successful poll with an empty result still plants a
        # high-water mark, so history never floods in on a later poll.
        new_checkpoints[_CHECKPOINT_KEY] = newest or _now_iso()
        return rows, new_checkpoints

    async def send_reply(
        self, channel_id: str, text: str, thread_ts: str | None = None
    ) -> bool:
        """Notifications are read-only; replying happens on the PR/issue itself."""
        return False

    async def add_reaction(self, channel_id: str, ts: str, emoji: str) -> bool:
        return False

    async def get_channel_history(
        self, channel_id: str, oldest: str, limit: int = 200
    ) -> list[dict[str, Any]]:
        """No replayable per-channel history behind the notifications API."""
        return []

    async def resolve_user_name(self, user_id: str) -> str:
        return user_id


def create_provider(
    config: dict[str, Any] | None = None,
) -> InboxGithubNotificationsProvider:
    """Manifest factory — core calls this with this app's saved settings."""
    return InboxGithubNotificationsProvider(config)
