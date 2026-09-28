"""Contract + behaviour tests for the inbox-github-notifications provider.

Contract: personalclaw.sdk.inbox:MessageSourceProvider

No network: behaviour tests stub ``_fetch_notifications`` and assert the mapping,
checkpointing, filtering and degrade-to-empty rules, and the egress tests replace the SDK's
guarded ``fetch`` so no socket opens.
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from personalclaw.sdk.net import FetchResponse
from provider import GithubApiError, InboxGithubNotificationsProvider, create_provider

_HERE = Path(__file__).resolve().parent

CONTRACT_METHODS = (
    "add_reaction",
    "get_channel_history",
    "poll",
    "resolve_user_name",
    "send_reply",
    "source_name",
)


def _thread(
    thread_id: str = "1001",
    repo: str = "acme/widgets",
    reason: str = "review_requested",
    title: str = "Fix the flux capacitor",
    updated_at: str = "2026-09-02T12:00:00Z",
) -> dict:
    return {
        "id": thread_id,
        "reason": reason,
        "updated_at": updated_at,
        "repository": {"full_name": repo},
        "subject": {
            "title": title,
            "type": "PullRequest",
            "url": f"https://api.github.example/repos/{repo}/pulls/42",
        },
    }


def _returns(value):
    """An async ``_fetch_notifications`` stub that answers every call with *value*."""

    async def _fetch(since: str):
        return value

    return _fetch


# ── Contract shape ────────────────────────────────────────────────────────


def test_factory_returns_the_provider() -> None:
    assert isinstance(create_provider({}), InboxGithubNotificationsProvider)


def test_factory_accepts_no_config() -> None:
    assert isinstance(create_provider(None), InboxGithubNotificationsProvider)


def test_nothing_abstract_is_left() -> None:
    assert not getattr(
        InboxGithubNotificationsProvider, "__abstractmethods__", frozenset()
    )


def test_registers_under_the_app_name() -> None:
    provider = create_provider({})
    assert provider.name == "inbox-github-notifications"
    assert provider.source_name == "inbox-github-notifications"


def test_every_contract_method_is_declared() -> None:
    for name in CONTRACT_METHODS:
        assert name in vars(
            InboxGithubNotificationsProvider
        ), f"{name} is not implemented"


def test_settings_reach_the_provider() -> None:
    assert create_provider({"timeout_secs": 5})._timeout == 5


# ── The manifest: what the Store discloses before install ─────────────────


def _manifest() -> dict:
    return json.loads((_HERE / "app.json").read_text(encoding="utf-8"))


def test_the_manifest_declares_its_network_reach() -> None:
    """Every poll calls GitHub, so the install consent must say the app uses the network."""
    assert _manifest()["permissions"]["network"] is True


def test_the_token_is_a_masked_setting() -> None:
    """Declared sensitive, so no route that shows settings ever returns it."""
    token = _manifest()["provider"]["settingsSchema"]["properties"]["token"]
    assert token["x-meta"]["sensitive"] is True
    # And its help says what is true of THIS app: notifications need an account.
    assert "anonymous" not in token["x-meta"]["help"].lower()


# ── Behaviour: poll mapping + checkpoints ─────────────────────────────────


def _poll(provider, watched=None, checkpoints=None):
    return asyncio.run(provider.poll(watched or [], checkpoints or {}, "owner"))


def test_no_token_returns_empty_and_keeps_checkpoints() -> None:
    rows, cps = _poll(create_provider({}), checkpoints={"github": "X"})
    assert rows == []
    assert cps == {"github": "X"}


def test_threads_map_to_inbox_rows() -> None:
    p = create_provider({"token": "t"})
    p._fetch_notifications = _returns([_thread()])
    rows, cps = _poll(p)
    (row,) = rows
    assert row.id == "1001"
    assert row.channel_id == "acme/widgets"
    assert row.thread_id == "42"
    assert row.text == "[review_requested] PullRequest: Fix the flux capacitor"
    assert row.kind == "message"
    assert row.timestamp > 0
    assert cps["github"] == "2026-09-02T12:00:00Z"


def test_mention_reason_maps_to_mention_kind() -> None:
    p = create_provider({"token": "t"})
    p._fetch_notifications = _returns([_thread(reason="mention")])
    rows, _ = _poll(p)
    assert rows[0].kind == "mention"


def test_checkpoint_is_the_newest_updated_at() -> None:
    p = create_provider({"token": "t"})
    p._fetch_notifications = _returns(
        [
            _thread(thread_id="1", updated_at="2026-09-02T10:00:00Z"),
            _thread(thread_id="2", updated_at="2026-09-02T14:00:00Z"),
            _thread(thread_id="3", updated_at="2026-09-02T12:00:00Z"),
        ]
    )
    _, cps = _poll(p)
    assert cps["github"] == "2026-09-02T14:00:00Z"


def test_checkpoint_is_passed_through_as_since() -> None:
    seen = {}
    p = create_provider({"token": "t"})

    async def fetch(since):
        seen["since"] = since
        return []

    p._fetch_notifications = fetch
    _poll(p, checkpoints={"github": "2026-09-01T00:00:00Z"})
    assert seen["since"] == "2026-09-01T00:00:00Z"


def test_watched_channels_filter_by_repo() -> None:
    p = create_provider({"token": "t"})
    p._fetch_notifications = _returns(
        [
            _thread(thread_id="1", repo="acme/widgets"),
            _thread(thread_id="2", repo="other/repo"),
        ]
    )
    rows, _ = _poll(p, watched=["acme/widgets"])
    assert [r.id for r in rows] == ["1"]


def test_filtered_threads_still_advance_the_checkpoint() -> None:
    """The high-water mark tracks what was SEEN, not what was kept — otherwise
    a filtered-out newest thread would be re-fetched forever."""
    p = create_provider({"token": "t"})
    p._fetch_notifications = _returns(
        [_thread(thread_id="1", repo="other/repo", updated_at="2026-09-02T15:00:00Z")]
    )
    rows, cps = _poll(p, watched=["acme/widgets"])
    assert rows == []
    assert cps["github"] == "2026-09-02T15:00:00Z"


def test_empty_first_poll_plants_a_high_water_mark() -> None:
    p = create_provider({"token": "t"})
    p._fetch_notifications = _returns([])
    _, cps = _poll(p)
    assert cps["github"]  # planted, so history never floods later


def test_api_failure_degrades_to_empty() -> None:
    p = create_provider({"token": "t"})

    async def boom(since):
        raise OSError("down")

    p._fetch_notifications = boom
    rows, cps = _poll(p, checkpoints={"github": "keep-me"})
    assert rows == []
    assert cps == {"github": "keep-me"}


def test_read_only_surface_says_so() -> None:
    p = create_provider({"token": "t"})
    assert asyncio.run(p.send_reply("c", "text")) is False
    assert asyncio.run(p.add_reaction("c", "ts", "eyes")) is False
    assert asyncio.run(p.get_channel_history("c", "0")) == []
    assert asyncio.run(p.resolve_user_name("someone")) == "someone"


# ── Egress: every request goes through the SDK's guarded fetch ─────────────


class _FetchRecorder:
    """Stands in for ``personalclaw.sdk.net.fetch`` and records what it was asked to do."""

    def __init__(self, status: int = 200, body: object = None) -> None:
        self.calls: list[dict] = []
        self._status = status
        self._body = json.dumps(body if body is not None else []).encode("utf-8")

    async def __call__(self, url, *, policy, method="GET", headers=None, data=None):
        self.calls.append(
            {"url": url, "policy": policy, "method": method, "headers": headers}
        )
        return FetchResponse(url=url, status=self._status, body=self._body)


def test_a_poll_goes_through_the_guard_with_the_connector_policy(monkeypatch) -> None:
    recorder = _FetchRecorder(body=[_thread()])
    monkeypatch.setattr("personalclaw.sdk.net.fetch", recorder)
    p = create_provider({"token": "tok", "timeout_secs": 7})
    rows, _ = _poll(p, checkpoints={"github": "2026-09-01T00:00:00Z"})
    assert [r.id for r in rows] == ["1001"]
    (call,) = recorder.calls
    parts = urlsplit(call["url"])
    assert (
        f"{parts.scheme}://{parts.netloc}{parts.path}"
        == "https://api.github.com/notifications"
    )
    assert parse_qs(parts.query)["since"] == ["2026-09-01T00:00:00Z"]
    assert call["method"] == "GET"
    assert call["policy"].name == "connector"
    assert call["policy"].timeout_s == 7.0
    # The token travels in a header, never in the URL.
    assert call["headers"]["Authorization"] == "Bearer tok"
    assert "tok" not in call["url"]


def test_a_status_that_is_not_200_degrades_to_empty(monkeypatch) -> None:
    monkeypatch.setattr("personalclaw.sdk.net.fetch", _FetchRecorder(status=401))
    p = create_provider({"token": "bad"})
    with pytest.raises(GithubApiError, match="401"):
        asyncio.run(p._fetch_notifications(""))
    rows, cps = _poll(p, checkpoints={"github": "keep-me"})
    assert rows == [] and cps == {"github": "keep-me"}


def test_the_app_opens_no_socket_of_its_own() -> None:
    """The guard is the ONE way out: no module of this app imports an HTTP client or a socket."""
    banned = {"urllib.request", "http.client", "socket", "requests", "httpx", "aiohttp"}
    found: dict[str, list[str]] = {}
    for path in sorted(_HERE.glob("*.py")):
        if path.name.startswith("test_"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        mods = [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        ] + [
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        ]
        hits = sorted(m for m in mods if m in banned)
        if hits:
            found[path.name] = hits
    assert found == {}
