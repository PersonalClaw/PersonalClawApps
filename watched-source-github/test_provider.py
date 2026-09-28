"""Contract + behaviour tests for the watched-source-github trigger source.

Contract: personalclaw.sdk.trigger_source:TriggerSourceProvider

No network: behaviour tests stub ``_get`` and drive the poll helpers directly, and the
egress tests replace the SDK's guarded ``fetch`` so no socket opens.
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import pytest

from personalclaw.sdk.net import FetchResponse
from provider import GithubApiError, WatchedSourceGithubProvider, create_provider

_HERE = Path(__file__).resolve().parent


def _returns(value):
    """An async ``_get`` stub that answers every path with *value*."""

    async def _get(path: str):
        return value

    return _get


def _run(coro):
    return asyncio.run(coro)


# ── Contract shape ────────────────────────────────────────────────────────


def test_factory_returns_the_provider() -> None:
    assert isinstance(create_provider({}), WatchedSourceGithubProvider)


def test_factory_accepts_no_config() -> None:
    assert isinstance(create_provider(None), WatchedSourceGithubProvider)


def test_nothing_abstract_is_left() -> None:
    assert not getattr(WatchedSourceGithubProvider, "__abstractmethods__", frozenset())


def test_registers_under_the_app_name() -> None:
    assert create_provider({}).name == "watched-source-github"


def test_declares_its_event_vocabulary() -> None:
    assert create_provider({}).events == ("new_release", "new_issue")


def test_settings_reach_the_provider() -> None:
    p = create_provider(
        {"timeout_secs": 5, "repos": "a/b, c/d", "poll_interval_secs": 90}
    )
    assert p._timeout == 5
    assert p._repos == ["a/b", "c/d"]
    assert p._poll_secs == 90


def test_poll_interval_has_a_floor() -> None:
    assert create_provider({"poll_interval_secs": 1})._poll_secs == 60


def test_an_entry_that_is_not_owner_name_is_skipped() -> None:
    """A repository entry is spliced into the API path, so ``../users`` or ``a/..`` must not
    be able to walk the request anywhere else on the host."""
    p = create_provider({"repos": "a/b, ../users, a/.., just-a-name, c/d.e, x/y/z"})
    assert p._repos == ["a/b", "c/d.e"]


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


# ── Lifecycle ─────────────────────────────────────────────────────────────


def test_start_returns_immediately_and_stop_is_idempotent() -> None:
    async def scenario() -> None:
        p = create_provider({"repos": ""})
        await asyncio.wait_for(p.start(lambda e: None), timeout=1)
        assert p._task is not None and not p._task.done()
        await p.stop()
        assert p._task is None
        await p.stop()  # second stop is a no-op

    _run(scenario())


def test_double_start_keeps_one_loop() -> None:
    async def scenario() -> None:
        p = create_provider({"repos": ""})
        await p.start(lambda e: None)
        first = p._task
        await p.start(lambda e: None)
        assert p._task is first
        await p.stop()

    _run(scenario())


# ── Releases: high-water then emit ───────────────────────────────────────


def _release(release_id: int, tag: str) -> dict:
    return {
        "id": release_id,
        "tag_name": tag,
        "name": f"Release {tag}",
        "html_url": f"https://github.example/a/b/releases/tag/{tag}",
    }


def test_first_release_observation_emits_nothing() -> None:
    p = create_provider({"repos": "a/b"})
    p._get = _returns(_release(1, "v1.0.0"))
    assert _run(p._poll_release("a/b")) == []
    assert p._seen_release["a/b"] == "1"


def test_new_release_emits_after_the_mark_is_planted() -> None:
    p = create_provider({"repos": "a/b"})
    p._get = _returns(_release(1, "v1.0.0"))
    _run(p._poll_release("a/b"))
    p._get = _returns(_release(2, "v1.1.0"))
    (event,) = _run(p._poll_release("a/b"))
    assert event.event == "new_release"
    assert event.key == "2"
    assert "v1.1.0" in event.text
    assert event.meta["repo"] == "a/b"


def test_unchanged_release_stays_silent() -> None:
    p = create_provider({"repos": "a/b"})
    p._get = _returns(_release(1, "v1.0.0"))
    _run(p._poll_release("a/b"))
    assert _run(p._poll_release("a/b")) == []


def test_repo_with_no_releases_is_a_normal_state() -> None:
    p = create_provider({"repos": "a/b"})
    p._get = _returns(None)  # the 404 path answers None
    assert _run(p._poll_release("a/b")) == []


# ── Issues: since-window + PR filtering ──────────────────────────────────


def _issue(number: int, created: str, title: str = "t", pr: bool = False) -> dict:
    row = {
        "number": number,
        "created_at": created,
        "title": title,
        "user": {"login": "someone"},
        "html_url": f"https://github.example/a/b/issues/{number}",
    }
    if pr:
        row["pull_request"] = {"url": "..."}
    return row


def test_first_issue_observation_plants_the_mark_silently() -> None:
    p = create_provider({"repos": "a/b"})
    p._get = _returns([_issue(1, "2026-09-01T00:00:00Z")])
    assert _run(p._poll_issues("a/b")) == []
    assert p._seen_issue_at["a/b"] == "2026-09-01T00:00:00Z"


def test_newer_issue_emits_and_advances_the_mark() -> None:
    p = create_provider({"repos": "a/b"})
    p._get = _returns([_issue(1, "2026-09-01T00:00:00Z")])
    _run(p._poll_issues("a/b"))
    p._get = _returns([_issue(2, "2026-09-02T00:00:00Z", "newer")])
    (event,) = _run(p._poll_issues("a/b"))
    assert event.event == "new_issue"
    assert event.key == "a/b#2"
    assert "newer" in event.text
    assert p._seen_issue_at["a/b"] == "2026-09-02T00:00:00Z"


def test_pull_requests_are_filtered_out() -> None:
    p = create_provider({"repos": "a/b"})
    p._get = _returns([_issue(1, "2026-09-01T00:00:00Z")])
    _run(p._poll_issues("a/b"))
    p._get = _returns([_issue(2, "2026-09-02T00:00:00Z", pr=True)])
    assert _run(p._poll_issues("a/b")) == []


# ── Egress: every request goes through the SDK's guarded fetch ─────────────


class _FetchRecorder:
    """Stands in for ``personalclaw.sdk.net.fetch`` and records what it was asked to do."""

    def __init__(self, status: int = 200, body: object = None) -> None:
        self.calls: list[dict] = []
        self._status = status
        self._body = json.dumps(body if body is not None else {}).encode("utf-8")

    async def __call__(self, url, *, policy, method="GET", headers=None, data=None):
        self.calls.append(
            {"url": url, "policy": policy, "method": method, "headers": headers}
        )
        return FetchResponse(url=url, status=self._status, body=self._body)


def test_a_request_goes_through_the_guard_with_the_connector_policy(
    monkeypatch,
) -> None:
    recorder = _FetchRecorder(body=_release(7, "v7"))
    monkeypatch.setattr("personalclaw.sdk.net.fetch", recorder)
    p = create_provider({"repos": "a/b", "token": "tok", "timeout_secs": 9})
    assert _run(p._get("/repos/a/b/releases/latest"))["id"] == 7
    (call,) = recorder.calls
    assert call["url"] == "https://api.github.com/repos/a/b/releases/latest"
    assert call["method"] == "GET"
    assert call["policy"].name == "connector"
    assert call["policy"].timeout_s == 9.0
    assert call["headers"]["Authorization"] == "Bearer tok"


def test_no_token_sends_no_authorization_header(monkeypatch) -> None:
    recorder = _FetchRecorder(body=[])
    monkeypatch.setattr("personalclaw.sdk.net.fetch", recorder)
    _run(create_provider({"repos": "a/b"})._get("/repos/a/b/issues"))
    assert "Authorization" not in recorder.calls[0]["headers"]


def test_a_404_reads_as_nothing_to_report(monkeypatch) -> None:
    monkeypatch.setattr("personalclaw.sdk.net.fetch", _FetchRecorder(status=404))
    assert (
        _run(create_provider({"repos": "a/b"})._get("/repos/a/b/releases/latest"))
        is None
    )


def test_any_other_status_raises_so_the_loop_skips_the_round(monkeypatch) -> None:
    monkeypatch.setattr("personalclaw.sdk.net.fetch", _FetchRecorder(status=503))
    with pytest.raises(GithubApiError, match="503"):
        _run(create_provider({"repos": "a/b"})._get("/repos/a/b/releases/latest"))


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
        hits = sorted(
            m for m in mods if m in banned or m.split(".")[0] in {"socket", "requests"}
        )
        if hits:
            found[path.name] = hits
    assert found == {}


# ── Degrade to silence ────────────────────────────────────────────────────


def test_api_failure_skips_the_round_and_loop_survives() -> None:
    async def scenario() -> list:
        emitted: list = []
        p = create_provider({"repos": "a/b"})

        async def boom(repo):
            raise OSError("down")

        p._poll_repo = boom
        p._poll_secs = 0.01  # type: ignore[assignment]
        await p.start(emitted.append)
        await asyncio.sleep(0.05)  # several rounds, all failing
        assert p._task is not None and not p._task.done()  # loop alive
        await p.stop()
        return emitted

    assert _run(scenario()) == []
