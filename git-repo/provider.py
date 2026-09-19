"""git-repo — index the CONTENT of git repositories into the knowledge library (AECO-1/2).

A first-party knowledge-source app. Point it at a git repository you own — a **local
working clone** or a **remote github.com URL** — and every text file in it (source code AND
docs) becomes a searchable item in ``knowledge.db`` with ``provider="git-repo"``
attribution, reachable through ``knowledge_search`` / ``HybridRetriever``. A re-poll after a
new commit ingests **only the files that changed** since the commit this source last
ingested (a commit-SHA cursor), never the whole tree again.

Why this is not ``git-sync`` / ``dir-sync`` / ``notes``:

* It is a :class:`~personalclaw.sdk.knowledge.KnowledgeSourceProvider` (``provider.type ==
  "knowledge"``, ``capabilities: ["source"]``) — it BUILDS the knowledge index. ``git-sync``
  is a ``sync`` transport for PersonalClaw's own durability shards; ``notes`` is a git
  markdown notebook that keeps no index of its own.
* It indexes **source code**, not just markdown. ``dir_source``'s default include list is
  ``*.md/*.markdown/*.txt/*.rst/*.org`` — docs only. This connector's default covers the
  common source-code extensions too, so a ``.py``/``.ts`` file is as searchable as a README.

Configuration comes from **two places that compose**, and the order is the whole point:

* the app's **settings** (the ``settingsSchema`` below) are the DEFAULTS for every source —
  the repository you index most, the ref, the globs, the token credential;
* each **source's own spec** overrides any of those keys for that one source, and reaches
  ``poll`` through the engine (``spec=``) rather than through a file this app owns.

So ONE install watches as many repositories as you add sources for: set the spec of the
first to ``{"repo": "/Users/you/code/api"}`` and the second to
``{"repo": "https://github.com/you/web"}`` and each poll indexes its own tree with its own
commit cursor. A source whose spec is empty inherits the settings unchanged, which is the
single-repo install and behaves exactly as it did before (AECO-2). ``resolve_source_spec``
does the merge and closes the key set, so a typo (``repos``) is refused when you save the
source rather than silently indexing the default repository forever.

Egress discipline (the channel/knowledge conformance rule, honored exactly):

* **The local-clone path opens no network socket at all** — it reads the repository through
  *local-only* git plumbing (``rev-parse`` / ``ls-tree`` / ``show`` / ``diff``). It NEVER
  runs a network git verb (``clone`` / ``fetch`` / ``pull`` / ``remote`` / ``ls-remote``):
  reading a clone already on disk is a filesystem read, not a fetch.
* **The remote path routes every byte through the core net chokepoint** — ``sdk.net.fetch``
  under the engine-supplied ``SOURCE`` egress policy (host classification, private-IP
  denial, redirect re-check). It owns no HTTP client and no socket. An optional access token
  for a private repo is resolved from the credential store and sent in a **header**, never
  in a URL (a URL reaches the egress audit row, the server's access log, and any redirect's
  Referer).
* **Results are fenced by the standard ingest path.** The provider emits raw file text as
  ``SourceItem.content`` (exactly as the core web/feed source providers do); the engine
  fences the title at the stream boundary and the knowledge pipeline treats source-item
  content as untrusted data when it reaches a model. The connector adds no un-fenced side
  channel — it owns neither the fetch nor a model call.
"""

from __future__ import annotations

import base64
import binascii
import fnmatch
import json
import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote, urlparse

from personalclaw.sdk.knowledge import (
    KnowledgeItem,
    KnowledgeSource,
    KnowledgeSourceProvider,
    SourceItem,
    SourcePollResult,
    resolve_source_spec,
)

logger = logging.getLogger(__name__)

#: The provider name — the value the engine stamps as each item's ``provider`` attribution
#: and the name a WatchedSource row references.
PROVIDER_NAME = "git-repo"

#: The ``SourceItem.change`` vocabulary (WATCHED-SOURCES §1.1). The SDK re-exports
#: ``SourceItem`` (whose ``change`` field defaults to ``"created"``) but NOT these three
#: constants, and an app must not reach past ``personalclaw.sdk.*`` nor modify core — so the
#: closed vocabulary is mirrored here as literals matching ``SourceItem``'s contract exactly.
#: The engine owns what each kind MEANS (created→new item, modified→re-index the same item,
#: deleted→archive, never hard-delete); this connector only reports the sighting.
CHANGE_CREATED = "created"
CHANGE_MODIFIED = "modified"
CHANGE_DELETED = "deleted"

#: Files considered when settings name no ``include`` globs. Unlike a watched notes directory
#: (docs only), a code repository's value is its SOURCE — so the default spans the common
#: source-code extensions AND the doc/markup ones. A ``.py`` or ``.ts`` file is as indexable
#: as a README, which is the whole point of this connector.
DEFAULT_INCLUDE = (
    # source
    "*.py", "*.pyi", "*.ts", "*.tsx", "*.js", "*.jsx", "*.mjs", "*.cjs",
    "*.go", "*.rs", "*.java", "*.kt", "*.kts", "*.rb", "*.php", "*.cs",
    "*.c", "*.h", "*.cc", "*.cpp", "*.hpp", "*.m", "*.mm", "*.swift",
    "*.scala", "*.clj", "*.ex", "*.exs", "*.erl", "*.hs", "*.lua", "*.pl",
    "*.r", "*.jl", "*.dart", "*.sh", "*.bash", "*.zsh", "*.fish", "*.ps1",
    "*.sql", "*.proto", "*.graphql", "*.tf", "*.vue", "*.svelte",
    # docs / markup / structured text
    "*.md", "*.markdown", "*.mdx", "*.rst", "*.txt", "*.org", "*.adoc", "*.tex",
    "*.json", "*.jsonc", "*.yaml", "*.yml", "*.toml", "*.ini", "*.cfg",
    "*.conf", "*.properties", "*.html", "*.htm", "*.css", "*.scss", "*.less",
    "*.xml", "*.csv",
)

#: Never indexed whatever the include globs say: VCS internals, dependency/build noise, and
#: the churn that would dominate every diff. Matched against any path COMPONENT.
SKIP_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
        ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
        ".next", ".nuxt", "target", "vendor", ".idea", ".vscode", ".personalclaw",
        "site-packages", ".tox", ".gradle", "Pods",
    }
)

#: Hard ceiling on files ingested per poll — a repo with a huge generated tree degrades to a
#: bounded ingest, not a multi-hour walk. Clamps the ``max_files`` setting.
MAX_FILES_PER_SOURCE = 5000

#: Per-file content ceiling. A file's text is truncated to this on read so one pathological
#: blob cannot blow the poll's memory; its identity (and a later deletion) is still tracked.
MAX_FILE_BYTES = 1024 * 1024

#: Ceiling for any single local ``git`` invocation, so a corrupt repo is a bounded failure.
GIT_TIMEOUT_SECS = 60

#: Local paths the connector must never index, even if explicitly configured — the
#: bypass-immune class (decision 7). The primary guard is "must be a git work tree", which a
#: credential directory is not; this is defense in depth for the case where one happens to be.
_SENSITIVE_BASENAMES = frozenset({".ssh", ".aws", ".gnupg", ".gpg", ".personalclaw", ".config"})

#: The GitHub REST host the remote path speaks. Remote is deliberately GitHub-only in v1; any
#: other remote is refused with guidance to use a local clone (which is host-agnostic).
_GITHUB_API = "https://api.github.com"
_GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})


def _matchers(include: Any) -> tuple[str, ...]:
    if isinstance(include, str):
        include = [p.strip() for p in include.split(",")]
    pats = tuple(str(p) for p in (include or ()) if str(p).strip())
    return pats or DEFAULT_INCLUDE


def _globs(raw: Any) -> tuple[str, ...]:
    """A comma-separated string or a list → a tuple of non-blank globs (no default)."""
    if isinstance(raw, str):
        raw = [p.strip() for p in raw.split(",")]
    return tuple(str(p).strip() for p in (raw or ()) if str(p).strip())


def _path_included(rel: str, include: tuple[str, ...], exclude: tuple[str, ...]) -> bool:
    """Whether a repo-relative posix path is indexable: not under a skip dir, matches an
    include glob (on its basename), and matches no exclude glob (path or basename)."""
    parts = rel.split("/")
    if any(p in SKIP_DIRS for p in parts[:-1]):
        return False
    name = parts[-1]
    if not any(fnmatch.fnmatch(name, pat) for pat in include):
        return False
    if any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(name, pat) for pat in exclude):
        return False
    return True


def _decode(raw: bytes) -> str:
    """Blob bytes → text, truncated to :data:`MAX_FILE_BYTES`, undecodable bytes replaced."""
    return raw[:MAX_FILE_BYTES].decode("utf-8", errors="replace")


def _is_remote(repo: str) -> bool:
    return repo.lower().startswith(("http://", "https://"))


def _parse_github(repo: str) -> tuple[str, str] | None:
    """``(owner, name)`` for a github.com URL, or None for any other host/shape."""
    parsed = urlparse(repo)
    if (parsed.hostname or "").lower() not in _GITHUB_HOSTS:
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        return None
    owner, name = parts[0], parts[1]
    if name.endswith(".git"):
        name = name[:-4]
    return (owner, name) if owner and name else None


def _validate_repo(repo: str) -> tuple[bool, str]:
    """Whether this repository is indexable at all, with the user's remediation when not.

    Module-level, not a method: one source's repository has nothing to do with the provider
    INSTANCE (which now serves every source of this app), and a guard reading ``self``
    would be a guard that could read the wrong source's value.
    """
    if _is_remote(repo):
        if _parse_github(repo) is None:
            return False, (
                "only github.com repository URLs are supported for remote sources; for any "
                "other host, point 'repo' at a local clone on this machine"
            )
        return True, ""
    resolved = _resolve_local(repo)
    if os.path.basename(resolved.rstrip("/")) in _SENSITIVE_BASENAMES:
        return False, "path is a sensitive location and cannot be indexed"
    if not os.path.isdir(resolved):
        return False, f"path is not a directory: {resolved}"
    if not os.path.exists(os.path.join(resolved, ".git")):
        return False, (
            f"path is not a git repository (no .git found): {resolved} — point 'repo' at a "
            "git clone, or use a remote github.com URL"
        )
    return True, ""


def _resolve_local(repo: str) -> str:
    return os.path.abspath(os.path.expandvars(os.path.expanduser(repo)))


#: The keys a source's spec may carry. Deliberately the SAME names as the app's settings,
#: because a spec's whole job is to override a setting FOR ONE SOURCE. Handed to
#: ``resolve_source_spec`` as its closed key set, so ``repos`` instead of ``repo`` is refused
#: when the source is saved — the alternative is a source that quietly indexes the install
#: default forever while its spec says otherwise, which reads as working.
SPEC_KEYS = ("repo", "ref", "include", "exclude", "max_files", "token_credential")


@dataclass(frozen=True)
class _RepoConfig:
    """ONE source's resolved configuration — its spec laid over the install settings.

    Frozen, and passed down every poll path as an argument rather than read off ``self``:
    one provider instance now serves every source of this app, so a value on the instance is
    a value that belongs to whichever source polled last. That is precisely the ceiling
    AECO-2 lifts, and the type system is the cheapest place to keep it lifted.
    """

    repo: str
    ref: str
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    max_files: int
    token_credential: str


class GitRepoSourceProvider(KnowledgeSourceProvider):
    """Poll-capable knowledge source over a git repository's file CONTENT (AECO-1/AECO-2).

    One instance serves EVERY source of this app. The constructor takes the app settings,
    which are the per-install DEFAULTS; each source's own spec (:data:`SPEC_KEYS`, delivered
    to ``poll`` by the engine) overrides them for that source alone:

    ``repo``        the repository — an absolute local path (a working clone) OR an
                    ``https://github.com/<owner>/<repo>`` URL.
    ``ref``         the branch/tag/commit to index (default ``HEAD`` → the repo's default head).
    ``include``     glob patterns for filenames (defaults to :data:`DEFAULT_INCLUDE`).
    ``exclude``     glob patterns to drop, matched on the full relative path or basename.
    ``max_files``   per-poll file cap, clamped to :data:`MAX_FILES_PER_SOURCE`.
    ``token_credential`` credential-store name of a token for a PRIVATE remote (optional).

    ``fetch_fn`` and ``secret_resolver`` are injectable seams (remote path only): a test
    drives the GitHub REST calls through ``fetch_fn`` so no test opens a socket — the same
    shape core's connector-pack provider uses.
    """

    #: A repo is somebody's server on the remote path; the engine clamps this up to its
    #: network floor anyway. The local path is cheap, but one interval fits both.
    poll_interval_seconds = 1800

    def __init__(
        self,
        *,
        repo: str = "",
        ref: str = "HEAD",
        include: Any = None,
        exclude: Any = None,
        max_files: int = MAX_FILES_PER_SOURCE,
        token_credential: str = "",
        fetch_fn: Callable[..., Any] | None = None,
        secret_resolver: Callable[[str], str] | None = None,
    ) -> None:
        # The install settings, kept RAW and keyed exactly like `SPEC_KEYS` so a source's
        # spec can be laid over them key-for-key. Coercion happens once per poll in
        # `_resolve`, against the MERGED values — coercing here would mean doing it twice,
        # in two places, on two different objects.
        self._settings: dict[str, Any] = {
            "repo": (repo or "").strip(),
            "ref": (ref or "HEAD").strip() or "HEAD",
            "include": include,
            "exclude": exclude,
            "max_files": max_files,
            "token_credential": (token_credential or "").strip(),
        }
        self._fetch_fn = fetch_fn
        self._secret_resolver = secret_resolver

    # ── one source's configuration: its spec over the install settings ───────────────

    def _resolve(self, spec: dict | None) -> tuple[_RepoConfig | None, str]:
        """This source's config, or ``(None, error)``. The ONE place the two layers meet.

        Called from ``validate_spec`` at save time AND from ``poll`` — the same call, so the
        message a user is refused with at save time is the message the poll would produce.
        Re-running it per poll is not redundant: the spec is a mutable row an MCP tool or a
        hand-edit can change after the save, and a repository that was a git work tree when
        the source was created can be an unmounted path by the next poll.
        """
        raw, err = resolve_source_spec(spec, defaults=self._settings, allowed=SPEC_KEYS)
        if err:
            return None, err
        repo = str(raw.get("repo") or "").strip()
        if not repo:
            return None, (
                "no repository for this source: put a 'repo' key in its spec (a local clone "
                "path or a github.com URL), or set a default repository in the Git "
                "Repository app's Settings"
            )
        ok, verr = _validate_repo(repo)
        if not ok:
            return None, verr
        # `or` would be wrong here: a configured `0` is a value to CLAMP (to 1, as before),
        # not an absent one to default. Only None/"" mean "unset".
        want = raw.get("max_files")
        try:
            max_files = (
                MAX_FILES_PER_SOURCE
                if want in (None, "")
                else max(1, min(int(want), MAX_FILES_PER_SOURCE))
            )
        except (TypeError, ValueError):
            max_files = MAX_FILES_PER_SOURCE
        return (
            _RepoConfig(
                repo=repo,
                ref=str(raw.get("ref") or "HEAD").strip() or "HEAD",
                include=_matchers(raw.get("include")),
                exclude=_globs(raw.get("exclude")),
                max_files=max_files,
                token_credential=str(raw.get("token_credential") or "").strip(),
            ),
            "",
        )

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @property
    def display_name(self) -> str:
        return "Git Repository"

    # ── corpus contract (the library itself owns search/get; see dir_source) ──────────

    async def list_sources(self) -> list[KnowledgeSource]:
        return []

    async def search(self, query: str, limit: int = 10) -> list[KnowledgeItem]:
        # Items land in the library on ingest, so the library's own search covers them; a
        # second search path here would be a divergent ranking of the same rows.
        return []

    async def get_item(self, item_id: str) -> KnowledgeItem | None:
        return None

    # ── spec validation (fail CLOSED, at create time AND at poll time) ────────────────

    def validate_spec(self, spec: dict) -> tuple[bool, str]:
        """The provider's verdict on one source's spec, at save time (core calls this from
        ``POST /api/knowledge/sources``). Exactly :meth:`_resolve`'s verdict, so a spec that
        saves is a spec that polls and the two can never drift apart."""
        cfg, err = self._resolve(spec)
        return (True, "") if cfg is not None else (False, err)

    # ── the poll ──────────────────────────────────────────────────────────────────────

    async def poll(
        self, source_id: str, cursor: str = "", *, spec: dict | None = None, policy: Any = None
    ) -> SourcePollResult:
        """One incremental pass over THIS source's repo. Never raises to the engine (§1.1) —
        a bad config, an unreadable repo, or an egress denial is a soft error so the source
        degrades rather than killing the loop. ``cursor`` is the last-ingested commit SHA
        (opaque to the engine); ``spec`` is this source's own row, delivered by the engine
        because an app holds no store handle to read it with (AECO-2)."""
        cfg, err = self._resolve(spec)
        if cfg is None:
            # Cursor untouched: a transient misconfiguration (an unmounted clone) must not wipe
            # the commit cursor, or the next good poll would re-ingest the whole tree.
            return SourcePollResult(cursor=cursor, error=err)
        try:
            if _is_remote(cfg.repo):
                return await self._poll_remote(cfg, cursor, policy)
            return self._poll_local(cfg, cursor)
        except Exception as exc:  # noqa: BLE001 — a poll must never raise to the engine
            logger.warning("git-repo poll of %s failed", source_id, exc_info=True)
            return SourcePollResult(cursor=cursor, error=f"poll failed: {str(exc)[:180]}")

    @staticmethod
    def _parse_cursor(cursor: str) -> str:
        """The last-ingested commit SHA out of the opaque cursor, or "" when unseeded."""
        try:
            data = json.loads(cursor) if cursor else {}
        except (TypeError, ValueError):
            return ""
        return str(data.get("commit") or "") if isinstance(data, dict) else ""

    @staticmethod
    def _dump_cursor(commit: str) -> str:
        return json.dumps({"commit": commit}, sort_keys=True)

    # ── local clone: local-only git plumbing (NO network verb, ever) ───────────────────

    def _git(self, repo: str, *args: str) -> subprocess.CompletedProcess:
        """Run ``git -C <repo> <args>`` captured, with a hard timeout. Every call site below
        passes only LOCAL plumbing (``rev-parse`` / ``ls-tree`` / ``show`` / ``diff``) — this
        method must never be handed a network verb (clone/fetch/pull/remote/ls-remote).
        Reading an existing clone touches no socket."""
        return subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True,
            timeout=GIT_TIMEOUT_SECS,
            check=False,
        )

    def _rev(self, repo: str, ref: str) -> str | None:
        cp = self._git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        sha = cp.stdout.decode("utf-8", "replace").strip()
        return sha or None

    def _list_tree(self, repo: str, sha: str, cfg: _RepoConfig) -> list[str]:
        """The included blob paths at ``sha``, sorted and capped. ``-z`` avoids git's path
        quoting; ``-r`` flattens so only blobs (files) appear."""
        cp = self._git(repo, "ls-tree", "-r", "-z", "--name-only", sha)
        if cp.returncode != 0:
            raise RuntimeError((cp.stderr.decode("utf-8", "replace").strip() or "ls-tree failed")[:180])
        paths = [p for p in cp.stdout.decode("utf-8", "replace").split("\0") if p]
        kept = sorted(p for p in paths if _path_included(p, cfg.include, cfg.exclude))
        return kept[: cfg.max_files]

    def _read_local(self, repo: str, sha: str, rel: str) -> str:
        cp = self._git(repo, "show", f"{sha}:{rel}")
        return _decode(cp.stdout) if cp.returncode == 0 else ""

    def _diff(self, repo: str, old: str, new: str) -> list[tuple[str, str, str]]:
        """``[(status, path, new_path)]`` between two commits. ``status`` is a single letter
        (A/M/D/T/R/C); for renames/copies ``new_path`` is the destination and ``path`` the
        source. ``-z`` NUL-separates fields so a path with a tab/space stays intact."""
        cp = self._git(repo, "diff", "--name-status", "-z", old, new)
        if cp.returncode != 0:
            raise RuntimeError((cp.stderr.decode("utf-8", "replace").strip() or "diff failed")[:180])
        toks = cp.stdout.decode("utf-8", "replace").split("\0")
        out: list[tuple[str, str, str]] = []
        i = 0
        while i < len(toks):
            status = toks[i]
            if not status:
                i += 1
                continue
            letter = status[0]
            if letter in ("R", "C"):  # rename/copy carry TWO paths: old then new
                if i + 2 >= len(toks):
                    break
                out.append((letter, toks[i + 1], toks[i + 2]))
                i += 3
            else:
                if i + 1 >= len(toks):
                    break
                out.append((letter, toks[i + 1], ""))
                i += 2
        return out

    def _poll_local(self, cfg: _RepoConfig, cursor: str) -> SourcePollResult:
        repo = _resolve_local(cfg.repo)
        head = self._rev(repo, cfg.ref)
        if head is None:
            return SourcePollResult(cursor=cursor, error=f"cannot resolve ref {cfg.ref!r} in {repo}")
        prev = self._parse_cursor(cursor)

        if not prev:
            # First poll: ingest the whole tree at HEAD (that is the point — index the repo).
            items = [
                self._make_item(rel, self._read_local(repo, head, rel), CHANGE_CREATED)
                for rel in self._list_tree(repo, head, cfg)
            ]
            return SourcePollResult(items=items, cursor=self._dump_cursor(head))
        if prev == head:
            return SourcePollResult(items=[], cursor=self._dump_cursor(head))

        items: list[SourceItem] = []
        for letter, path, new_path in self._diff(repo, prev, head):
            items.extend(self._local_change_items(cfg, repo, head, letter, path, new_path))
            if len(items) >= cfg.max_files:
                break
        return SourcePollResult(items=items[: cfg.max_files], cursor=self._dump_cursor(head))

    def _local_change_items(
        self, cfg: _RepoConfig, repo: str, head: str, letter: str, path: str, new_path: str
    ) -> list[SourceItem]:
        """Map one diff record to zero or more sightings, honoring the include filter on BOTH
        ends of a rename so a file renamed out of scope is archived and one renamed in is
        created."""
        inc, exc = cfg.include, cfg.exclude
        if letter == "D":
            return [self._deleted_item(path)] if _path_included(path, inc, exc) else []
        if letter in ("R", "C"):
            out: list[SourceItem] = []
            if letter == "R" and _path_included(path, inc, exc):
                out.append(self._deleted_item(path))  # the old name is gone
            if _path_included(new_path, inc, exc):
                out.append(self._make_item(new_path, self._read_local(repo, head, new_path), CHANGE_CREATED))
            return out
        # A (added), M (modified), T (type-changed) → the file exists at HEAD.
        if not _path_included(path, inc, exc):
            return []
        change = CHANGE_CREATED if letter == "A" else CHANGE_MODIFIED
        return [self._make_item(path, self._read_local(repo, head, path), change)]

    # ── remote (github): every byte through sdk.net.fetch; no socket owned here ─────────

    async def _fetch_json(self, url: str, *, policy: Any, headers: dict[str, str]) -> tuple[int, Any]:
        """One guarded GET → ``(status, parsed_json_or_None)``. The ONE way remote bytes
        enter: the injected ``fetch_fn`` in tests, else the core ``sdk.net.fetch`` chokepoint
        under the engine's egress policy. This app owns no HTTP client and no socket."""
        if self._fetch_fn is not None:
            resp = await self._fetch_fn(url, policy=policy, method="GET", headers=headers)
        else:
            from personalclaw.sdk.net import CONNECTOR, egress_policy_for, fetch

            # The engine hands the SOURCE policy; a direct call falls back to the CONNECTOR
            # posture the SDK exposes (host-classified, private-IP-denied), never unguarded.
            resolved = policy if policy is not None else egress_policy_for(CONNECTOR)
            resp = await fetch(url, policy=resolved, method="GET", headers=headers)
        status = int(getattr(resp, "status", 0) or 0)
        body = getattr(resp, "body", b"") or b""
        if status == 200 and body:
            try:
                return status, json.loads(body)
            except (TypeError, ValueError):
                return status, None
        return status, None

    def _remote_headers(self, cfg: _RepoConfig) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "personalclaw-git-repo",
        }
        token = self._resolve_token(cfg.token_credential)
        if token:
            headers["Authorization"] = f"Bearer {token}"  # header only — never the URL
        return headers

    def _resolve_token(self, credential: str) -> str:
        """The token for THIS source's credential name (a private repo can be per-source)."""
        if not credential:
            return ""
        if self._secret_resolver is not None:
            return self._secret_resolver(credential) or ""
        from personalclaw.sdk.credentials import CredentialStore
        from personalclaw.sdk.util import config_dir

        try:
            return CredentialStore(config_dir()).resolve(credential).secret or ""
        except Exception:  # noqa: BLE001 — a missing token just means "try unauthenticated"
            logger.debug("git-repo: credential %r not resolvable", credential)
            return ""

    async def _poll_remote(self, cfg: _RepoConfig, cursor: str, policy: Any) -> SourcePollResult:
        parsed = _parse_github(cfg.repo)
        if parsed is None:  # already refused in _validate_repo, but poll re-checks
            return SourcePollResult(cursor=cursor, error="unsupported remote (github.com only)")
        owner, name = parsed
        headers = self._remote_headers(cfg)
        base = f"{_GITHUB_API}/repos/{quote(owner)}/{quote(name)}"

        status, commit = await self._fetch_json(
            f"{base}/commits/{quote(cfg.ref, safe='')}", policy=policy, headers=headers
        )
        if status != 200 or not isinstance(commit, dict) or not commit.get("sha"):
            return SourcePollResult(
                cursor=cursor, error=f"could not resolve {owner}/{name}@{cfg.ref} (HTTP {status})"
            )
        head = str(commit["sha"])
        prev = self._parse_cursor(cursor)

        if not prev:
            return await self._remote_full(cfg, base, head, policy, headers, cursor)
        if prev == head:
            return SourcePollResult(items=[], cursor=self._dump_cursor(head))
        return await self._remote_incremental(cfg, base, prev, head, policy, headers, cursor)

    async def _remote_full(self, cfg, base, head, policy, headers, cursor) -> SourcePollResult:
        status, tree = await self._fetch_json(
            f"{base}/git/trees/{head}?recursive=1", policy=policy, headers=headers
        )
        if status != 200 or not isinstance(tree, dict):
            return SourcePollResult(cursor=cursor, error=f"could not read tree (HTTP {status})")
        entries = [
            e for e in (tree.get("tree") or [])
            if isinstance(e, dict) and e.get("type") == "blob" and e.get("path")
            and _path_included(str(e["path"]), cfg.include, cfg.exclude)
        ]
        entries.sort(key=lambda e: str(e["path"]))
        items = [
            self._make_item(
                str(e["path"]),
                await self._fetch_blob(base, str(e.get("sha") or ""), policy, headers),
                CHANGE_CREATED,
            )
            for e in entries[: cfg.max_files]
        ]
        result = SourcePollResult(items=items, cursor=self._dump_cursor(head))
        if tree.get("truncated"):
            result.error = "repository tree was truncated by GitHub; a local clone indexes it fully"
        return result

    async def _remote_incremental(self, cfg, base, prev, head, policy, headers, cursor) -> SourcePollResult:
        status, cmp = await self._fetch_json(
            f"{base}/compare/{prev}...{head}", policy=policy, headers=headers
        )
        if status != 200 or not isinstance(cmp, dict):
            return SourcePollResult(cursor=cursor, error=f"could not compare commits (HTTP {status})")
        items: list[SourceItem] = []
        for f in cmp.get("files") or []:
            if not isinstance(f, dict):
                continue
            items.extend(await self._remote_change_items(cfg, base, f, policy, headers))
            if len(items) >= cfg.max_files:
                break
        return SourcePollResult(items=items[: cfg.max_files], cursor=self._dump_cursor(head))

    async def _remote_change_items(self, cfg, base, f, policy, headers) -> list[SourceItem]:
        inc, exc = cfg.include, cfg.exclude
        status = str(f.get("status") or "")
        path = str(f.get("filename") or "")
        prev_path = str(f.get("previous_filename") or "")
        sha = str(f.get("sha") or "")
        if status == "removed":
            return [self._deleted_item(path)] if _path_included(path, inc, exc) else []
        if status == "renamed":
            out: list[SourceItem] = []
            if prev_path and _path_included(prev_path, inc, exc):
                out.append(self._deleted_item(prev_path))
            if _path_included(path, inc, exc):
                out.append(self._make_item(path, await self._fetch_blob(base, sha, policy, headers), CHANGE_CREATED))
            return out
        if not _path_included(path, inc, exc):
            return []
        change = CHANGE_CREATED if status in ("added", "copied") else CHANGE_MODIFIED
        return [self._make_item(path, await self._fetch_blob(base, sha, policy, headers), change)]

    async def _fetch_blob(self, base, sha, policy, headers) -> str:
        if not sha:
            return ""
        status, blob = await self._fetch_json(f"{base}/git/blobs/{sha}", policy=policy, headers=headers)
        if status != 200 or not isinstance(blob, dict):
            return ""
        if blob.get("encoding") == "base64":
            try:
                return _decode(base64.b64decode(str(blob.get("content") or "")))
            except (binascii.Error, ValueError):
                return ""
        return _decode(str(blob.get("content") or "").encode("utf-8", "replace"))

    # ── shared item shaping ─────────────────────────────────────────────────────────

    def _make_item(self, rel: str, content: str, change: str) -> SourceItem:
        """A live-file sighting. ``guid`` is the repo-relative path — stable across commits, so
        an edit re-indexes the SAME item rather than minting a duplicate."""
        return SourceItem(
            guid=rel, title=rel, content=content, change=change, metadata={"relative_path": rel}
        )

    def _deleted_item(self, rel: str) -> SourceItem:
        from datetime import datetime, timezone

        return SourceItem(
            guid=rel,
            title=rel,
            change=CHANGE_DELETED,
            metadata={"source_deleted_at": datetime.now(timezone.utc).isoformat()},
        )


def create_provider(config: dict[str, Any] | None = None) -> GitRepoSourceProvider:
    """Manifest factory (``provider.implementation = "provider:create_provider"``).

    ``config`` is the app's ``ProviderSettings`` (a mapping), and it supplies the DEFAULTS
    every source inherits. ONE instance is built per install and serves every source of this
    app; each source's own spec overrides these values for itself (:data:`SPEC_KEYS`), which
    is what makes one install able to watch several repositories.
    """
    config = config or {}
    return GitRepoSourceProvider(
        repo=str(config.get("repo", "") or ""),
        ref=str(config.get("ref", "") or "HEAD"),
        include=config.get("include"),
        exclude=config.get("exclude"),
        max_files=config.get("max_files", MAX_FILES_PER_SOURCE),
        token_credential=str(config.get("token_credential", "") or ""),
    )
