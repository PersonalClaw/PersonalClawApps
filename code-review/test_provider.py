"""Tests for the code-review tool provider and its review pipeline.

Everything here runs with no network, no credentials, no gateway and no `gh`: the PR diff
comes from ``fixtures/sample_pr.diff`` and the model leg of the fan-out runs against a
fake registry entry. That is deliberate — a test that needed a live PR could only ever
tell you the app worked once.

Contract: personalclaw.sdk.tool:ToolProvider
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from personalclaw.sdk.manifest import AppManifest
from personalclaw.sdk.model import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent, ProviderEntry
from personalclaw.sdk.security import fence_untrusted
from personalclaw.sdk.tool import RiskLevel

from provider import (
    FANOUT_MODES,
    CodeReviewProvider,
    GhError,
    create_provider,
    parse_model_findings,
)
from review import (
    DEPTH_BUDGET,
    Finding,
    FindingsLog,
    PrRef,
    blast_radius,
    build_briefs,
    parse_diff,
    parse_pr_ref,
    render_report,
    static_findings,
)

HERE = Path(__file__).parent
FIXTURE = HERE / "fixtures" / "sample_pr.diff"
REF = PrRef("acme", "widget", 42)


@pytest.fixture
def diff() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture
def files(diff: str):
    return blast_radius(parse_diff(diff))


@pytest.fixture
def home(monkeypatch, tmp_path):
    """Point core's config dir at a tmp dir so the findings log never touches ~."""
    monkeypatch.setenv("PERSONALCLAW_HOME", str(tmp_path / "pclaw"))
    return tmp_path


@pytest.fixture
def provider(home) -> CodeReviewProvider:
    return create_provider({"fanout": "static"})


# ── The scaffold contract (a change that breaks registration fails here first) ──

CONTRACT_METHODS = ("display_name", "invoke", "list_tools", "name")


def test_factory_returns_the_provider() -> None:
    assert isinstance(create_provider({}), CodeReviewProvider)


def test_factory_accepts_no_config() -> None:
    assert isinstance(create_provider(None), CodeReviewProvider)


def test_nothing_abstract_is_left() -> None:
    assert not getattr(CodeReviewProvider, "__abstractmethods__", frozenset())


def test_registers_under_the_app_name() -> None:
    assert create_provider({}).name == "code-review"


def test_declares_its_display_name() -> None:
    assert create_provider({}).display_name == "Code Review"


def test_every_contract_method_is_declared() -> None:
    for name in CONTRACT_METHODS:
        assert name in vars(CodeReviewProvider), f"{name} is not implemented"


def test_settings_reach_the_provider() -> None:
    p = create_provider({"timeout_secs": 5, "concurrency": 7, "fanout": "plan"})
    assert (p._timeout, p._concurrency, p._fanout) == (5, 7, "plan")


def test_bad_settings_fall_back_instead_of_raising() -> None:
    p = create_provider({"fanout": "nonsense", "concurrency": 99, "timeout_secs": 1})
    assert p._fanout == "model"
    assert p._concurrency == 8  # clamped, not crashed
    assert p._timeout == 5  # floored
    assert create_provider({"timeout_secs": 0})._timeout == 20  # 0 means "unset"


def test_constructing_the_provider_touches_no_filesystem(monkeypatch, tmp_path) -> None:
    """Core builds a provider just to read its tool list; that must not mkdir under ~."""
    monkeypatch.setenv("PERSONALCLAW_HOME", str(tmp_path / "unused"))
    create_provider({})
    assert not (tmp_path / "unused").exists()


@pytest.mark.asyncio
async def test_exposes_the_three_tools(provider) -> None:
    tools = {t.name: t for t in await provider.list_tools()}
    assert set(tools) == {"review_pr", "record_finding", "review_findings"}
    assert tools["review_pr"].provider == "code-review"
    # A read-only review of someone else's PR should not stall on an approval prompt.
    assert all(not t.requires_approval for t in tools.values())


@pytest.mark.asyncio
async def test_declared_risk_matches_what_each_tool_actually_does(provider) -> None:
    """The Tools page renders this declaration as a badge. All three were declared SAFE,
    which put a green Safe badge on the tool that spawns `gh` — contradicting the install
    scanner's own warning about the same call. The badge has to match the behaviour."""
    tools = {t.name: t for t in await provider.list_tools()}
    # Spawns a subprocess and reads a remote repo; also appends to the findings log.
    assert tools["review_pr"].risk_level is RiskLevel.CAUTION
    # Writes to disk.
    assert tools["record_finding"].risk_level is RiskLevel.CAUTION
    # Reads the local log and nothing else.
    assert tools["review_findings"].risk_level is RiskLevel.SAFE


@pytest.mark.asyncio
async def test_unknown_tool_is_a_failure_with_a_hint(provider) -> None:
    result = await provider.invoke("review_everything", {})
    assert not result.success
    assert "review_pr" in " ".join(result.recovery_hints)


def test_manifest_round_trips_and_declares_minimum_permissions() -> None:
    raw = json.loads((HERE / "app.json").read_text(encoding="utf-8"))
    manifest = AppManifest.from_dict(raw)
    assert AppManifest.from_dict(manifest.to_dict()).to_dict() == manifest.to_dict()
    # The install-consent surface: local storage for the findings log, and no network —
    # GitHub is reached only through the user's own already-authenticated `gh`.
    assert raw["permissions"] == {"storage": True, "network": False}
    assert raw["provider"]["type"] == "tool"


def test_settings_schema_covers_every_fanout_mode() -> None:
    raw = json.loads((HERE / "app.json").read_text(encoding="utf-8"))
    schema = raw["provider"]["settingsSchema"]["properties"]["fanout"]
    assert tuple(schema["enum"]) == FANOUT_MODES


# ── PR reference parsing ────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    "acme/widget#42",
    "https://github.com/acme/widget/pull/42",
    "http://github.com/acme/widget/pull/42/",
    "acme/widget/pull/42",
])
def test_accepts_every_pr_reference_shape(raw: str) -> None:
    assert parse_pr_ref(raw) == REF


@pytest.mark.parametrize("raw", [
    "",
    "acme/widget",
    "widget#42",
    "acme/widget#notanumber",
    "acme/../../etc/passwd#1",
    "acme/widget#42; rm -rf /",
    "--repo=evil/repo#1",
    "https://evil.example.com/acme/widget/pull/42",
    "acme/widget#42\nacme/other#1",
])
def test_refuses_anything_that_is_not_a_pr_reference(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_pr_ref(raw)


def test_slug_is_filesystem_safe() -> None:
    assert REF.slug == "acme__widget__42"
    assert "/" not in REF.slug and ".." not in REF.slug


# ── Diff parsing ────────────────────────────────────────────────────────────

def test_parses_every_file_in_the_fixture(diff: str) -> None:
    paths = [f.path for f in parse_diff(diff)]
    assert paths == [
        "src/auth/session.py",
        "src/api/routes.py",
        "src/api/handlers.js",
        "docs/auth.md",
        "tests/test_session.py",
        "package-lock.json",
        "assets/logo.png",
    ]


def test_classifies_status_binary_and_generated(diff: str) -> None:
    by = {f.path: f for f in parse_diff(diff)}
    assert by["tests/test_session.py"].status == "deleted"
    assert by["assets/logo.png"].binary
    assert by["package-lock.json"].generated
    assert not by["src/auth/session.py"].generated


def test_counts_added_and_removed_lines(diff: str) -> None:
    session = {f.path: f for f in parse_diff(diff)}["src/auth/session.py"]
    assert session.added > session.removed > 0
    assert session.churn == session.added + session.removed


def test_added_lines_strip_the_plus_marker(diff: str) -> None:
    session = {f.path: f for f in parse_diff(diff)}["src/auth/session.py"]
    assert any(line.startswith("SIGNING_KEY") for line in session.added_lines)
    assert not any(line.startswith("+") for line in session.added_lines)


def test_an_empty_diff_yields_no_files() -> None:
    assert parse_diff("") == []


# ── Blast radius ────────────────────────────────────────────────────────────

def test_heaviest_file_first(files) -> None:
    weights = [f.weight for f in files]
    assert weights == sorted(weights, reverse=True)


def test_auth_code_outweighs_its_own_documentation(files) -> None:
    by = {f.path: f for f in files}
    assert by["src/auth/session.py"].weight > by["src/api/routes.py"].weight
    assert by["src/api/routes.py"].weight > by["docs/auth.md"].weight


def test_lockfile_and_binary_are_capped(files) -> None:
    by = {f.path: f for f in files}
    assert by["package-lock.json"].weight <= 5
    assert by["assets/logo.png"].weight <= 5


def test_fan_in_inside_the_changed_set_is_counted(files) -> None:
    """Both routes.py and handlers.js import auth/session — that is what lifts it."""
    session = {f.path: f for f in files}["src/auth/session.py"]
    assert any("imported by" in reason for reason in session.weight_because)


def test_every_weight_is_explained(files) -> None:
    for f in files:
        assert f.weight_because, f"{f.path} was weighted with no reason recorded"
        assert 0 <= f.weight <= 100


def test_depth_follows_weight(files) -> None:
    for f in files:
        expected = "deep" if f.weight >= 65 else ("skim" if f.weight < 25 else "normal")
        assert f.depth == expected


def test_a_deleted_import_target_is_heavier_than_the_same_file_modified() -> None:
    common = ("diff --git a/src/core.py b/src/core.py\n@@ -1,2 +1,2 @@\n-a\n+b\n"
              "diff --git a/src/user.py b/src/user.py\n@@ -1,2 +1,2 @@\n+from src.core import x\n")
    modified = {f.path: f for f in blast_radius(parse_diff(common))}["src/core.py"]
    deleted_src = common.replace(
        "diff --git a/src/core.py b/src/core.py\n",
        "diff --git a/src/core.py b/src/core.py\ndeleted file mode 100644\n",
    )
    deleted = {f.path: f for f in blast_radius(parse_diff(deleted_src))}["src/core.py"]
    assert deleted.weight > modified.weight


# ── Per-file briefs: the isolation contract ─────────────────────────────────

def test_one_brief_per_reviewable_file(files) -> None:
    briefs = build_briefs(files)
    assert [b.path for b in briefs] == [
        f.path for f in files if not (f.binary or f.generated) and f.hunks
    ]
    assert len(briefs) == 5  # the lockfile and the png have nothing to review


def test_a_brief_carries_only_its_own_file(files) -> None:
    """The whole point of the fan-out: no brief can see another file's diff."""
    briefs = build_briefs(files)
    for brief in briefs:
        for other in briefs:
            if other.path == brief.path:
                continue
            assert other.path not in brief.prompt, f"{brief.path}'s brief leaked {other.path}"


def test_a_brief_states_its_own_weight_and_reasons(files) -> None:
    session = next(b for b in build_briefs(files) if b.path == "src/auth/session.py")
    assert f"{session.weight}/100" in session.prompt
    assert "imported by" in session.prompt


def test_depth_budgets_the_diff_a_brief_may_carry(files) -> None:
    big = next(f for f in files if f.path == "docs/auth.md")
    big.depth = "skim"
    big.hunks = ["@@ -1 +1 @@\n+" + "x" * (DEPTH_BUDGET["skim"] + 5_000)]
    brief = next(b for b in build_briefs([big]) if b.path == big.path)
    assert brief.truncated
    assert "diff truncated" in brief.prompt
    assert len(brief.prompt) < DEPTH_BUDGET["skim"] + 2_000


def test_the_diff_is_fenced_as_untrusted_data(files) -> None:
    """A PR diff is attacker-authored text about to be read by a model."""
    plain = build_briefs(files)[0].prompt
    fenced = build_briefs(files, fence=fence_untrusted)[0].prompt
    assert fenced != plain
    assert "untrusted" in fenced.lower()


# ── The deterministic pass ──────────────────────────────────────────────────

def test_finds_the_hard_coded_credential(files) -> None:
    hits = [f for f in static_findings(files) if "credential" in f.summary.lower()]
    assert hits and hits[0].file == "src/auth/session.py"
    assert hits[0].severity == "high"
    assert "SIGNING_KEY" in hits[0].evidence


def test_finds_the_blanket_except(files) -> None:
    assert any("exception swallow" in f.summary.lower() for f in static_findings(files))


def test_finds_debug_leftovers_and_the_bare_todo(files) -> None:
    summaries = " | ".join(f.summary for f in static_findings(files))
    assert "Debug print" in summaries
    assert "TODO" in summaries


def test_findings_are_severity_ordered(files) -> None:
    order = {"high": 0, "medium": 1, "low": 2}
    ranks = [order[f.severity] for f in static_findings(files)]
    assert ranks == sorted(ranks)


def test_generated_and_binary_files_raise_nothing(files) -> None:
    noisy = [f for f in static_findings(files)
             if f.file in {"package-lock.json", "assets/logo.png"}]
    assert noisy == []


def test_one_finding_per_rule_per_file() -> None:
    src = ("diff --git a/src/a.py b/src/a.py\n@@ -1 +1,3 @@\n"
           "+# TODO one\n+# TODO two\n+# TODO three\n")
    todos = [f for f in static_findings(blast_radius(parse_diff(src))) if "TODO" in f.summary]
    assert len(todos) == 1


def test_an_unreviewably_large_file_is_itself_the_finding() -> None:
    body = "\n".join(f"+line {i}" for i in range(900))
    src = f"diff --git a/src/big.py b/src/big.py\n@@ -1 +1,900 @@\n{body}\n"
    found = static_findings(blast_radius(parse_diff(src)))
    assert any("too large to review" in f.summary for f in found)


def test_a_clean_diff_raises_nothing() -> None:
    src = ("diff --git a/src/ok.py b/src/ok.py\n@@ -1,2 +1,3 @@\n"
           " def f():\n-    return 1\n+    return 2\n")
    assert static_findings(blast_radius(parse_diff(src))) == []


# ── The local findings log ──────────────────────────────────────────────────

def test_findings_round_trip_through_the_log(tmp_path) -> None:
    log = FindingsLog(tmp_path / "findings")
    written = log.append(REF, [
        Finding(file="a.py", severity="high", summary="boom", evidence="x = 1"),
        Finding(file="b.py", severity="low", summary="meh"),
    ])
    assert written == 2
    rows = log.read(REF)
    assert [r["file"] for r in rows] == ["a.py", "b.py"]
    assert rows[0]["severity"] == "high" and rows[0]["evidence"] == "x = 1"
    assert all("ts" in r for r in rows)


def test_one_finding_per_line(tmp_path) -> None:
    log = FindingsLog(tmp_path / "findings")
    log.append(REF, [Finding(file="a.py", severity="low", summary="one")])
    log.append(REF, [Finding(file="b.py", severity="low", summary="two")])
    lines = log.path_for(REF).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert all(json.loads(line)["summary"] for line in lines)


def test_static_findings_strip_control_characters_from_evidence() -> None:
    """ARCC SAX-06: attacker-authored evidence must not forge a second log record."""
    src = ("diff --git a/src/a.py b/src/a.py\n@@ -1 +1,2 @@\n"
           '+api_key = "abcdefgh12345"\r\n')
    found = static_findings(blast_radius(parse_diff(src)))
    assert found
    assert "\r" not in found[0].evidence and "\n" not in found[0].evidence


def test_a_torn_last_line_never_costs_the_reader_the_log(tmp_path) -> None:
    log = FindingsLog(tmp_path / "findings")
    log.append(REF, [Finding(file="a.py", severity="low", summary="kept")])
    with log.path_for(REF).open("a", encoding="utf-8") as fh:
        fh.write('{"file": "b.py", "sever')
    rows = log.read(REF)
    assert [r["summary"] for r in rows] == ["kept"]


def test_the_log_path_stays_inside_the_findings_dir(tmp_path) -> None:
    root = tmp_path / "findings"
    log = FindingsLog(root)
    assert log.path_for(REF).parent == root.resolve()


def test_reviewed_prs_lists_what_is_on_this_machine(tmp_path) -> None:
    log = FindingsLog(tmp_path / "findings")
    log.append(REF, [Finding(file="a.py", severity="low", summary="s")])
    log.append(PrRef("other", "repo", 7), [Finding(file="b.py", severity="low", summary="s")])
    assert log.reviewed_prs() == ["acme/widget/42", "other/repo/7"]


def test_reading_a_pr_with_no_findings_is_empty_not_an_error(tmp_path) -> None:
    assert FindingsLog(tmp_path / "findings").read(REF) == []


# ── Rendering ───────────────────────────────────────────────────────────────

def test_the_report_leads_with_the_weight_table(files) -> None:
    body = render_report(REF, files, static_findings(files), fanout="static",
                         log_path=Path("/tmp/x.jsonl"))
    assert body.index("## Blast radius") < body.index("## Findings")
    for f in files:
        assert f"`{f.path}`" in body


def test_the_report_says_where_the_findings_went(files) -> None:
    body = render_report(REF, files, [], fanout="static", log_path=Path("/tmp/x.jsonl"))
    assert "nothing was posted to the PR" in body
    assert "/tmp/x.jsonl" in body


# ── review_pr, end to end over the fixture ──────────────────────────────────

def _fake_gh(diff: str):
    async def _gh(self, ref):
        assert isinstance(ref, PrRef)
        return diff
    return _gh


@pytest.mark.asyncio
async def test_static_review_writes_findings_locally(monkeypatch, provider, diff) -> None:
    monkeypatch.setattr(CodeReviewProvider, "_gh_diff", _fake_gh(diff))
    result = await provider.invoke("review_pr", {"pr": "acme/widget#42"})
    assert result.success
    assert result.metadata["files_reviewed"] == 7
    assert result.metadata["findings"] == result.metadata["findings_written"] > 0
    kept = Path(result.metadata["findings_path"])
    assert kept.exists()
    rows = [json.loads(line) for line in kept.read_text(encoding="utf-8").splitlines()]
    assert {r["source"] for r in rows} == {"static"}


@pytest.mark.asyncio
async def test_review_orders_the_fan_out_by_weight(monkeypatch, provider, diff) -> None:
    monkeypatch.setattr(CodeReviewProvider, "_gh_diff", _fake_gh(diff))
    result = await provider.invoke("review_pr", {"pr": "acme/widget#42"})
    weights = list(result.metadata["weights"].values())
    assert weights == sorted(weights, reverse=True)
    assert result.metadata["briefs"][0]["file"] == "src/auth/session.py"


@pytest.mark.asyncio
async def test_max_files_caps_the_fan_out_and_says_so(monkeypatch, provider, diff) -> None:
    monkeypatch.setattr(CodeReviewProvider, "_gh_diff", _fake_gh(diff))
    result = await provider.invoke("review_pr", {"pr": "acme/widget#42", "max_files": 2})
    assert result.metadata["files_reviewed"] == 2
    assert result.metadata["files_dropped"] == 5
    assert "left out of the fan-out" in result.output


@pytest.mark.asyncio
async def test_plan_mode_hands_the_fan_out_to_the_host_agent(monkeypatch, home, diff) -> None:
    p = create_provider({"fanout": "plan"})
    monkeypatch.setattr(CodeReviewProvider, "_gh_diff", _fake_gh(diff))
    result = await p.invoke("review_pr", {"pr": "acme/widget#42"})
    assert result.success
    assert result.metadata["fanout"] == "plan"
    assert len(result.metadata["briefs"]) == 5
    assert "Spawn ONE subagent per brief" in result.output
    assert "record_finding" in result.output


@pytest.mark.asyncio
async def test_a_bad_pr_reference_never_reaches_gh(monkeypatch, provider) -> None:
    async def _explode(self, ref):  # pragma: no cover — must not be called
        raise AssertionError("gh was invoked with an unvalidated reference")
    monkeypatch.setattr(CodeReviewProvider, "_gh_diff", _explode)
    result = await provider.invoke("review_pr", {"pr": "acme/widget#42; rm -rf /"})
    assert not result.success
    assert "not a PR reference" in result.error


@pytest.mark.asyncio
async def test_a_missing_gh_is_an_actionable_failure(monkeypatch, provider) -> None:
    async def _missing(self, ref):
        raise FileNotFoundError("gh")
    monkeypatch.setattr(CodeReviewProvider, "_gh_diff", _missing)
    result = await provider.invoke("review_pr", {"pr": "acme/widget#42"})
    assert not result.success
    assert "gh auth login" in " ".join(result.recovery_hints)


@pytest.mark.asyncio
async def test_a_gh_refusal_is_reported_with_its_own_detail(monkeypatch, provider) -> None:
    async def _refuse(self, ref):
        raise GhError("`gh pr diff` failed for acme/widget#42 (exit 1): no such PR")
    monkeypatch.setattr(CodeReviewProvider, "_gh_diff", _refuse)
    result = await provider.invoke("review_pr", {"pr": "acme/widget#42"})
    assert not result.success
    assert "no such PR" in result.error
    assert result.recovery_hints


@pytest.mark.asyncio
async def test_an_empty_diff_is_a_failure_not_a_clean_bill(monkeypatch, provider) -> None:
    monkeypatch.setattr(CodeReviewProvider, "_gh_diff", _fake_gh(""))
    result = await provider.invoke("review_pr", {"pr": "acme/widget#42"})
    assert not result.success
    assert "no changed files" in result.error


@pytest.mark.asyncio
async def test_an_unknown_fanout_mode_is_refused(provider) -> None:
    result = await provider.invoke("review_pr", {"pr": "acme/widget#42", "fanout": "magic"})
    assert not result.success
    assert "model" in " ".join(result.recovery_hints)


# ── The model leg of the fan-out ────────────────────────────────────────────

class _FakeModel:
    """One instance per file. Records the prompt it saw, replies with one finding."""

    seen: list[str] = []
    started = 0
    torn_down = 0

    def __init__(self, reply: str = "") -> None:
        self._reply = reply or (
            '{"severity": "high", "summary": "unsafe comparison", "evidence": "sig == mint(...)"}\n'
        )

    async def start(self) -> None:
        type(self).started += 1

    async def stream(self, message: str):
        type(self).seen.append(message)
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=self._reply)
        yield LLMEvent(kind=EVENT_COMPLETE)

    async def shutdown(self) -> None:
        type(self).torn_down += 1


class _FakeRegistry:
    def __init__(self, entries):
        self._entries = entries
        self.built: list[str] = []

    def list_entries(self):
        return list(self._entries)

    def build(self, name, **kwargs):
        self.built.append(name)
        return _FakeModel()


@pytest.fixture
def fake_registry(monkeypatch):
    import personalclaw.sdk.model as sdk_model

    _FakeModel.seen = []
    _FakeModel.started = 0
    _FakeModel.torn_down = 0
    registry = _FakeRegistry([ProviderEntry(name="fake", type="fake", model="m")])
    monkeypatch.setattr(sdk_model, "get_default_registry", lambda: registry)
    return registry


@pytest.mark.asyncio
async def test_model_fanout_is_one_isolated_call_per_file(
    monkeypatch, home, diff, fake_registry
) -> None:
    p = create_provider({"fanout": "model", "concurrency": 2})
    monkeypatch.setattr(CodeReviewProvider, "_gh_diff", _fake_gh(diff))
    result = await p.invoke("review_pr", {"pr": "acme/widget#42"})
    assert result.success
    # Five reviewable files → five provider builds, five starts, five teardowns.
    assert len(fake_registry.built) == 5
    assert _FakeModel.started == 5 == _FakeModel.torn_down
    assert len(_FakeModel.seen) == 5
    # Every call saw exactly one file's diff.
    for prompt in _FakeModel.seen:
        assert prompt.count("File: ") == 1
    assert "model:fake" in result.metadata["fanout"]
    assert "5 isolated per-file calls" in result.metadata["fanout"]


@pytest.mark.asyncio
async def test_model_findings_are_persisted_and_attributed(
    monkeypatch, home, diff, fake_registry
) -> None:
    p = create_provider({"fanout": "model"})
    monkeypatch.setattr(CodeReviewProvider, "_gh_diff", _fake_gh(diff))
    result = await p.invoke("review_pr", {"pr": "acme/widget#42"})
    rows = [json.loads(line) for line in
            Path(result.metadata["findings_path"]).read_text(encoding="utf-8").splitlines()]
    assert {"static", "model"} <= {r["source"] for r in rows}
    model_rows = [r for r in rows if r["source"] == "model"]
    assert len(model_rows) == 5
    assert all(r["summary"] == "unsafe comparison" for r in model_rows)


@pytest.mark.asyncio
async def test_a_failing_per_file_review_is_named_not_swallowed(
    monkeypatch, home, diff, fake_registry
) -> None:
    calls = {"n": 0}

    def _flaky(name, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("provider exploded")
        return _FakeModel()

    monkeypatch.setattr(fake_registry, "build", _flaky)
    p = create_provider({"fanout": "model", "concurrency": 1})
    monkeypatch.setattr(CodeReviewProvider, "_gh_diff", _fake_gh(diff))
    result = await p.invoke("review_pr", {"pr": "acme/widget#42"})
    assert result.success
    assert "1 failed" in result.metadata["fanout"]
    assert "was NOT reviewed" in result.output


@pytest.mark.asyncio
async def test_no_model_provider_degrades_to_static_and_says_so(monkeypatch, home, diff) -> None:
    import personalclaw.sdk.model as sdk_model

    monkeypatch.setattr(sdk_model, "get_default_registry", lambda: _FakeRegistry([]))
    p = create_provider({"fanout": "model"})
    monkeypatch.setattr(CodeReviewProvider, "_gh_diff", _fake_gh(diff))
    result = await p.invoke("review_pr", {"pr": "acme/widget#42"})
    assert result.success
    assert "no model provider is registered" in result.metadata["fanout"]
    assert result.metadata["findings"] > 0  # the static pass still ran


def test_configured_model_entry_wins(monkeypatch, home) -> None:
    import personalclaw.sdk.model as sdk_model

    entries = [ProviderEntry(name="first", type="a", model="m"),
               ProviderEntry(name="chosen", type="b", model="m")]
    monkeypatch.setattr(sdk_model, "get_default_registry", lambda: _FakeRegistry(entries))
    assert create_provider({"model_entry": "chosen"})._resolve_entry() == "chosen"
    assert create_provider({"model_entry": "absent"})._resolve_entry() == "first"


# ── Reading a per-file review's answer ──────────────────────────────────────

def _brief(files, path="src/auth/session.py"):
    return next(b for b in build_briefs(files) if b.path == path)


def test_json_lines_become_findings(files) -> None:
    brief = _brief(files)
    out = parse_model_findings(
        '{"severity": "high", "summary": "one", "evidence": "x"}\n'
        "```\n"
        '{"severity": "medium", "summary": "two"}\n',
        brief,
    )
    assert [(f.severity, f.summary) for f in out] == [("high", "one"), ("medium", "two")]
    assert all(f.file == brief.path and f.source == "model" for f in out)
    assert all(f.weight == brief.weight for f in out)


def test_an_unknown_severity_lands_as_low(files) -> None:
    out = parse_model_findings('{"severity": "catastrophic", "summary": "s"}', _brief(files))
    assert out[0].severity == "low"


def test_a_clean_file_yields_nothing(files) -> None:
    assert parse_model_findings("", _brief(files)) == []
    assert parse_model_findings("   \n\n", _brief(files)) == []


def test_prose_is_kept_verbatim_rather_than_dropped(files) -> None:
    out = parse_model_findings("This file looks risky but I cannot say why.", _brief(files))
    assert len(out) == 1
    assert "kept verbatim" in out[0].summary
    assert "risky" in out[0].evidence


def test_a_summaryless_object_is_not_a_finding(files) -> None:
    assert parse_model_findings('{"severity": "high"}', _brief(files)) == []


# ── record_finding / review_findings ────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_subagent_records_a_finding(provider) -> None:
    result = await provider.invoke("record_finding", {
        "pr": "acme/widget#42", "file": "src/auth/session.py",
        "severity": "high", "summary": "signing key is hard-coded",
        "evidence": 'SIGNING_KEY = "s3cr3t"',
    })
    assert result.success
    rows = [json.loads(line) for line in
            Path(result.metadata["findings_path"]).read_text(encoding="utf-8").splitlines()]
    assert rows[0]["source"] == "subagent"
    assert rows[0]["summary"] == "signing key is hard-coded"


@pytest.mark.asyncio
@pytest.mark.parametrize("args,missing", [
    ({"pr": "acme/widget#42", "severity": "high", "summary": "s"}, "file"),
    ({"pr": "acme/widget#42", "file": "a.py", "severity": "high"}, "summary"),
])
async def test_record_finding_requires_a_file_and_a_summary(provider, args, missing) -> None:
    result = await provider.invoke("record_finding", args)
    assert not result.success
    assert missing in result.error


@pytest.mark.asyncio
async def test_record_finding_refuses_an_invented_severity(provider) -> None:
    result = await provider.invoke("record_finding", {
        "pr": "acme/widget#42", "file": "a.py", "severity": "apocalyptic", "summary": "s",
    })
    assert not result.success
    assert "severity must be one of" in result.error


@pytest.mark.asyncio
async def test_findings_read_back_and_filter_by_severity(provider) -> None:
    for severity in ("high", "low"):
        await provider.invoke("record_finding", {
            "pr": "acme/widget#42", "file": "a.py",
            "severity": severity, "summary": f"a {severity} thing",
        })
    everything = await provider.invoke("review_findings", {"pr": "acme/widget#42"})
    assert everything.metadata["findings"] == 2
    just_high = await provider.invoke(
        "review_findings", {"pr": "acme/widget#42", "severity": "high"}
    )
    assert just_high.metadata["findings"] == 1
    assert "a high thing" in just_high.output


@pytest.mark.asyncio
async def test_findings_with_no_pr_lists_every_reviewed_pr(provider) -> None:
    empty = await provider.invoke("review_findings", {})
    assert empty.success and empty.metadata["prs"] == []
    await provider.invoke("record_finding", {
        "pr": "acme/widget#42", "file": "a.py", "severity": "low", "summary": "s",
    })
    listed = await provider.invoke("review_findings", {})
    assert listed.metadata["prs"] == ["acme/widget/42"]


@pytest.mark.asyncio
async def test_an_unreviewed_pr_reads_as_empty_not_as_an_error(provider) -> None:
    result = await provider.invoke("review_findings", {"pr": "acme/widget#9999"})
    assert result.success and result.metadata["findings"] == 0


@pytest.mark.asyncio
async def test_review_findings_validates_the_reference(provider) -> None:
    result = await provider.invoke("review_findings", {"pr": "../../etc/passwd"})
    assert not result.success
