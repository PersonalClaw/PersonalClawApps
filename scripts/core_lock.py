"""``core.lock``: the one core commit every apps CI run installs, and its one reader.

``core.lock`` at the repository root holds exactly one line, a 40-character lowercase hex core
commit, and a newline. No branch, no tag, no short SHA: a re-run of an old CI run installs the
same core, so an apps result can be reproduced and bisected.

Commands (standard library only, so they run before any environment exists):

- ``read``: check the file and print the commit.
- ``export``: what CI's jobs do before they install core. Appends ``CORE_SHA`` and ``CORE_SPEC``
  (the install spec for that commit) to ``$GITHUB_ENV``, ``sha`` to ``$GITHUB_OUTPUT``, and prints
  the commit, so the log says which core a run tested.
- ``resolve REF``: turn a core branch, tag or full commit into one commit (the core-main canary
  tests one commit for its whole run); ``--export`` hands it on as ``export`` does.
- ``verify``: refuse when the core installed in this interpreter was built from another commit.
- ``bump``: compare core ``main``'s head with the lock and write both to the job summary. Opening
  or updating the one ``core.lock`` pull request needs the pin-refresh GitHub App, which does not
  exist yet; that half is deferred. Without the App's credentials the bump reports and opens
  nothing; with them it fails loudly, because the pull-request half is not built.

The bump never writes ``core.lock``. Only a reviewed pull request changes it.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "core.lock"
CORE_URL = "https://github.com/PersonalClaw/PersonalClaw"
_FULL_SHA = re.compile(r"[0-9a-f]{40}")


def parse(text: str) -> str:
    """The commit a ``core.lock`` text holds; ValueError when it is not exactly one full SHA."""
    if not text.endswith("\n") or text.count("\n") != 1:
        raise ValueError("core.lock must be exactly one line ending in a newline")
    sha = text[:-1]
    if not _FULL_SHA.fullmatch(sha):
        raise ValueError(
            f"core.lock must hold a 40-character lowercase hex core commit, not {sha!r}"
        )
    return sha


def read_lock(path: Path = LOCK) -> str:
    """The commit *path* pins; ValueError naming the file when it is missing or malformed."""
    try:
        text = path.read_bytes().decode("ascii")
    except OSError as exc:
        raise ValueError(f"{path}: cannot read core.lock ({exc.strerror or exc})") from exc
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path}: core.lock is not ASCII") from exc
    try:
        return parse(text)
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from exc


def spec(sha: str) -> str:
    return f"personalclaw @ git+{CORE_URL}@{sha}"


def installed_core() -> dict:
    """The core installed in this interpreter: version, the commit it was built from, and what
    the install asked for (a branch, a tag or a commit), as its ``direct_url.json`` records."""
    try:
        dist = importlib.metadata.distribution("personalclaw")
    except importlib.metadata.PackageNotFoundError:
        return {"version": None, "commit": None, "requested": None}
    vcs = (json.loads(dist.read_text("direct_url.json") or "{}").get("vcs_info") or {})
    return {"version": dist.version, "commit": vcs.get("commit_id"),
            "requested": vcs.get("requested_revision")}


def _append(variable: str, lines: list[str]) -> bool:
    target = os.environ.get(variable)
    if not target:
        return False
    with open(target, "a", encoding="utf-8") as fh:
        fh.writelines(line + "\n" for line in lines)
    return True


def _hand_on(sha: str, origin: str) -> None:
    print(f"core under test: {sha} (from {origin})")
    print(f"core install spec: {spec(sha)}")
    if not _append("GITHUB_ENV", [f"CORE_SHA={sha}", f"CORE_SPEC={spec(sha)}"]):
        print("(no GITHUB_ENV: printed only)")
    _append("GITHUB_OUTPUT", [f"sha={sha}"])


def resolve(ref: str, repo: str = CORE_URL) -> str:
    """One commit for *ref*: a full SHA as given, else the branch or tag *repo* has by that name."""
    if _FULL_SHA.fullmatch(ref.lower()):
        return ref.lower()
    candidates = (f"refs/heads/{ref}", f"refs/tags/{ref}^{{}}", f"refs/tags/{ref}")
    try:
        proc = subprocess.run(["git", "ls-remote", repo, f"refs/heads/{ref}", f"refs/tags/{ref}"],
                              capture_output=True, text=True, check=False)
    except OSError as exc:
        raise ValueError(f"cannot run git to resolve {ref!r}: {exc}") from exc
    if proc.returncode != 0:
        raise ValueError(f"git ls-remote {repo} failed resolving {ref!r}: {proc.stderr.strip()}")
    found = {}
    for line in proc.stdout.splitlines():
        sha, _, name = line.partition("\t")
        found[name] = sha
    for name in candidates:  # a branch first, then an annotated tag's commit, then a tag
        if name in found:
            return found[name]
    raise ValueError(f"{repo} has no branch or tag {ref!r}; name a branch, a tag or a full commit")


def _cmd_read(args) -> int:
    print(read_lock(args.file))
    return 0


def _cmd_export(args) -> int:
    _hand_on(read_lock(args.file), args.file.name)
    return 0


def _cmd_resolve(args) -> int:
    sha = resolve(args.ref, args.repo)
    if args.export:
        _hand_on(sha, f"{args.ref} at {args.repo}")
    else:
        print(sha)
    return 0


def _cmd_verify(args) -> int:
    want = read_lock(args.file)
    core = installed_core()
    if core["commit"] != want:
        print(
            f"core_lock: core.lock pins {want}, but the core installed in {sys.executable} is "
            f"version {core['version']}, commit {core['commit'] or 'not recorded'}. "
            f'Install the locked core with: uv pip install "{spec(want)}"',
            file=sys.stderr,
        )
        return 1
    print(f"installed core is the locked one: {want} (version {core['version']})")
    return 0


def _cmd_bump(args) -> int:
    pinned = read_lock(args.file)
    main = args.core_main.lower() if args.core_main else resolve("main", args.repo)
    if not _FULL_SHA.fullmatch(main):
        raise ValueError(f"--core-main must be a full commit, not {args.core_main!r}")
    state = "current" if main == pinned else "behind"
    has_app = args.app_credentials == "true"
    lines = [
        "### core.lock bump",
        "",
        "| | commit |",
        "|---|---|",
        f"| core.lock pins | `{pinned}` |",
        f"| core main is at | `{main}` |",
        "",
        f"The pin is **{state}**"
        + ("." if state == "current" else f": a bump would pin `{main}`."),
        "",
    ]
    if has_app:
        lines.append(
            "The pin-refresh App's credentials are configured, but the pull-request half of this "
            "workflow is not built yet, so nothing was opened. Build it before relying on the App."
        )
    else:
        lines.append(
            "The pin-refresh App's credentials are absent (that half is deferred): this run "
            "opened no pull request and pushed nothing."
        )
    text = "\n".join(lines) + "\n"
    print(text, end="")
    if not _append("GITHUB_STEP_SUMMARY", [text]):
        print("(no GITHUB_STEP_SUMMARY: printed only)")
    if has_app:
        print("::error::the pin-refresh App's credentials are configured, but this workflow "
              "cannot open the core.lock pull request yet; build its pull-request half first")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python scripts/core_lock.py", description=__doc__.split(
        "\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def with_file(p):
        p.add_argument("--file", type=Path, default=LOCK, help="the lock to read (default: core.lock)")
        return p

    with_file(sub.add_parser("read", help="print the locked commit")).set_defaults(func=_cmd_read)
    with_file(sub.add_parser("export", help="hand CI the locked commit")).set_defaults(
        func=_cmd_export)
    p = sub.add_parser("resolve", help="turn a core branch, tag or commit into one commit")
    p.add_argument("ref")
    p.add_argument("--repo", default=CORE_URL, help="the core repository (default: %(default)s)")
    p.add_argument("--export", action="store_true", help="hand the commit on, as `export` does")
    p.set_defaults(func=_cmd_resolve)
    with_file(sub.add_parser("verify", help="refuse unless the installed core is the locked one")
              ).set_defaults(func=_cmd_verify)
    p = with_file(sub.add_parser("bump", help="compare core main with the lock and report"))
    p.add_argument("--core-main", help="core main's commit (default: ask the core repository)")
    p.add_argument("--repo", default=CORE_URL, help="the core repository (default: %(default)s)")
    p.add_argument("--app-credentials", choices=("true", "false"), required=True,
                   help="whether the pin-refresh App's credentials are configured")
    p.set_defaults(func=_cmd_bump)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ValueError as exc:
        print(f"core_lock: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
