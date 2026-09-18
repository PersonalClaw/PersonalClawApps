"""git-repo connector tests — real ``git`` against local fixture repos, no network.

The falsification target (AECO-1), each clause asserted so a degenerate implementation
cannot pass:

* **BOTH a source file AND a doc are searchable** — a fixture repo with a ``.py`` and a
  ``.ts`` AND a ``.md`` is polled, then ``HybridRetriever`` is queried for a token unique to
  EACH. A ``dir_source``-style docs-only connector (``*.md`` include) fails the ``.py``/``.ts``
  assertions. Attribution is checked to be ``provider="git-repo"``.
* **Cursor-aware incremental, not full re-ingest** — after a second commit adding ONE file,
  the PROVIDER'S OWN ``poll`` returns EXACTLY ONE item (the new file), and the engine reports
  exactly one new item. Asserting the provider (not just the engine's dedup gate) returned one
  is what proves the commit-cursor diff, not a whole-tree re-emit collapsed by the novelty gate.
* **All fetching through the net chokepoint, no app-owned socket** — the remote path is driven
  through an injected ``fetch_fn`` (so no socket opens) and asserted to reach ONLY the GitHub
  REST endpoints, using the ``compare`` API for the incremental poll; a structural rail proves
  the module imports no socket/HTTP library and runs no NETWORK git verb.

Time/network are never touched: local git is real subprocess against ``tmp_path`` repos; the
remote path is a canned-JSON fake. Isolation: ``tmp_path`` knowledge.db + ``PERSONALCLAW_HOME``.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from provider import (
    DEFAULT_INCLUDE,
    MAX_FILES_PER_SOURCE,
    GitRepoSourceProvider,
    create_provider,
)
from personalclaw.knowledge.retrieval import HybridRetriever
from personalclaw.knowledge.source_engine import SourceEngine
from personalclaw.knowledge.store import KnowledgeStore

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


# ── fixtures + helpers ────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PERSONALCLAW_HOME", str(tmp_path / "home"))


@pytest.fixture()
def store(tmp_path):
    return KnowledgeStore(str(tmp_path / "knowledge.db"))


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@personalclaw.local",
         "-C", str(cwd), *args],
        capture_output=True, text=True, check=True,
    )


def _commit(repo: Path, msg: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", msg)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture()
def repo(tmp_path):
    """A git repo with a source file, a second-language source file, and a doc — the exact
    shape that separates a code connector from a docs-only one."""
    d = tmp_path / "myrepo"
    d.mkdir()
    _git(d, "init", "-b", "main")
    (d / "alpha.py").write_text("# quokkamarker\ndef alpha():\n    return 1\n", encoding="utf-8")
    (d / "beta.ts").write_text("// narwhalmarker\nexport const beta = 2;\n", encoding="utf-8")
    (d / "guide.md").write_text("# Guide\n\naxolotlmarker documentation here.\n", encoding="utf-8")
    _commit(d, "initial")
    return d


class _FakeQueue:
    def __init__(self):
        self.enqueued: list[str] = []

    def enqueue(self, item_id: str) -> None:
        self.enqueued.append(item_id)

    def recover_pending(self) -> int:
        return 0


def _cfg(**over):
    from personalclaw.config.loader import SourcesConfig

    base = dict(
        enabled=True, poll_interval_default_secs=1, network_floor_secs=0,
        max_sources=100, max_items_per_poll=1000, daily_request_budget=288,
    )
    base.update(over)
    return SourcesConfig(**base)


def _engine(store, provider):
    return SourceEngine(
        store, _FakeQueue(),
        providers_lister=lambda: [provider],
        config_loader=lambda: _cfg(),
    )


def _rows(store, sid):
    return store.db.execute(
        "SELECT * FROM items WHERE source_id = ? ORDER BY guid", (sid,)
    ).fetchall()


async def _poll(engine, store, sid):
    return await engine.poll_source(store.get_source(sid), _cfg())


# ── clause 1: BOTH a source file AND a doc land and are searchable ─────────────────


@pytest.mark.asyncio
async def test_source_and_doc_both_ingested_and_searchable(store, repo):
    provider = create_provider({"repo": str(repo)})
    engine = _engine(store, provider)
    sid = store.create_source(
        name="myrepo", provider="git-repo", kind="external", spec={}, item_type="bookmark"
    )

    new_count = await _poll(engine, store, sid)
    assert new_count == 3, "the .py, the .ts and the .md should all ingest on the first poll"

    guids = {r["guid"] for r in _rows(store, sid)}
    assert guids == {"alpha.py", "beta.ts", "guide.md"}
    for r in _rows(store, sid):
        assert r["provider"] == "git-repo", "each item is attributed to the app"

    ret = HybridRetriever(store)  # no embedder → keyword (FTS) arm, which is enough here

    py_hits = ret.search("quokkamarker")
    assert any(h["title"] == "alpha.py" and h["provider"] == "git-repo" for h in py_hits), (
        "a SOURCE-CODE file must be searchable — a docs-only (*.md) connector fails here"
    )
    ts_hits = ret.search("narwhalmarker")
    assert any(h["title"] == "beta.ts" for h in ts_hits), "the .ts source file must be searchable"
    md_hits = ret.search("axolotlmarker")
    assert any(h["title"] == "guide.md" for h in md_hits), "the doc must be searchable too"


# ── clause 2: a re-poll after one new commit adds EXACTLY ONE item (cursor-aware) ──


@pytest.mark.asyncio
async def test_second_poll_after_new_commit_adds_exactly_one_item(store, repo):
    provider = create_provider({"repo": str(repo)})
    engine = _engine(store, provider)
    sid = store.create_source(
        name="myrepo", provider="git-repo", kind="external", spec={}, item_type="bookmark"
    )

    assert await _poll(engine, store, sid) == 3
    cursor_after_first = store.get_source_cursor(sid)
    assert json.loads(cursor_after_first)["commit"], "the cursor records the ingested commit SHA"

    # A new commit adds exactly one file.
    (repo / "gamma.py").write_text("# gamma\nvalue = 3\n", encoding="utf-8")
    _commit(repo, "add gamma")

    # The PROVIDER itself — not the engine's dedup gate — returns exactly one item: the new
    # file. A whole-tree re-emit (relying on the novelty gate to collapse N→1) would return 4.
    result = await provider.poll(sid, cursor_after_first)
    assert len(result.items) == 1, "cursor-aware incremental must emit only the changed file"
    assert result.items[0].guid == "gamma.py"
    assert result.items[0].change == "created"

    # And end to end the engine records exactly one new item; the tree is now four rows.
    assert await _poll(engine, store, sid) == 1
    assert {r["guid"] for r in _rows(store, sid)} == {"alpha.py", "beta.ts", "guide.md", "gamma.py"}


@pytest.mark.asyncio
async def test_no_change_second_poll_is_quiet(store, repo):
    provider = create_provider({"repo": str(repo)})
    engine = _engine(store, provider)
    sid = store.create_source(name="r", provider="git-repo", kind="external", spec={})
    await _poll(engine, store, sid)
    assert await _poll(engine, store, sid) == 0, "no new commit → nothing re-ingested"


# ── clause 2 (continued): modify re-indexes the SAME item; delete archives it ──────


@pytest.mark.asyncio
async def test_modify_reindexes_same_item_delete_archives_and_keeps_row(store, repo):
    provider = create_provider({"repo": str(repo)})
    engine = _engine(store, provider)
    sid = store.create_source(name="r", provider="git-repo", kind="external", spec={})
    await _poll(engine, store, sid)
    py_before = next(r for r in _rows(store, sid) if r["guid"] == "alpha.py")

    # Edit one file, delete another.
    (repo / "alpha.py").write_text("# quokkamarker\ndef alpha():\n    return 42\n", encoding="utf-8")
    (repo / "beta.ts").unlink()
    _commit(repo, "edit alpha, drop beta")

    assert await _poll(engine, store, sid) == 1, "one re-index (the edit); an archive is not a re-index"

    py_after = next(r for r in _rows(store, sid) if r["guid"] == "alpha.py")
    assert py_after["id"] == py_before["id"], "a modify updates the SAME item, no duplicate row"
    assert "return 42" in py_after["content"]

    ts_after = next(r for r in _rows(store, sid) if r["guid"] == "beta.ts")
    assert ts_after["is_archived"], "a deleted file is archived…"
    item = store.get_item(ts_after["id"])
    assert item is not None and item["file_metadata"].get("source_deleted_at"), "…never hard-deleted"


# ── clause 3: remote path routes through net.fetch and is incremental ──────────────


@dataclass
class _FakeResp:
    status: int
    body: bytes
    headers: dict = field(default_factory=dict)


def _b64(text: str) -> str:
    import base64

    return base64.b64encode(text.encode("utf-8")).decode("ascii")


class _FakeGitHub:
    """A canned github.com REST backend: records every URL and serves JSON from a route map,
    so the connector's remote path is exercised with no socket."""

    def __init__(self, routes: dict[str, dict]):
        self.routes = routes
        self.calls: list[str] = []

    async def __call__(self, url, *, policy=None, method="GET", headers=None):
        self.calls.append(url)
        for pattern, payload in self.routes.items():
            if pattern in url:
                return _FakeResp(200, json.dumps(payload).encode("utf-8"))
        return _FakeResp(404, b"{}")


@pytest.mark.asyncio
async def test_remote_full_then_incremental_only_through_net_fetch():
    tree_routes = {
        "/commits/HEAD": {"sha": "sha1"},
        "/git/trees/sha1": {
            "truncated": False,
            "tree": [
                {"path": "app.py", "type": "blob", "sha": "blobpy"},
                {"path": "README.md", "type": "blob", "sha": "blobmd"},
                {"path": "logo.png", "type": "blob", "sha": "blobpng"},  # excluded by globs
            ],
        },
        "/git/blobs/blobpy": {"encoding": "base64", "content": _b64("# tokaapp\nx = 1\n")},
        "/git/blobs/blobmd": {"encoding": "base64", "content": _b64("# readme tokamd\n")},
    }
    gh = _FakeGitHub(tree_routes)
    provider = create_provider({"repo": "https://github.com/acme/widgets"})
    provider._fetch_fn = gh  # inject the canned backend (no socket)

    first = await provider.poll("src-remote", "", policy=None)
    assert {i.guid for i in first.items} == {"app.py", "README.md"}, "binary is filtered; text ingested"
    assert any("tokaapp" in i.content for i in first.items), "blob content decoded from base64"
    assert json.loads(first.cursor)["commit"] == "sha1"
    # Every byte came through the injected chokepoint, only to GitHub's REST API.
    assert gh.calls and all(u.startswith("https://api.github.com/") for u in gh.calls)

    # Incremental: a new head, and the compare API returns exactly one changed file.
    gh2 = _FakeGitHub({
        "/commits/HEAD": {"sha": "sha2"},
        "/compare/sha1...sha2": {"files": [{"filename": "new.py", "status": "added", "sha": "blobnew"}]},
        "/git/blobs/blobnew": {"encoding": "base64", "content": _b64("# newfile\n")},
    })
    provider._fetch_fn = gh2
    second = await provider.poll("src-remote", first.cursor, policy=None)
    assert [i.guid for i in second.items] == ["new.py"], "incremental emits only the changed file"
    assert any("/compare/sha1...sha2" in u for u in gh2.calls), "used the commit-cursor compare API"
    assert not any("/git/trees/" in u for u in gh2.calls), "did NOT re-walk the whole tree"


@pytest.mark.asyncio
async def test_remote_token_rides_in_a_header_never_the_url():
    captured = {}

    async def fetch_fn(url, *, policy=None, method="GET", headers=None):
        captured["url"] = url
        captured["headers"] = headers or {}
        return _FakeResp(200, json.dumps({"sha": "s"}).encode())

    provider = create_provider(
        {"repo": "https://github.com/acme/widgets", "token_credential": "gh_tok"}
    )
    provider._fetch_fn = fetch_fn
    provider._secret_resolver = lambda name: "SECRET123" if name == "gh_tok" else ""
    await provider.poll("src", "", policy=None)
    assert captured["headers"].get("Authorization") == "Bearer SECRET123"
    assert "SECRET123" not in captured["url"], "a secret must never reach the URL"


# ── clause 3 (structural rail): no app-owned socket, no NETWORK git verb ───────────


def test_module_owns_no_socket_and_runs_no_network_git_verb():
    src = Path(__file__).with_name("provider.py").read_text(encoding="utf-8")

    # No HTTP/socket library is imported: the ONLY network path is personalclaw.sdk.net.
    for banned in (
        "import socket", "import ssl", "import http.client", "from http.client",
        "import urllib.request", "from urllib.request", "import requests",
        "import httpx", "import aiohttp",
    ):
        assert banned not in src, f"the connector must own no HTTP client: found {banned!r}"

    tree = ast.parse(src)

    # subprocess is used for LOCAL git only, and every git subcommand is a read-only,
    # local plumbing verb — never a network verb (clone/fetch/pull/remote/ls-remote).
    ALLOWED_GIT_VERBS = {"rev-parse", "ls-tree", "show", "diff"}
    git_verbs: set[str] = set()
    subprocess_calls = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "run":
                if isinstance(func.value, ast.Name) and func.value.id == "subprocess":
                    subprocess_calls += 1
                    # First arg is the ["git", "-C", repo, "<verb>", ...] list.
                    if node.args and isinstance(node.args[0], ast.List):
                        strs = [
                            e.value for e in node.args[0].elts
                            if isinstance(e, ast.Constant) and isinstance(e.value, str)
                        ]
                        for s in strs:
                            if s in ("clone", "fetch", "pull", "remote", "ls-remote"):
                                git_verbs.add(s)  # a network verb slipped in — caught below
    assert subprocess_calls >= 1, "the local path shells out to git"
    assert git_verbs == set(), f"NETWORK git verbs are forbidden, found: {sorted(git_verbs)}"


def test_provider_imports_only_the_sdk():
    """Belt-and-suspenders over the repo-wide import-boundary lint: provider.py reaches core
    only through personalclaw.sdk.*."""
    src = Path(__file__).with_name("provider.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    offenders: list[str] = []
    for node in ast.walk(tree):
        mods: list[str] = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods = [node.module]
        for m in mods:
            if m == "personalclaw" or (m.startswith("personalclaw.") and not m.startswith("personalclaw.sdk")):
                offenders.append(m)
    assert offenders == [], f"app code may only import personalclaw.sdk.*, found: {offenders}"


# ── config + validation ────────────────────────────────────────────────────────────


def test_create_provider_reads_settings(tmp_path):
    p = create_provider({
        "repo": "/some/clone", "ref": "dev", "include": "*.py, *.md",
        "exclude": "*.min.js", "max_files": 42, "token_credential": "gh",
    })
    assert p.name == "git-repo" and p.display_name == "Git Repository"
    assert p._repo == "/some/clone" and p._ref == "dev"
    assert p._include == ("*.py", "*.md") and p._exclude == ("*.min.js",)
    assert p._max_files == 42 and p._token_credential == "gh"


def test_create_provider_defaults_are_idle_and_broad():
    p = create_provider(None)
    assert p._repo == "" and p._ref == "HEAD"
    assert p._include == DEFAULT_INCLUDE
    assert "*.py" in DEFAULT_INCLUDE and "*.md" in DEFAULT_INCLUDE, "source AND docs by default"
    assert p._max_files == MAX_FILES_PER_SOURCE


def test_max_files_is_clamped():
    assert create_provider({"repo": "/x", "max_files": 10_000})._max_files == MAX_FILES_PER_SOURCE
    assert create_provider({"repo": "/x", "max_files": 0})._max_files == 1
    assert create_provider({"repo": "/x", "max_files": "nope"})._max_files == MAX_FILES_PER_SOURCE


def test_validate_spec_requires_configured_repo_and_empty_spec(repo):
    # No repo configured → refuse creating a source, with guidance.
    idle = create_provider(None)
    ok, err = idle.validate_spec({})
    assert ok is False and "Settings" in err

    # Repo configured (a real clone) + empty spec → accepted.
    p = create_provider({"repo": str(repo)})
    assert p.validate_spec({})[0] is True

    # A non-empty spec is refused — the repo belongs in Settings, not the spec.
    ok, err = p.validate_spec({"repo": "elsewhere"})
    assert ok is False and "Settings" in err


def test_validate_repo_local_and_remote_rules(tmp_path, repo):
    p = create_provider(None)
    assert p._validate_repo(str(repo))[0] is True                       # a real git clone
    assert p._validate_repo(str(tmp_path / "nope"))[0] is False          # missing
    plain = tmp_path / "plain"
    plain.mkdir()
    assert p._validate_repo(str(plain))[0] is False                     # dir but not a git repo
    assert p._validate_repo("https://github.com/acme/widgets")[0] is True
    assert p._validate_repo("https://github.com/acme/widgets.git")[0] is True
    ok, err = p._validate_repo("https://gitlab.com/acme/widgets")       # non-github remote
    assert ok is False and "github.com" in err


@pytest.mark.asyncio
async def test_poll_with_no_repo_is_a_soft_error_not_a_raise():
    result = await create_provider(None).poll("src", "")
    assert result.items == [] and "no repository configured" in result.error


@pytest.mark.asyncio
async def test_include_override_narrows_ingestion(store, repo):
    provider = create_provider({"repo": str(repo), "include": "*.md"})  # docs only, by choice
    engine = _engine(store, provider)
    sid = store.create_source(name="r", provider="git-repo", kind="external", spec={})
    await _poll(engine, store, sid)
    assert {r["guid"] for r in _rows(store, sid)} == {"guide.md"}, "an explicit include narrows scope"
