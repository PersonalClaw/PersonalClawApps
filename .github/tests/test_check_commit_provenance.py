"""The commit-provenance check holds every commit a pull request brings, and every commit that
lands on ``main``, to the project's commit policy.

``scripts/check_commit_provenance.py`` is core's check, copied unchanged (the first case below
holds the copy to core's file byte for byte), so one policy reads the same in both repositories.
Each case after it plants one commit in a scratch repository (no remote, no system or global git
configuration, no credential helper) and runs the check the way CI runs it: as a program, with
``--mode pr`` over a pull request's commits as the commits API returns them, or ``--mode main``
over a pushed range. They are core's own cases.

The rules:
  1. every commit carries ``Signed-off-by: <author name> <author email>`` as a line of its own,
     Dependabot's own commits (by its one exact address) skipped;
  2. (main only) every committer is an identity ``MAINTAINERS`` lists;
  3. no commit a maintainer authored (by name or by email) carries a co-author, a tool or
     session trailer, a generated-with footer or a session link;
  5. no commit message, and no pull request title, carries a marker that starts no run.

The workflow cases at the end hold the two places the check runs: ``dco.yml`` on every pull
request and again on each edit of one, which reads the commits from the API with the token named
on the fetch step alone, and ``provenance.yml`` on every push to main, which runs the PREVIOUS
main commit's copy of the check. Each shape rule fails on a planted edit of the real workflow,
and the workflows' own shell runs against scratch data, so what is pinned is what they do, not
how they are spelled.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "check_commit_provenance.py"
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"

MAINTAINER = ("Ada Maintainer", "ada@example.com")
MERGER = ("Merge Bot", "merge@example.com")
CONTRIBUTOR = ("Cy Contributor", "cy@example.org")
OTHER = ("Dee Other", "dee@example.net")
#: Dependabot's author identity, exactly as GitHub writes it on the bot's commits.
DEPENDABOT = ("dependabot[bot]", "49699333+dependabot[bot]@users.noreply.github.com")

_MAINTAINERS_FILE = (
    "# Identities allowed to commit on main.\n"
    "Ada Maintainer <ada@example.com>  the maintainer's own commits\n"
    "Merge Bot <merge@example.com>  the squash merge of a pull request\n"
)

_GIT_ENV_KEYS = ("GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_TERMINAL_PROMPT")


def _signoff(who: tuple[str, str]) -> str:
    return f"Signed-off-by: {who[0]} <{who[1]}>"


def _scratch_env(root: Path) -> dict[str, str]:
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(root),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    }


class Repo:
    """A scratch repository that commits with exactly the identities a case names."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True)
        self.env = _scratch_env(root)
        self.git("init", "-q", "-b", "main")
        self.git("config", "commit.gpgsign", "false")
        self.maintainers = root.parent / f"{root.name}-MAINTAINERS"
        self.maintainers.write_text(_MAINTAINERS_FILE, encoding="utf-8")
        self.base = self.commit("base\n\n" + _signoff(MAINTAINER))

    def git(self, *args: str, stdin: bytes | None = None) -> str:
        done = subprocess.run(
            ["git", *args],
            cwd=self.root,
            env=self.env,
            input=stdin,
            capture_output=True,
            check=True,
        )
        return done.stdout.decode("utf-8", "replace").strip()

    def commit(
        self,
        message: str,
        *,
        author: tuple[str, str] = MAINTAINER,
        committer: tuple[str, str] | None = None,
    ) -> str:
        committer = committer or author
        env = {
            **self.env,
            "GIT_AUTHOR_NAME": author[0],
            "GIT_AUTHOR_EMAIL": author[1],
            "GIT_COMMITTER_NAME": committer[0],
            "GIT_COMMITTER_EMAIL": committer[1],
        }
        message_file = self.root.parent / f"{self.root.name}-message.txt"
        message_file.write_text(message + "\n", encoding="utf-8")
        subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "--cleanup=verbatim", "-F", str(message_file)],
            cwd=self.root,
            env=env,
            check=True,
        )
        return self.git("rev-parse", "HEAD")

    def raw_commit(self, body: bytes) -> str:
        """A commit object written byte for byte, on top of HEAD: what `git commit` refuses."""
        tree = self.git("rev-parse", "HEAD^{tree}")
        head = self.git("rev-parse", "HEAD")
        who = f"{MAINTAINER[0]} <{MAINTAINER[1]}> 1700000000 +0000"
        header = f"tree {tree}\nparent {head}\nauthor {who}\ncommitter {who}\n\n".encode()
        sha = self.git(
            "hash-object", "-t", "commit", "-w", "--literally", "--stdin", stdin=header + body
        )
        self.git("update-ref", "HEAD", sha)
        return sha

    def check_main(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return self.run("--mode", "main", "--range", f"{self.base}..HEAD", *extra)

    def check_pr(
        self, *extra: str, title: str = "feat: a change", count: int | None = None
    ) -> subprocess.CompletedProcess[str]:
        """The pull request's commits as the commits API returns them, one page per commit, with
        the event payload and the pull request's own commit count."""
        shas = self.git("rev-list", "--reverse", f"{self.base}..HEAD").split()
        return self.check_api(
            [self._api_commit(sha) for sha in shas], *extra, title=title, count=count
        )

    def check_api(
        self,
        items: list,
        *extra: str,
        title: str = "feat: a change",
        count: int | None = None,
        head: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """The given API items, with the event's head the last item's id unless one is named."""
        pages = "".join(json.dumps([item]) + "\n" for item in items)
        commits = self.root.parent / f"{self.root.name}-commits.json"
        commits.write_text(pages, encoding="utf-8")
        event = self.root.parent / f"{self.root.name}-event.json"
        event.write_text(json.dumps({"pull_request": {"title": title}}), encoding="utf-8")
        if head is None:
            last = items[-1] if items else None
            named = isinstance(last, dict) and isinstance(last.get("sha"), str)
            head = last["sha"] if named else "a" * 40
        return self.run(
            "--mode",
            "pr",
            "--commits-json",
            str(commits),
            "--expected-count",
            str(len(items) if count is None else count),
            "--expected-head",
            head,
            "--event",
            str(event),
            *extra,
        )

    def _api_commit(self, sha: str) -> dict:
        fields = self.git("log", "-1", "--format=%an%x00%ae%x00%cn%x00%ce", sha).split("\0")
        message = self.git("cat-file", "commit", sha).split("\n\n", 1)[1]
        return _api_item(sha, (fields[0], fields[1]), (fields[2], fields[3]), message)

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(_SCRIPT), "--maintainers", str(self.maintainers), *args],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )


def _api_item(sha: str, author: tuple[str, str], committer: tuple[str, str], message: str) -> dict:
    return {
        "sha": sha,
        "commit": {
            "author": {"name": author[0], "email": author[1]},
            "committer": {"name": committer[0], "email": committer[1]},
            "message": message.rstrip("\n"),
        },
    }


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path / "repo")


def _rules(done: subprocess.CompletedProcess[str]) -> list[int]:
    """The rule number of every violation line the check printed."""
    found = []
    for line in done.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] == "rule":
            found.append(int(parts[2]))
    return found


# ── A clean range ────────────────────────────────────────────────────────────────────────────


def test_a_clean_range_passes_in_both_modes(repo: Repo) -> None:
    repo.commit("feat: a change\n\nBody.\n\n" + _signoff(MAINTAINER))
    for done in (repo.check_main(), repo.check_pr()):
        assert done.returncode == 0, done.stdout + done.stderr
        assert _rules(done) == []


# ── Rule 1: a sign-off that matches the author ───────────────────────────────────────────────


def test_an_unsigned_commit_fails_rule_1_in_both_modes(repo: Repo) -> None:
    sha = repo.commit("feat: unsigned")
    for done in (repo.check_main(), repo.check_pr()):
        assert done.returncode == 1
        assert _rules(done) == [1]
        assert done.stdout.startswith(sha)


@pytest.mark.parametrize(
    "signed_as",
    [("Ada Other", "ada@example.com"), ("Ada Maintainer", "someone@example.com")],
    ids=["another-name", "another-email"],
)
def test_a_signoff_that_is_not_the_author_fails_rule_1(
    repo: Repo, signed_as: tuple[str, str]
) -> None:
    repo.commit("feat: signed by someone else\n\n" + _signoff(signed_as))
    done = repo.check_main()
    assert done.returncode == 1 and _rules(done) == [1]
    assert "Ada Maintainer <ada@example.com>" in done.stdout


def test_a_signoff_in_the_middle_of_a_line_is_not_one(repo: Repo) -> None:
    repo.commit(f"feat: a change\n\nAs required: {_signoff(MAINTAINER)} was meant here.")
    for done in (repo.check_main(), repo.check_pr()):
        assert done.returncode == 1 and _rules(done) == [1], done.stdout


def test_a_dependabot_commit_without_a_signoff_is_skipped(repo: Repo) -> None:
    repo.commit("build(deps): bump a thing", author=DEPENDABOT, committer=MAINTAINER)
    for done in (repo.check_main(), repo.check_pr()):
        assert done.returncode == 0, done.stdout


@pytest.mark.parametrize(
    "author",
    [
        ("Robot Builder", "robot@example.org"),
        ("dependabot[bot]", "bot@example.org"),
        ("dependabot[bot]", "Dependabot@users.noreply.github.com"),
        ("dependabot[bot]", "1+dependabot[bot]@users.noreply.github.com"),
        ("dependabot[bot]", "49699333+DEPENDABOT[bot]@users.noreply.github.com"),
    ],
    ids=["bot-in-the-name", "its-name-another-email", "a-lookalike", "another-id", "another-case"],
)
def test_the_dependabot_skip_is_its_one_exact_address(repo: Repo, author: tuple[str, str]) -> None:
    """The skip reads the author's email and nothing else, and only Dependabot's exact address
    passes. Any author can set that address, so the skip is as narrow as it can be made: no
    name, no other id, no other case, no lookalike."""
    repo.commit("build(deps): bump a thing", author=author, committer=MAINTAINER)
    for done in (repo.check_main(), repo.check_pr()):
        assert done.returncode == 1 and _rules(done) == [1], done.stdout


# ── Rule 2: a committer MAINTAINERS lists (main only) ────────────────────────────────────────


def test_a_committer_not_in_maintainers_fails_rule_2_on_main_only(repo: Repo) -> None:
    repo.commit("feat: a contributor's own\n\n" + _signoff(CONTRIBUTOR), author=CONTRIBUTOR)
    main = repo.check_main()
    assert main.returncode == 1 and _rules(main) == [2]
    assert "Cy Contributor <cy@example.org>" in main.stdout
    pr = repo.check_pr()
    assert pr.returncode == 0, pr.stdout


@pytest.mark.parametrize(
    "committer",
    [("Someone Else", "ada@example.com"), ("Ada Maintainer", "ada@example.org")],
    ids=["listed-email-another-name", "listed-name-another-email"],
)
def test_rule_2_matches_the_whole_listed_identity(repo: Repo, committer: tuple[str, str]) -> None:
    repo.commit("feat: a change\n\n" + _signoff(MAINTAINER), committer=committer)
    done = repo.check_main()
    assert done.returncode == 1 and _rules(done) == [2], done.stdout


# ── Rule 3: no co-author or tool-session marks on a maintainer's commit ─────────────────────


@pytest.mark.parametrize(
    "mark",
    [
        "Co-authored-by: Dee Other <dee@example.net>",
        "co-authored-by: Dee Other <dee@example.net>",
        "Session-Id: 0f3c9a",
        "Agent-Session: 0f3c9a",
        "X-Session-Url: https://tool.example.com/x",
        "Generated-by: some-tool 1.2",
        "Assisted-by: some-tool",
        "🤖 Generated with some-tool",
        "Generated by some-tool",
        "See https://tool.example.com/sessions/0f3c9a for the run.",
        "https://tool.example.com/code/session_01AbCdEf",
        "https://tool.example.com/share/0f3c9a",
        "https://tool.example.com/chat/0f3c9a",
        "https://chat.example.com/c/0f3c9a",
        "https://tool.example.com/c/conversation/0f3c9a",
    ],
)
def test_a_maintainer_commit_carrying_a_tool_or_co_author_mark_fails_rule_3(
    repo: Repo, mark: str
) -> None:
    repo.commit(f"feat: a change\n\n{mark}\n{_signoff(MAINTAINER)}")
    for done in (repo.check_main(), repo.check_pr()):
        assert done.returncode == 1, done.stdout
        assert _rules(done) == [3], done.stdout


@pytest.mark.parametrize(
    "line",
    [
        "The policy refuses a Co-authored-by: line on our commits.",
        "Possession: the store keeps the session it opened.",
        "Sessions are listed in https://example.com/docs/sessions.md now.",
        "Chat history lives in https://example.com/guide/chat-history.",
    ],
    ids=["a-trailer-quoted-mid-line", "possession", "a-page-about-sessions", "a-chat-page"],
)
def test_body_prose_that_only_names_these_words_is_not_a_mark(repo: Repo, line: str) -> None:
    repo.commit(f"docs: explain\n\n{line}\n\n" + _signoff(MAINTAINER))
    done = repo.check_main()
    assert done.returncode == 0, done.stdout


@pytest.mark.parametrize(
    "author",
    [("ADA MAINTAINER", "ada@elsewhere.example.org"), ("Ada M.", "ADA@EXAMPLE.COM")],
    ids=["listed-name-any-case", "listed-email-any-case"],
)
def test_rule_3_knows_a_maintainer_by_name_or_email(repo: Repo, author: tuple[str, str]) -> None:
    """Rule 3 is about the person, so a maintainer committing under another address, or the
    listed address under another spelling of the name, is still one."""
    repo.commit(
        f"feat: a change\n\nCo-authored-by: Dee Other <dee@example.net>\n{_signoff(author)}",
        author=author,
        committer=MAINTAINER,
    )
    for done in (repo.check_main(), repo.check_pr()):
        assert done.returncode == 1 and _rules(done) == [3], done.stdout


def test_a_contributors_own_co_author_passes(repo: Repo) -> None:
    repo.commit(
        "feat: pairing\n\nCo-authored-by: Dee Other <dee@example.net>\n" + _signoff(CONTRIBUTOR),
        author=CONTRIBUTOR,
        committer=MAINTAINER,
    )
    for done in (repo.check_main(), repo.check_pr()):
        assert done.returncode == 0, done.stdout


# ── Rule 5: no marker that starts no run ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "marker",
    ["[skip ci]", "[ci skip]", "[no ci]", "[skip actions]", "[actions skip]", "[SKIP CI]"],
)
def test_a_skip_marker_in_any_commit_message_fails_rule_5(repo: Repo, marker: str) -> None:
    """GitHub starts no run for a push whose head commit carries one, and the next push checks
    only its own range, so a marker anywhere in what lands is refused."""
    repo.commit(f"feat: a change {marker}\n\n" + _signoff(MAINTAINER))
    repo.commit("feat: the next one\n\n" + _signoff(MAINTAINER))
    for done in (repo.check_main(), repo.check_pr()):
        assert done.returncode == 1 and _rules(done) == [5], done.stdout


def test_a_skip_checks_trailer_fails_rule_5(repo: Repo) -> None:
    repo.commit(f"feat: a change\n\nskip-checks: true\n{_signoff(MAINTAINER)}")
    assert _rules(repo.check_main()) == [5]


def test_a_skip_marker_in_the_pull_request_title_fails_rule_5(repo: Repo) -> None:
    """A squash merge's message starts with the title, so the title is checked too."""
    repo.commit("feat: a change\n\n" + _signoff(MAINTAINER))
    done = repo.check_pr(title="feat: a change [ci skip]")
    assert done.returncode == 1 and _rules(done) == [5], done.stdout
    assert done.stdout.startswith("pull-request rule 5 ")


# ── The two ways GitHub lands a pull request ─────────────────────────────────────────────────


def test_a_rebase_merge_keeps_the_message_and_the_maintainer_commits_it(repo: Repo) -> None:
    """GitHub's rebase merge re-commits each commit with the merging maintainer as committer
    and the message unchanged, so rules 1 and 3 see the pull request's own commit."""
    repo.commit(
        "feat: a contributor's change\n\n" + _signoff(CONTRIBUTOR),
        author=CONTRIBUTOR,
        committer=MAINTAINER,
    )
    assert repo.check_main().returncode == 0


def test_a_squash_merge_is_committed_by_the_merge_identity(repo: Repo) -> None:
    """GitHub's squash merge writes one commit authored by the pull request's author and
    committed by GitHub, with a message built from the commits and a co-author line for each
    other author. That passes: the author signed off, GitHub is listed, and the co-author is
    on a contributor's commit."""
    repo.commit(
        "feat: a contributor's change (#7)\n\n"
        f"{_signoff(CONTRIBUTOR)}\n{_signoff(MAINTAINER)}\n"
        "Co-authored-by: Dee Other <dee@example.net>",
        author=CONTRIBUTOR,
        committer=MERGER,
    )
    assert repo.check_main().returncode == 0


def test_a_squash_message_without_the_authors_signoff_fails_rule_1(repo: Repo) -> None:
    repo.commit(
        "feat: a contributor's change (#7)\n\n" + _signoff(MAINTAINER),
        author=CONTRIBUTOR,
        committer=MERGER,
    )
    done = repo.check_main()
    assert done.returncode == 1 and _rules(done) == [1]


def test_a_maintainer_squash_that_gained_a_co_author_fails_rule_3(repo: Repo) -> None:
    """A maintainer's pull request that carries another author's commit gains a co-author line
    when it is squashed, and rule 3 refuses that on the push to main: trim the line at merge."""
    repo.commit(
        f"feat: a change (#8)\n\n{_signoff(MAINTAINER)}\n"
        "Co-authored-by: Dee Other <dee@example.net>",
        author=MAINTAINER,
        committer=MERGER,
    )
    done = repo.check_main()
    assert done.returncode == 1 and _rules(done) == [3]


# ── A merge commit on main ───────────────────────────────────────────────────────────────────


def test_a_push_that_brings_a_merge_is_checked_on_both_parents_and_the_merge(repo: Repo) -> None:
    """A push to main can bring a merge commit: every commit it brings is checked, the merge
    itself and the commits only its second parent reaches, not the first-parent line alone."""
    repo.git("checkout", "-q", "-b", "side")
    reached_by_the_merge = repo.commit("feat: on a side branch")
    repo.git("checkout", "-q", "main")
    repo.commit("feat: on main\n\n" + _signoff(MAINTAINER))
    who = {
        "GIT_AUTHOR_NAME": MAINTAINER[0],
        "GIT_AUTHOR_EMAIL": MAINTAINER[1],
        "GIT_COMMITTER_NAME": MAINTAINER[0],
        "GIT_COMMITTER_EMAIL": MAINTAINER[1],
    }
    subprocess.run(
        ["git", "merge", "-q", "--no-ff", "-m", "merge side", "side"],
        cwd=repo.root,
        env={**repo.env, **who},
        capture_output=True,
        check=True,
    )
    merge = repo.git("rev-parse", "HEAD")
    assert repo.git("rev-parse", "HEAD^2") == reached_by_the_merge
    done = repo.check_main()
    assert done.returncode == 1, done.stdout + done.stderr
    named = sorted(line.split()[0] for line in done.stdout.splitlines() if " rule 1 " in line)
    assert named == sorted([reached_by_the_merge, merge]), done.stdout


# ── Output: one line per violation, and no line a commit can start ──────────────────────────


def test_every_violation_gets_its_own_line_and_the_report_file_holds_them(
    repo: Repo, tmp_path: Path
) -> None:
    repo.commit("feat: two problems\n\nSession-Id: 1", author=MAINTAINER, committer=OTHER)
    report = tmp_path / "report.txt"
    done = repo.check_main("--report", str(report))
    assert done.returncode == 1
    assert sorted(_rules(done)) == [1, 2, 3]
    assert report.read_text(encoding="utf-8").splitlines() == done.stdout.splitlines()


def test_every_violation_is_also_an_annotation_on_its_own_line(repo: Repo) -> None:
    """A red run's summary names the commit: each violation is printed again as `::error::`,
    and a name planted with a newline still makes one annotation, not a second command."""
    sha = "d" * 40
    name = "Cy\n::warning::planted"
    done = repo.check_api([_api_item(sha, (name, "cy@example.org"), MAINTAINER, "feat: x")])
    assert done.returncode == 1
    annotations = [line for line in done.stderr.splitlines() if line.startswith("::")]
    assert annotations == [f"::error::{line}" for line in done.stdout.splitlines()], done.stderr


def test_a_clean_run_writes_an_empty_report(repo: Repo, tmp_path: Path) -> None:
    repo.commit("feat: fine\n\n" + _signoff(MAINTAINER))
    report = tmp_path / "report.txt"
    assert repo.check_main("--report", str(report)).returncode == 0
    assert report.read_text(encoding="utf-8") == ""


@pytest.mark.parametrize(
    "name",
    ["Cy\r::error::planted", "Cy\n::warning::planted", "Cy\u2028::error::planted", "Cy\x1b[2Jx"],
    ids=["carriage-return", "newline", "line-separator", "escape"],
)
def test_no_field_a_commit_supplies_can_start_a_line(repo: Repo, name: str) -> None:
    """The runner reads a line that starts `::` as a workflow command, so every field a commit
    supplies is printed with its control and line-break characters collapsed."""
    sha = "a" * 40
    item = _api_item(sha, (name, "cy@example.org"), (name, "cy@example.org"), "feat: x")
    done = repo.check_api([item])
    assert done.returncode == 1 and _rules(done) == [1]
    assert all(line.startswith(f"{sha} rule ") for line in done.stdout.splitlines()), done.stdout
    assert not any(ch in done.stdout for ch in "\r\x1b\u2028")
    assert "\n::" not in done.stdout and not done.stdout.startswith("::")


def test_a_record_separator_in_a_message_is_read_as_text(repo: Repo) -> None:
    repo.commit("feat: a change\x1e with a separator\n\n" + _signoff(MAINTAINER))
    done = repo.check_main()
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize(
    "record",
    [b"a" * 40 + b"\x00Ada Maintainer", b"b" * 40 + b"\x00A\x00a@x\x00A\x00a@x\x00message"],
    ids=["short-of-its-fields", "another-commits-record"],
)
def test_a_record_git_could_not_format_whole_cannot_be_checked(
    monkeypatch: pytest.MonkeyPatch, record: bytes
) -> None:
    """git log formats one commit's six fields, the commit's own id first. A record short of
    its fields, or naming another commit, is refused rather than read as a commit. Once the NUL
    refusal runs first, no commit git writes reaches this, so git is stood in for here."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("check_commit_provenance", _SCRIPT)
    assert spec and spec.loader
    provenance = importlib.util.module_from_spec(spec)
    # A dataclass looks its module up by name while it is built, so the module is registered
    # first, and only for this test.
    monkeypatch.setitem(sys.modules, spec.name, provenance)
    spec.loader.exec_module(provenance)
    sha = "a" * 40
    replies = {"rev-list": sha.encode(), "cat-file": b"tree t\n\nmsg\n", "log": record}

    def git(*args: str) -> bytes:
        return replies.get(args[0], b"")

    monkeypatch.setattr(provenance, "_git", git)
    with pytest.raises(provenance.Unusable, match="not 6"):
        provenance.commits_in_range(f"{'c' * 40}..{sha}")


def test_a_commit_whose_object_holds_a_nul_cannot_be_checked(repo: Repo) -> None:
    """git's formatted message stops at a NUL, so a co-author after one would be read past.
    Such a commit is refused outright."""
    repo.raw_commit(
        f"feat: a change\n\n{_signoff(MAINTAINER)}\n".encode()
        + b"\x00Co-authored-by: Dee Other <dee@example.net>\n"
    )
    done = repo.check_main()
    assert done.returncode == 2, done.stdout + done.stderr
    assert "NUL" in done.stderr


# ── Failing closed: exit 2, and no report ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "contents",
    ["Ada Maintainer <ada@example.com>\n", "no identity here  a reason\n", ""],
    ids=["no-reason", "no-identity", "empty"],
)
def test_an_unreadable_maintainers_file_fails_closed(repo: Repo, contents: str) -> None:
    repo.commit("feat: fine\n\n" + _signoff(MAINTAINER))
    repo.maintainers.write_text(contents, encoding="utf-8")
    done = repo.check_main()
    assert done.returncode == 2, done.stdout + done.stderr


def test_a_maintainers_line_without_its_reason_fails_closed_among_good_ones(repo: Repo) -> None:
    """Every identity says why it may commit on main. One line without a reason, beside lines
    that have one, is refused by number, and the commit it would admit is never judged."""
    repo.commit("feat: fine\n\n" + _signoff(MAINTAINER))
    repo.maintainers.write_text(
        _MAINTAINERS_FILE + "Cy Contributor <cy@example.org>\n", encoding="utf-8"
    )
    done = repo.check_main()
    assert done.returncode == 2, done.stdout + done.stderr
    # The message is printed with whitespace runs collapsed, as every line the check prints is.
    assert "MAINTAINERS:4: expected `Name <email> reason`" in done.stderr, done.stderr


def test_a_missing_maintainers_file_fails_closed(repo: Repo) -> None:
    repo.commit("feat: fine\n\n" + _signoff(MAINTAINER))
    repo.maintainers.unlink()
    assert repo.check_main().returncode == 2


def test_the_maintainers_file_is_always_named(repo: Repo) -> None:
    """There is no default: the workflow that runs the check names the copy it trusts."""
    done = subprocess.run(
        [sys.executable, str(_SCRIPT), "--mode", "main", "--range", f"{repo.base}..HEAD"],
        cwd=repo.root,
        env=repo.env,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 2


def test_a_check_that_cannot_run_writes_no_report_and_removes_a_stale_one(
    repo: Repo, tmp_path: Path
) -> None:
    repo.commit("feat: unsigned")
    report = tmp_path / "report.txt"
    report.write_text("stale line from an earlier run\n", encoding="utf-8")
    repo.maintainers.write_text("", encoding="utf-8")
    done = repo.check_main("--report", str(report))
    assert done.returncode == 2
    assert not report.exists()


@pytest.mark.parametrize(
    "bad_range",
    ["0000000000000000000000000000000000000000..HEAD", "nonexistent..HEAD", "HEAD..HEAD"],
    ids=["branch-creation", "unknown-commit", "empty"],
)
def test_a_range_with_nothing_to_check_fails_closed(repo: Repo, bad_range: str) -> None:
    repo.commit("feat: fine\n\n" + _signoff(MAINTAINER))
    done = repo.run("--mode", "main", "--range", bad_range)
    assert done.returncode == 2, done.stdout + done.stderr
    if bad_range.startswith("0000"):
        assert "all zeros" in done.stderr, done.stderr


@pytest.mark.parametrize(
    "text",
    ["", "[]\n", "{not json\n", '[{"sha": "abc"}]\n', '{"sha": "x"}\n'],
    ids=["empty", "no-commits", "not-json", "no-fields", "not-a-page"],
)
def test_an_empty_or_malformed_commit_list_fails_closed(repo: Repo, text: str) -> None:
    commits = repo.root.parent / "commits.json"
    commits.write_text(text, encoding="utf-8")
    event = repo.root.parent / "event.json"
    event.write_text('{"pull_request": {"title": "t"}}', encoding="utf-8")
    done = repo.run(
        "--mode",
        "pr",
        "--commits-json",
        str(commits),
        "--expected-count",
        "1",
        "--expected-head",
        "a" * 40,
        "--event",
        str(event),
    )
    assert done.returncode == 2, (done.stdout, done.stderr)
    assert "--expected-head" not in done.stderr, done.stderr


@pytest.mark.parametrize(
    "bad",
    [
        {"sha": "b" * 40},
        {"sha": "not-a-sha", "commit": _api_item("x", MAINTAINER, MAINTAINER, "m")["commit"]},
        {
            "sha": "b" * 40,
            "commit": {**_api_item("x", MAINTAINER, MAINTAINER, "m")["commit"], "message": 7},
        },
    ],
    ids=["no-commit", "not-a-sha", "a-message-that-is-not-text"],
)
def test_good_commits_beside_a_malformed_one_fail_closed(repo: Repo, bad: dict) -> None:
    good = _api_item("a" * 40, MAINTAINER, MAINTAINER, "feat: x\n\n" + _signoff(MAINTAINER))
    done = repo.check_api([good, bad, good])
    assert done.returncode == 2, (done.stdout, done.stderr)


@pytest.mark.parametrize("count", [2, 251], ids=["one-short", "past-the-api-limit"])
def test_a_commit_list_short_of_the_pull_requests_count_fails_closed(
    repo: Repo, count: int
) -> None:
    """The commits endpoint returns at most 250 and pagination then stops without an error, so
    a list shorter than the pull request's own count is refused rather than half-checked."""
    repo.commit("feat: fine\n\n" + _signoff(MAINTAINER))
    done = repo.check_pr(count=count)
    assert done.returncode == 2, (done.stdout, done.stderr)
    assert "at most 250" in done.stderr


def _signed(sha: str, subject: str) -> dict:
    return _api_item(sha, MAINTAINER, MAINTAINER, f"{subject}\n\n" + _signoff(MAINTAINER))


@pytest.mark.parametrize(
    ("order", "head"),
    [
        (["a", "b"], "c" * 40),  # truncated: the head's own commit never arrived
        (["b", "a"], "b" * 40),  # reordered: the head is in the list, but not last
        (["a", "b"], "b" * 39),  # not a full commit id
    ],
    ids=["truncated", "reordered", "head-not-a-sha"],
)
def test_a_commit_list_that_does_not_end_at_the_events_head_fails_closed(
    repo: Repo, order: list[str], head: str
) -> None:
    """The commits API answers for the pull request as it is when asked, so a list from after a
    later push, or one cut short or reordered, is refused rather than checked as this head's."""
    report = repo.root.parent / "report.txt"
    items = [_signed(c * 40, f"feat: {c}") for c in order]
    done = repo.check_api(items, "--report", str(report), head=head)
    assert done.returncode == 2, (done.stdout, done.stderr)
    assert "head" in done.stderr, done.stderr
    assert not report.exists()


@pytest.mark.parametrize("head", ["", "b" * 39, "B" * 40], ids=["empty", "short", "upper-case"])
def test_a_head_that_is_not_a_commit_id_is_named_as_such(repo: Repo, head: str) -> None:
    """A head that is no commit id is the workflow's mistake (an empty or mistyped input), not
    a pull request that moved, so the refusal says which."""
    done = repo.check_api([_signed("b" * 40, "feat: b")], head=head)
    assert done.returncode == 2, (done.stdout, done.stderr)
    assert "--expected-head must be a full commit id" in done.stderr, done.stderr


#: Commit ids that are not a full one: the whole string is read, never a prefix of it. Under
#: ``re.match``, a ``$`` anchor also matches before a trailing newline.
_NOT_A_FULL_ID = [
    pytest.param("b" * 40 + "\n", id="a-trailing-newline"),
    pytest.param("b" * 40 + "\r\n", id="a-trailing-crlf"),
    pytest.param("b" * 39, id="39-hex"),
    pytest.param("b" * 41, id="41-hex"),
    pytest.param("B" * 40, id="upper-case"),
    pytest.param("b" * 39 + "B", id="one-upper-case-letter"),
]
assert len(_NOT_A_FULL_ID) == 6


@pytest.mark.parametrize("head", _NOT_A_FULL_ID)
def test_the_events_head_is_read_whole(repo: Repo, head: str) -> None:
    """``--expected-head`` is a full commit id end to end: a head with a newline after its 40 hex
    is the workflow's mistake, refused as such, not read as the list's last commit."""
    done = repo.check_api([_signed("b" * 40, "feat: b")], head=head)
    assert done.returncode == 2, (done.stdout, done.stderr)
    assert "--expected-head must be a full commit id" in done.stderr, done.stderr


@pytest.mark.parametrize("sha", _NOT_A_FULL_ID)
def test_a_commit_id_in_the_list_is_read_whole(repo: Repo, sha: str) -> None:
    """Each commit the API returns names a full commit id end to end, or the list is refused as
    one the API did not return, before any rule or the head is judged."""
    done = repo.check_api([_signed(sha, "feat: b")], head="b" * 40)
    assert done.returncode == 2, (done.stdout, done.stderr)
    assert "commit 0 in the list is not a commit the API returns" in done.stderr, done.stderr


def test_a_commit_list_ending_at_the_events_head_is_checked(repo: Repo) -> None:
    """The control for the head check: the same well-signed commits, ending at the head the
    event names, pass."""
    items = [_signed("a" * 40, "feat: a"), _signed("b" * 40, "feat: b")]
    done = repo.check_api(items, head="b" * 40)
    assert done.returncode == 0, (done.stdout, done.stderr)


def test_pr_mode_without_the_events_head_is_refused(repo: Repo) -> None:
    commits = repo.root.parent / "commits.json"
    commits.write_text(json.dumps([_signed("a" * 40, "feat: a")]) + "\n", encoding="utf-8")
    event = repo.root.parent / "event.json"
    event.write_text('{"pull_request": {"title": "t"}}', encoding="utf-8")
    done = repo.run(
        "--mode",
        "pr",
        "--commits-json",
        str(commits),
        "--expected-count",
        "1",
        "--event",
        str(event),
    )
    assert done.returncode == 2, (done.stdout, done.stderr)
    assert "--expected-head" in done.stderr, done.stderr


def test_main_mode_refuses_the_events_head(repo: Repo) -> None:
    repo.commit("feat: fine\n\n" + _signoff(MAINTAINER))
    done = repo.check_main("--expected-head", "a" * 40)
    assert done.returncode == 2, (done.stdout, done.stderr)


# ── Real commits on main ────────────────────────────────────────────────────────────────────


def test_the_repositorys_maintainers_file_lists_the_squash_merge_identity() -> None:
    lines = [
        line
        for line in (_REPO_ROOT / "MAINTAINERS").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert any(line.startswith("GitHub <noreply@github.com> ") for line in lines), lines
    assert all(len(line.split(">", 1)[1].strip()) > 0 for line in lines), lines



# ── The copy is core's, unchanged ───────────────────────────────────────────────────────────

#: The core commit whose ``scripts/check_commit_provenance.py`` this repository's copy is, and
#: that file's git blob id there: ``git rev-parse <commit>:scripts/check_commit_provenance.py``
#: in a core clone prints it.
CORE_COMMIT = "3c5c58c3ac0477464aeacef9352d7decbd324193"
CORE_BLOB = "73510bfb24f55771eb620c8739a7e660f857ed37"


def _blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def test_the_check_is_cores_copy_byte_for_byte() -> None:
    """One policy, one implementation: a rule changes in core first, and its new copy comes here
    with the core commit it came from named above."""
    assert _blob_id(_SCRIPT.read_bytes()) == CORE_BLOB, (
        f"scripts/check_commit_provenance.py is no longer core's copy at {CORE_COMMIT}"
    )


# ── The workflows' shape, each rule failing on a planted edit of the real file ──────────────

_DCO = ".github/workflows/dco.yml"
_PROVENANCE = ".github/workflows/provenance.yml"
#: Every pull request, and every edit of one: rule 5 reads the title, so a title edited after the
#: last push is checked again.
_DCO_TYPES = {"opened", "edited", "synchronize", "reopened"}
_DCO_GROUP = "dco-${{ github.event.pull_request.number }}"
_DCO_SPARSE = {"scripts/check_commit_provenance.py", "MAINTAINERS"}
_DCO_FETCH = "Fetch the pull request's commits"
_DCO_CHECK = "Verify DCO sign-off on every commit"
_DCO_RUN = "python3 -I scripts/check_commit_provenance.py --mode pr"
#: The one command provenance.yml's job runs: the fetched check, isolated, over the push's range.
_PROVENANCE_RUN = (
    'python3 -I "$check" --maintainers "$maintainers" '
    '--mode main --range "${BEFORE}..${GITHUB_SHA}"'
)
_TOKEN = re.compile(r"\$\{\{[^}]*\b(?:github\.token|secrets\.)")
#: What lets a job be skipped, replaced or cancelled on its own, or fail without failing the run.
_JOB_ESCAPES = {"concurrency", "if", "needs", "continue-on-error"}
#: What lets a step be skipped, or fail without failing its job.
_STEP_ESCAPES = {"if", "continue-on-error"}


def _texts(**edits: tuple[str, str]) -> dict[str, str]:
    """Every workflow's text by file name, with each planted edit (``file=(old, new)``) applied
    once. A plant whose anchor is gone fails here, so no plant can pass by missing its target."""
    texts = {
        path.name: path.read_text(encoding="utf-8") for path in sorted(_WORKFLOWS.glob("*.yml"))
    }
    for name, (old, new) in edits.items():
        name = name.replace("_", ".")
        assert texts[name].count(old) == 1, f"the plant's anchor is not once in {name}: {old!r}"
        texts[name] = texts[name].replace(old, new)
    return texts


def _triggers(workflow: dict) -> object:
    # YAML 1.1 reads a bare `on:` key as True.
    return workflow.get("on", workflow.get(True))


def _hands_token(node: object) -> bool:
    return bool(_TOKEN.search(json.dumps(node or {})))


def _checkouts(steps: list) -> list[dict]:
    return [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@")]


def _escapes(where: str, job: dict) -> list[str]:
    """A job that can be skipped or fail green, or a step of it that can: either leaves the check
    doing nothing while the run stays green."""
    bad = []
    if _JOB_ESCAPES & set(job):
        bad.append(f"{where} can be skipped, replaced or cancelled on its own")
    for step in job.get("steps") or []:
        if _STEP_ESCAPES & set(step):
            name = step.get("name") or step.get("uses")
            bad.append(f"{where}:{name} can be skipped, or fail without failing the job")
    return bad


def dco_violations(texts: dict[str, str]) -> list[str]:
    """``dco.yml``: on every pull request and on every edit of one, one run per pull request at a
    time, read-only, one job with no job or step that can be skipped or fail green, one checkout
    of the check and MAINTAINERS alone, the job token named on the step that runs ``gh api`` and
    on no other, and the check run isolated in ``pr`` mode, handed the pull request's own count,
    head and event file. No other workflow runs the pull request's check, and none runs on
    ``pull_request_target``."""
    bad: list[str] = []
    for name, text in texts.items():
        triggers = _triggers(yaml.safe_load(text) or {})
        if isinstance(triggers, str):
            triggers = [triggers]
        if isinstance(triggers, (dict, list)) and "pull_request_target" in triggers:
            bad.append(f"{name} runs on pull_request_target")
        if name != "dco.yml" and "--mode pr" in text and "check_commit_provenance" in text:
            bad.append(f"{name} runs the pull request's check, which only dco.yml may")
    if "dco.yml" not in texts:
        return [f"{_DCO} is missing", *bad]
    workflow = yaml.safe_load(texts["dco.yml"])
    on = _triggers(workflow)
    pr = on.get("pull_request") if isinstance(on, dict) else None
    types = pr.get("types") if isinstance(pr, dict) else None
    if not (
        isinstance(on, dict)
        and set(on) == {"pull_request"}
        and isinstance(pr, dict)
        and set(pr) == {"types"}
        and isinstance(types, list)
        and set(types) == _DCO_TYPES
    ):
        bad.append(f"{_DCO} runs on {on!r}, not on every pull request and every edit of one")
    concurrency = workflow.get("concurrency")
    group = concurrency.get("group") if isinstance(concurrency, dict) else concurrency
    if group != _DCO_GROUP:
        bad.append(f"{_DCO}'s group is not its pull request's own: {group!r}")
    if workflow.get("permissions") != {"contents": "read"}:
        bad.append(f"{_DCO} grants {workflow.get('permissions')!r}, not contents: read")
    jobs = workflow.get("jobs") or {}
    if set(jobs) != {"dco"}:
        bad.append(f"{_DCO} has the jobs {sorted(jobs)}, not the one dco job")
    job = jobs.get("dco")
    if not isinstance(job, dict):
        return bad
    where = f"{_DCO}:dco"
    if job.get("permissions") != {"contents": "read", "pull-requests": "read"}:
        bad.append(f"{where} holds {job.get('permissions')!r}, not contents and pull-requests: read")
    bad += _escapes(where, job)
    steps = job.get("steps") or []
    checkouts = _checkouts(steps)
    if len(checkouts) != 1:
        bad.append(f"{where} has {len(checkouts)} checkouts, not one")
    for checkout in checkouts:
        inputs = checkout.get("with") or {}
        if {"ref", "repository", "fetch-depth"} & set(inputs):
            bad.append(f"{where}'s checkout names a ref, a repository or a history")
        if inputs.get("persist-credentials") is not False:
            bad.append(f"{where}'s checkout keeps its credentials")
        if set(str(inputs.get("sparse-checkout", "")).split()) != _DCO_SPARSE:
            bad.append(f"{where}'s checkout is not the check and MAINTAINERS alone")
    by_name = {str(s.get("name", "")): s for s in steps}
    for step in steps:
        named = f"{where}:{step.get('name') or step.get('uses')}"
        if (_hands_token(step.get("env")) or _hands_token(step.get("with"))) and step.get(
            "name"
        ) != _DCO_FETCH:
            bad.append(f"{named} hands the job token to a step that does not fetch")
        if "${{" in str(step.get("run", "")):
            bad.append(f"{named} pastes an expression into code")
    fetch, check = by_name.get(_DCO_FETCH), by_name.get(_DCO_CHECK)
    if fetch is None or not _hands_token(fetch.get("env")):
        bad.append(f"{where} has no fetch step holding the token")
    else:
        commands = [line.split()[0] for line in str(fetch.get("run", "")).splitlines() if line]
        if commands != ["set", "gh"]:
            bad.append(f"{where}'s fetch step runs more than `gh api`: {commands}")
    if check is None:
        bad.append(f"{where} has no check step")
    else:
        run, env = str(check.get("run", "")), check.get("env") or {}
        if not run.lstrip().startswith("set -euo pipefail"):
            bad.append(f"{where}'s check step does not start with `set -euo pipefail`")
        if _DCO_RUN not in run:
            bad.append(f"{where}'s check step does not run the check, isolated, in pr mode")
        if (
            '--expected-count "$COMMITS"' not in run
            or env.get("COMMITS") != "${{ github.event.pull_request.commits }}"
        ):
            bad.append(f"{where}'s check is not handed the pull request's own commit count")
        if (
            '--expected-head "$HEAD_SHA"' not in run
            or env.get("HEAD_SHA") != "${{ github.event.pull_request.head.sha }}"
        ):
            bad.append(f"{where}'s check is not handed the head the event names")
        if '--event "$GITHUB_EVENT_PATH"' not in run:
            bad.append(f"{where}'s check does not read the title from the event file")
    return bad


def provenance_violations(texts: dict[str, str]) -> list[str]:
    """``provenance.yml`` runs on every push to main and nothing else, one group per commit and
    never cancelled, read-only, one job with no job or step that can be skipped or fail green,
    one checkout of the whole history keeping no credential, and runs the previous main commit's
    check and MAINTAINERS, isolated. No other workflow runs the check over a main push."""
    if "provenance.yml" not in texts:
        return [f"{_PROVENANCE} is missing"]
    workflow = yaml.safe_load(texts["provenance.yml"])
    bad: list[str] = []
    if _triggers(workflow) != {"push": {"branches": ["main"]}}:
        bad.append(f"{_PROVENANCE} runs on {_triggers(workflow)}, not on every push to main alone")
    concurrency = workflow.get("concurrency") or {}
    if concurrency.get("group") != "provenance-${{ github.sha }}":
        bad.append(f"{_PROVENANCE}'s group is not its commit's own: {concurrency.get('group')!r}")
    if concurrency.get("cancel-in-progress") is not False:
        bad.append(f"{_PROVENANCE} can cancel a run")
    if workflow.get("permissions") != {"contents": "read"}:
        bad.append(f"{_PROVENANCE} grants {workflow.get('permissions')!r}, not contents: read")
    jobs = workflow.get("jobs") or {}
    if len(jobs) != 1:
        bad.append(f"{_PROVENANCE} has {len(jobs)} jobs, not one")
    for job_id, job in jobs.items():
        where = f"{_PROVENANCE}:{job_id}"
        if job.get("permissions") != {"contents": "read"}:
            bad.append(f"{where} holds {job.get('permissions')!r}, not contents: read")
        bad += _escapes(where, job)
        steps = job.get("steps") or []
        checkouts = _checkouts(steps)
        if len(checkouts) != 1:
            bad.append(f"{where} has {len(checkouts)} checkouts, not one")
        for checkout in checkouts:
            inputs = checkout.get("with") or {}
            depth = inputs.get("fetch-depth")
            if type(depth) is not int or depth != 0:
                bad.append(f"{where}'s checkout does not fetch the whole history: {depth!r}")
            if inputs.get("persist-credentials") is not False:
                bad.append(f"{where}'s checkout keeps its credentials")
        run = "\n".join(str(step.get("run", "")) for step in steps)
        if _PROVENANCE_RUN not in run:
            bad.append(f"{where} does not run the fetched check over `before..sha`")
        for fetched in ("scripts/check_commit_provenance.py", "MAINTAINERS"):
            if f'git show "$BEFORE:{fetched}"' not in run:
                bad.append(f"{where} does not read {fetched} from the previous main commit")
        if re.search(r"python3?\s+(?:-\S+\s+)*(?:\./)?scripts/check_commit_provenance\.py", run):
            bad.append(f"{where} runs the pushed copy of the check by path")
    for name, text in texts.items():
        if name != "provenance.yml" and "--mode main" in text and "check_commit_provenance" in text:
            bad.append(f"{name} runs the main-push check, which only provenance.yml may")
    return bad


def test_the_committed_workflows_have_the_shape_the_check_needs() -> None:
    texts = _texts()
    assert dco_violations(texts) == []
    assert provenance_violations(texts) == []


_TOKEN_LINE = "          GH_TOKEN: ${{ github.token }}\n"
_DCO_TRIGGER = "on:\n  pull_request:\n"
_DCO_TYPES_LINE = "    types: [opened, edited, synchronize, reopened]\n"
_DCO_TIMEOUT = "    timeout-minutes: 5\n"
_DCO_FETCH_STEP = f"      - name: {_DCO_FETCH}\n"
_DCO_CHECK_STEP = f"      - name: {_DCO_CHECK}\n"
_A_SECOND_CHECKOUT = (
    "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1\n"
    "        with:\n"
    "          persist-credentials: false\n"
)
_ANOTHER_JOB = "  other:\n    runs-on: ubuntu-24.04\n    steps:\n      - run: 'true'\n"

_DCO_PLANTS = [
    pytest.param(
        "dco.yml",
        _DCO_TRIGGER,
        "on:\n  pull_request_target:\n",
        "not on every pull request and every edit of one",
        id="pull-request-target",
    ),
    pytest.param(
        "dco.yml",
        _DCO_TYPES_LINE,
        "    types: [opened, synchronize, reopened]\n",
        "not on every pull request and every edit of one",
        id="no-run-when-the-title-is-edited",
    ),
    pytest.param(
        "dco.yml",
        _DCO_TYPES_LINE,
        "    types: [closed]\n",
        "not on every pull request and every edit of one",
        id="a-run-only-when-closed",
    ),
    pytest.param(
        "dco.yml",
        _DCO_TYPES_LINE,
        _DCO_TYPES_LINE + "    paths-ignore: ['**/*.md', 'scripts/**']\n",
        "not on every pull request and every edit of one",
        id="a-paths-filter",
    ),
    pytest.param(
        "dco.yml",
        _DCO_TYPES_LINE,
        _DCO_TYPES_LINE + "    branches: [main]\n",
        "not on every pull request and every edit of one",
        id="a-branch-filter",
    ),
    pytest.param(
        "dco.yml",
        _DCO_TRIGGER,
        "on:\n  push:\n  pull_request:\n",
        "not on every pull request and every edit of one",
        id="a-push-trigger-beside-it",
    ),
    pytest.param(
        "dco.yml",
        f"  group: {_DCO_GROUP}\n",
        "  group: dco\n",
        "not its pull request's own",
        id="a-group-shared-across-pull-requests",
    ),
    pytest.param(
        "dco.yml",
        "permissions:\n  contents: read\n\njobs",
        "permissions:\n  contents: write\n\njobs",
        "dco.yml grants {'contents': 'write'}",
        id="a-write-permission-on-the-workflow",
    ),
    pytest.param(
        "dco.yml",
        "      pull-requests: read   # list the pull request's commits\n",
        "      pull-requests: write\n",
        "not contents and pull-requests: read",
        id="a-write-permission-on-the-job",
    ),
    pytest.param(
        "dco.yml",
        "jobs:\n  dco:\n",
        "jobs:\n" + _ANOTHER_JOB + "  dco:\n",
        "not the one dco job",
        id="a-second-job",
    ),
    pytest.param(
        "dco.yml",
        _DCO_TIMEOUT,
        _DCO_TIMEOUT + "    if: false\n",
        "dco.yml:dco can be skipped",
        id="a-job-that-can-be-skipped",
    ),
    pytest.param(
        "dco.yml",
        _DCO_TIMEOUT,
        _DCO_TIMEOUT + "    needs: [other]\n",
        "dco.yml:dco can be skipped",
        id="a-job-that-waits-on-another",
    ),
    pytest.param(
        "dco.yml",
        _DCO_TIMEOUT,
        _DCO_TIMEOUT + "    continue-on-error: true\n",
        "dco.yml:dco can be skipped",
        id="a-job-that-can-fail-green",
    ),
    pytest.param(
        "dco.yml",
        _DCO_CHECK_STEP,
        _DCO_CHECK_STEP + "        if: false\n",
        f"{_DCO_CHECK} can be skipped",
        id="a-check-step-that-can-be-skipped",
    ),
    pytest.param(
        "dco.yml",
        _DCO_CHECK_STEP,
        _DCO_CHECK_STEP + "        continue-on-error: true\n",
        f"{_DCO_CHECK} can be skipped, or fail without failing the job",
        id="a-check-step-that-can-fail-green",
    ),
    pytest.param(
        "dco.yml",
        _DCO_FETCH_STEP,
        _DCO_FETCH_STEP + "        if: false\n",
        f"{_DCO_FETCH} can be skipped",
        id="a-fetch-step-that-can-be-skipped",
    ),
    pytest.param(
        "dco.yml",
        "          sparse-checkout-cone-mode: false\n          persist-credentials: false\n",
        "          sparse-checkout-cone-mode: false\n          persist-credentials: true\n",
        "keeps its credentials",
        id="a-checkout-that-keeps-its-credentials",
    ),
    pytest.param(
        "dco.yml",
        "            MAINTAINERS\n          sparse-checkout-cone-mode",
        "            MAINTAINERS\n            scripts/\n          sparse-checkout-cone-mode",
        "not the check and MAINTAINERS alone",
        id="a-checkout-of-more-than-the-check",
    ),
    pytest.param(
        "dco.yml",
        "          sparse-checkout-cone-mode: false\n          persist-credentials: false\n",
        "          sparse-checkout-cone-mode: false\n          persist-credentials: false\n"
        "          ref: ${{ github.event.pull_request.head.sha }}\n",
        "names a ref",
        id="a-checkout-of-another-ref",
    ),
    pytest.param(
        "dco.yml",
        _DCO_FETCH_STEP,
        _A_SECOND_CHECKOUT + _DCO_FETCH_STEP,
        "has 2 checkouts, not one",
        id="a-second-checkout",
    ),
    pytest.param(
        "dco.yml",
        "          COMMITS: ${{ github.event.pull_request.commits }}\n",
        "          COMMITS: ${{ github.event.pull_request.commits }}\n" + _TOKEN_LINE,
        f"{_DCO_CHECK} hands the job token",
        id="the-token-on-the-check-step",
    ),
    pytest.param(
        "dco.yml",
        _TOKEN_LINE,
        "",
        "has no fetch step holding the token",
        id="a-fetch-step-without-the-token",
    ),
    pytest.param(
        "dco.yml",
        'gh api --paginate "repos/$REPO/pulls/$PR/commits" > "$RUNNER_TEMP/commits.json"\n',
        'gh api --paginate "repos/$REPO/pulls/$PR/commits" > "$RUNNER_TEMP/commits.json"\n'
        "          python3 -I scripts/check_commit_provenance.py --help\n",
        "runs more than `gh api`",
        id="the-check-run-in-the-fetch-step",
    ),
    pytest.param(
        "dco.yml",
        _DCO_CHECK_STEP,
        "      - name: Verify\n",
        "has no check step",
        id="no-check-step",
    ),
    pytest.param(
        "dco.yml",
        "          set -euo pipefail\n          python3 -I scripts/check_commit_provenance.py",
        "          python3 -I scripts/check_commit_provenance.py",
        "does not start with `set -euo pipefail`",
        id="a-check-step-that-goes-on-past-a-failure",
    ),
    pytest.param(
        "dco.yml",
        "          python3 -I scripts/check_commit_provenance.py --mode pr \\\n",
        "          python3 scripts/check_commit_provenance.py --mode pr \\\n",
        "does not run the check, isolated",
        id="the-check-not-isolated",
    ),
    pytest.param(
        "dco.yml",
        '            --expected-count "$COMMITS" \\\n',
        "            --expected-count 1 \\\n",
        "own commit count",
        id="a-count-that-is-not-the-pull-requests",
    ),
    pytest.param(
        "dco.yml",
        "          HEAD_SHA: ${{ github.event.pull_request.head.sha }}\n",
        "          HEAD_SHA: ${{ github.sha }}\n",
        "not handed the head the event names",
        id="a-head-that-is-not-the-events",
    ),
    pytest.param(
        "dco.yml",
        '            --expected-head "$HEAD_SHA" \\\n',
        '            --expected-head "${{ github.event.pull_request.head.sha }}" \\\n',
        "pastes an expression into code",
        id="an-expression-pasted-into-the-check",
    ),
    pytest.param(
        "dco.yml",
        '            --event "$GITHUB_EVENT_PATH"\n',
        "            --event /dev/null\n",
        "does not read the title from the event file",
        id="a-title-not-read-from-the-event",
    ),
    pytest.param(
        "ci.yml",
        "  audit:\n",
        "  dco-again:\n    runs-on: ubuntu-24.04\n    steps:\n      - run: python3 -I "
        "scripts/check_commit_provenance.py --mode pr\n\n  audit:\n",
        "ci.yml runs the pull request's check",
        id="the-pull-request-check-in-ci-yml",
    ),
    pytest.param(
        "ci.yml",
        "on:\n  pull_request:\n  push:\n",
        "on:\n  pull_request_target:\n  push:\n",
        "ci.yml runs on pull_request_target",
        id="pull-request-target-in-ci-yml",
    ),
]


@pytest.mark.parametrize(("file", "old", "new", "expect"), _DCO_PLANTS)
def test_dco_ymls_shape_fails_on_a_planted(file: str, old: str, new: str, expect: str) -> None:
    found = dco_violations(_texts(**{file.replace(".", "_"): (old, new)}))
    assert any(expect in line for line in found), found


def test_a_missing_dco_workflow_fails() -> None:
    texts = _texts()
    del texts["dco.yml"]
    assert dco_violations(texts) == [f"{_DCO} is missing"]


_PROVENANCE_CHECK_STEP = (
    "      - name: check every commit the push lands, with the previous commit's check\n"
)

_PROVENANCE_PLANTS = [
    pytest.param(
        "provenance.yml",
        "group: provenance-${{ github.sha }}",
        "group: provenance-${{ github.ref }}",
        "not its commit's own",
        id="a-group-shared-across-commits",
    ),
    pytest.param(
        "provenance.yml",
        "cancel-in-progress: false",
        "cancel-in-progress: true",
        "can cancel a run",
        id="a-run-that-can-be-cancelled",
    ),
    pytest.param(
        "provenance.yml",
        "    branches: [main]\n",
        '    branches: ["**"]\n',
        "not on every push to main alone",
        id="a-branch-filter-that-admits-more-than-main",
    ),
    pytest.param(
        "provenance.yml",
        '          python3 -I "$check" --maintainers "$maintainers" --mode main',
        "          python3 scripts/check_commit_provenance.py --maintainers MAINTAINERS --mode main",
        "runs the pushed copy of the check by path",
        id="the-pushed-copy-run-by-path",
    ),
    pytest.param(
        "provenance.yml",
        'git show "$BEFORE:scripts/check_commit_provenance.py" > "$check"',
        'cp scripts/check_commit_provenance.py "$check"',
        "does not read scripts/check_commit_provenance.py from the previous main commit",
        id="the-pushed-check-copied",
    ),
    pytest.param(
        "provenance.yml",
        'git show "$BEFORE:MAINTAINERS" > "$maintainers"',
        'git show "$GITHUB_SHA:MAINTAINERS" > "$maintainers"',
        "does not read MAINTAINERS from the previous main commit",
        id="the-pushed-maintainers-read",
    ),
    pytest.param(
        "provenance.yml",
        '--mode main --range "${BEFORE}..${GITHUB_SHA}"',
        '--mode main --range "${GITHUB_SHA}^..${GITHUB_SHA}"',
        "does not run the fetched check over `before..sha`",
        id="a-range-of-the-head-alone",
    ),
    pytest.param(
        "provenance.yml",
        "permissions:\n  contents: read\n\njobs",
        "permissions:\n  contents: write\n\njobs",
        "not contents: read",
        id="a-write-permission",
    ),
    pytest.param(
        "provenance.yml",
        "    permissions:\n      contents: read\n    steps:\n",
        "    permissions:\n      contents: write\n    steps:\n",
        "provenance.yml:provenance holds {'contents': 'write'}",
        id="a-write-permission-on-the-job",
    ),
    pytest.param(
        "provenance.yml",
        "\njobs:\n  provenance:\n",
        "\njobs:\n" + _ANOTHER_JOB + "  provenance:\n",
        "has 2 jobs, not one",
        id="a-second-job",
    ),
    pytest.param(
        "provenance.yml",
        "    timeout-minutes: 5\n",
        "    timeout-minutes: 5\n    needs: [other]\n",
        "can be skipped",
        id="a-job-that-waits-on-another",
    ),
    pytest.param(
        "provenance.yml",
        "    timeout-minutes: 5\n",
        "    timeout-minutes: 5\n    continue-on-error: true\n",
        "provenance.yml:provenance can be skipped",
        id="a-job-that-can-fail-green",
    ),
    pytest.param(
        "provenance.yml",
        _PROVENANCE_CHECK_STEP,
        _PROVENANCE_CHECK_STEP + "        if: false\n",
        "with the previous commit's check can be skipped",
        id="a-check-step-that-can-be-skipped",
    ),
    pytest.param(
        "provenance.yml",
        _PROVENANCE_CHECK_STEP,
        _PROVENANCE_CHECK_STEP + "        continue-on-error: true\n",
        "with the previous commit's check can be skipped, or fail without failing the job",
        id="a-check-step-that-can-fail-green",
    ),
    pytest.param(
        "provenance.yml",
        "          fetch-depth: 0\n",
        "",
        "does not fetch the whole history",
        id="a-shallow-checkout",
    ),
    pytest.param(
        "provenance.yml",
        "          fetch-depth: 0\n",
        "          fetch-depth: 1\n",
        "does not fetch the whole history: 1",
        id="a-checkout-one-commit-deep",
    ),
    pytest.param(
        "provenance.yml",
        "          persist-credentials: false\n",
        "          persist-credentials: true\n",
        "provenance's checkout keeps its credentials",
        id="a-checkout-that-keeps-its-credentials",
    ),
    pytest.param(
        "provenance.yml",
        _PROVENANCE_CHECK_STEP,
        _A_SECOND_CHECKOUT + _PROVENANCE_CHECK_STEP,
        "has 2 checkouts, not one",
        id="a-second-checkout",
    ),
    pytest.param(
        "ci.yml",
        "  audit:\n",
        "  provenance-again:\n    runs-on: ubuntu-24.04\n    steps:\n      - run: python3 "
        "scripts/check_commit_provenance.py --mode main --range x..y\n\n  audit:\n",
        "ci.yml runs the main-push check",
        id="the-main-push-check-in-ci-yml",
    ),
]


@pytest.mark.parametrize(("file", "old", "new", "expect"), _PROVENANCE_PLANTS)
def test_provenance_ymls_shape_fails_on_a_planted(file: str, old: str, new: str, expect: str) -> None:
    found = provenance_violations(_texts(**{file.replace(".", "_"): (old, new)}))
    assert any(expect in line for line in found), found


def test_a_missing_provenance_workflow_fails() -> None:
    texts = _texts()
    del texts["provenance.yml"]
    assert provenance_violations(texts) == [f"{_PROVENANCE} is missing"]


# ── dco.yml, run: the fetch step's file is the check step's input ─────────────────────────


def _dco_step(name: str) -> tuple[str, dict]:
    job = yaml.safe_load((_WORKFLOWS / "dco.yml").read_text(encoding="utf-8"))["jobs"]["dco"]
    step = next(s for s in job["steps"] if s.get("name") == name)
    return str(step["run"]), dict(step.get("env") or {})


def _python3_on(path: Path) -> str:
    """A PATH whose ``python3`` is this interpreter, as the runner image's own would be."""
    path.mkdir(parents=True, exist_ok=True)
    shim = path / "python3"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    return f"{path}{os.pathsep}{os.environ['PATH']}"


def _run_the_dco_job(tmp_path: Path, items: list[dict], count: int, head: str):
    """The fetch step, with a stand-in ``gh`` that answers with *items* as one page each, then
    the check step, in a checkout holding this repository's check and a scratch MAINTAINERS."""
    checkout = tmp_path / "checkout"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "scripts" / "check_commit_provenance.py").write_bytes(_SCRIPT.read_bytes())
    (checkout / "MAINTAINERS").write_text(_MAINTAINERS_FILE, encoding="utf-8")
    pages = tmp_path / "pages.json"
    pages.write_text("".join(json.dumps([item]) + "\n" for item in items), encoding="utf-8")
    bin_dir = tmp_path / "bin"
    path = _python3_on(bin_dir)
    gh = bin_dir / "gh"
    gh.write_text(
        '#!/bin/sh\necho "$GH_TOKEN $*" > "$GH_ARGS"\nexec cat "$PAGES"\n', encoding="utf-8"
    )
    gh.chmod(0o755)
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"title": "feat: a change"}}), encoding="utf-8")
    runner = tmp_path / "runner"
    runner.mkdir()
    base = {"PATH": path, "HOME": str(tmp_path), "RUNNER_TEMP": str(runner), "LC_ALL": "C"}
    fetch, fetch_env = _dco_step(_DCO_FETCH)
    assert set(fetch_env) == {"GH_TOKEN", "REPO", "PR"}
    fetched = subprocess.run(
        ["bash", "-c", fetch],
        cwd=checkout,
        env={
            **base,
            "GH_TOKEN": "planted-token",
            "REPO": "owner/apps",
            "PR": "7",
            "GH_ARGS": str(tmp_path / "gh-args"),
            "PAGES": str(pages),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert fetched.returncode == 0, fetched.stderr
    asked = (tmp_path / "gh-args").read_text(encoding="utf-8").split()
    assert asked == ["planted-token", "api", "--paginate", "repos/owner/apps/pulls/7/commits"]
    check, check_env = _dco_step(_DCO_CHECK)
    assert set(check_env) == {"COMMITS", "HEAD_SHA"}
    return subprocess.run(
        ["bash", "-c", check],
        cwd=checkout,
        env={**base, "COMMITS": str(count), "HEAD_SHA": head, "GITHUB_EVENT_PATH": str(event)},
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_the_dco_job_checks_the_commits_its_fetch_step_wrote(tmp_path: Path) -> None:
    signed = _api_item("a" * 40, MAINTAINER, MAINTAINER, "feat: a\n\n" + _signoff(MAINTAINER))
    unsigned = _api_item("b" * 40, CONTRIBUTOR, CONTRIBUTOR, "feat: b")
    clean = _run_the_dco_job(tmp_path / "clean", [signed], 1, "a" * 40)
    assert clean.returncode == 0, clean.stdout + clean.stderr
    red = _run_the_dco_job(tmp_path / "red", [signed, unsigned], 2, "b" * 40)
    assert red.returncode == 1, red.stdout + red.stderr
    assert red.stdout.startswith(f"{'b' * 40} rule 1 "), red.stdout


def test_the_dco_job_refuses_a_list_short_of_the_pull_requests_count(tmp_path: Path) -> None:
    signed = _api_item("a" * 40, MAINTAINER, MAINTAINER, "feat: a\n\n" + _signoff(MAINTAINER))
    done = _run_the_dco_job(tmp_path, [signed], 2, "a" * 40)
    assert done.returncode == 2, done.stdout + done.stderr
    assert "at most 250" in done.stderr


# ── provenance.yml, run: every push to main, with the previous main commit's check ─────────


def _provenance_run() -> tuple[str, dict]:
    [job] = yaml.safe_load((_WORKFLOWS / "provenance.yml").read_text("utf-8"))["jobs"].values()
    step = next(s for s in job["steps"] if "check_commit_provenance" in str(s.get("run", "")))
    return str(step["run"]), dict(step.get("env") or {})


class Pushes:
    """A scratch main whose commits carry stand-in checks, to run provenance.yml's own shell."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True)
        self.env = _scratch_env(root)
        self.git("init", "-q", "-b", "main")
        for key, value in (("user.name", MAINTAINER[0]), ("user.email", MAINTAINER[1])):
            self.git("config", key, value)
        self.git("config", "commit.gpgsign", "false")

    def git(self, *args: str) -> str:
        done = subprocess.run(
            ["git", *args], cwd=self.root, env=self.env, capture_output=True, text=True, check=True
        )
        return done.stdout.strip()

    def commit(self, files: dict[str, str | None], message: str = "c") -> str:
        for rel, text in files.items():
            path = self.root / rel
            if text is None:
                path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def push(self, before: str, after: str, tmp: Path) -> subprocess.CompletedProcess[str]:
        run, env = _provenance_run()
        assert set(env) == {"BEFORE"}
        (tmp / "runner").mkdir(exist_ok=True)
        return subprocess.run(
            ["bash", "-c", run],
            cwd=self.root,
            env={
                **self.env,
                "PATH": _python3_on(tmp / "bin"),
                "BEFORE": before,
                "GITHUB_SHA": after,
                "RUNNER_TEMP": str(tmp / "runner"),
                "GITHUB_STEP_SUMMARY": str(tmp / "summary.md"),
            },
            capture_output=True,
            text=True,
            timeout=60,
        )


def _stand_in(label: str, code: int) -> str:
    """A check that says which copy ran, how, and with which MAINTAINERS, then exits *code*."""
    return (
        "import sys\n"
        f"print('ran:{label}', 'isolated' if sys.flags.isolated else 'not-isolated')\n"
        "listed = open(sys.argv[sys.argv.index('--maintainers') + 1]).read().strip()\n"
        "print('maintainers:' + listed)\n"
        "print('args:' + ' '.join(sys.argv[3:]))\n"
        f"sys.exit({code})\n"
    )


_CHECK = "scripts/check_commit_provenance.py"


def test_a_push_is_judged_by_the_previous_commits_check_and_maintainers(tmp_path: Path) -> None:
    """The pushed commit edits the check to pass itself and adds its committer to MAINTAINERS.
    Neither is what runs: the previous commit's copies run, isolated (`python3 -I`)."""
    pushes = Pushes(tmp_path / "main")
    before = pushes.commit({_CHECK: _stand_in("before", 0), "MAINTAINERS": "before-list"})
    after = pushes.commit({_CHECK: _stand_in("pushed", 0), "MAINTAINERS": "pushed-list"})
    done = pushes.push(before, after, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "ran:before isolated" in done.stdout
    assert "maintainers:before-list" in done.stdout
    assert f"args:--mode main --range {before}..{after}" in done.stdout
    assert "pushed" not in done.stdout


def test_the_previous_commits_verdict_is_the_jobs(tmp_path: Path) -> None:
    pushes = Pushes(tmp_path / "main")
    before = pushes.commit({_CHECK: _stand_in("before", 1), "MAINTAINERS": "m"})
    after = pushes.commit({_CHECK: _stand_in("pushed", 0)})
    assert pushes.push(before, after, tmp_path).returncode == 1


def test_the_push_that_adds_the_check_is_judged_by_its_own_copy(tmp_path: Path) -> None:
    """The one push with no previous copy is the one that lands the check: the previous main
    commit's history never held it. The summary says so."""
    pushes = Pushes(tmp_path / "main")
    before = pushes.commit({"README": "x"})
    after = pushes.commit({_CHECK: _stand_in("pushed", 0), "MAINTAINERS": "pushed-list"})
    done = pushes.push(before, after, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "ran:pushed isolated" in done.stdout
    assert "never had the check" in (tmp_path / "summary.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "before",
    ["0000000000000000000000000000000000000000", "e" * 40],
    ids=["all-zeros", "a-commit-the-checkout-does-not-hold"],
)
def test_a_previous_commit_that_does_not_resolve_refuses_the_push(
    tmp_path: Path, before: str
) -> None:
    """A BEFORE the checkout cannot resolve (a branch re-created, or an old tip a forced update
    orphaned) is not one that never had the check: the job refuses it before choosing a copy."""
    pushes = Pushes(tmp_path / "main")
    pushes.commit({"README": "x"})
    after = pushes.commit({_CHECK: _stand_in("pushed", 0), "MAINTAINERS": "pushed-list"})
    done = pushes.push(before, after, tmp_path)
    assert done.returncode != 0, done.stdout + done.stderr
    assert "ran:" not in done.stdout


def test_a_previous_commit_that_lost_the_check_refuses_the_push(tmp_path: Path) -> None:
    """Deleting the check must not open a push that judges itself with its own copy."""
    pushes = Pushes(tmp_path / "main")
    pushes.commit({_CHECK: _stand_in("old", 0), "MAINTAINERS": "m"})
    before = pushes.commit({_CHECK: None})
    after = pushes.commit({_CHECK: _stand_in("pushed", 0)})
    done = pushes.push(before, after, tmp_path)
    assert done.returncode == 2, done.stdout + done.stderr
    assert "ran:" not in done.stdout


def _a_git_whose_log_fails(bin_dir: Path) -> None:
    """A ``git`` that hands every command but ``log`` to the real one, and fails ``log`` the way
    a git that cannot read the history does."""
    real = shutil.which("git")
    assert real, "no git on PATH"
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "git"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "log" ]; then echo "fatal: a stand-in failure" >&2; exit 128; fi\n'
        f'exec "{real}" "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)


@pytest.mark.parametrize("lost", [True, False], ids=["lost-the-check", "never-had-it"])
def test_a_history_git_cannot_read_refuses_the_push(tmp_path: Path, lost: bool) -> None:
    """Which copy runs turns on the previous main commit's history of the check. A git that
    cannot read that history has not found it empty: the job stops, and no copy runs."""
    pushes = Pushes(tmp_path / "main")
    if lost:
        pushes.commit({_CHECK: _stand_in("old", 0), "MAINTAINERS": "m"})
        before = pushes.commit({_CHECK: None})
    else:
        before = pushes.commit({"README": "x"})
    after = pushes.commit({_CHECK: _stand_in("pushed", 0), "MAINTAINERS": "pushed-list"})
    _a_git_whose_log_fails(tmp_path / "bin")
    done = pushes.push(before, after, tmp_path)
    assert done.returncode != 0, done.stdout + done.stderr
    assert "ran:" not in done.stdout
    summary = tmp_path / "summary.md"
    assert not summary.exists() or "never had" not in summary.read_text(encoding="utf-8")
