"""An installed app imports core only through ``personalclaw.sdk.*``, never its internals.

The apps side of the SDK boundary; core's ``tests/test_apps_import_boundary.py`` holds the same
line from its side. Every non-test ``*/*.py`` in the repository is parsed, and an absolute import
of ``personalclaw`` or any ``personalclaw.<x>`` other than ``personalclaw.sdk`` is a violation.
A missing symbol is promoted to an sdk submodule in core, never reached around.

The files read are the ones git knows of or would add: tracked, plus untracked and not ignored.
So a new app file is read before it is added, and an ignored folder (a virtualenv inside the
checkout, ``node_modules``, build output) is not, because nothing in it is published. Outside a
git checkout there is no such list, and the check refuses rather than reading every file.

Pure stdlib, no core install.

    python .github/scripts/check_boundary.py
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]


def candidate_files() -> list[pathlib.Path]:
    """Every ``*/*.py`` git tracks or would add, minus tests and caches."""
    out = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout
    files = set()
    for raw in out.split(b"\0"):
        if not raw:
            continue
        rel = pathlib.PurePosixPath(raw.decode("utf-8", "surrogateescape"))
        # The shape ``Path(".").rglob("*/*.py")`` matched: a .py inside some folder.
        if rel.suffix != ".py" or len(rel.parts) < 2:
            continue
        if "__pycache__" in rel.parts or rel.name.startswith("test_"):
            continue
        path = pathlib.Path(rel)
        if (ROOT / path).is_file():  # a tracked file deleted in the work tree is not read
            files.add(path)
    return sorted(files)


def main() -> int:
    try:
        files = candidate_files()
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", b"") or b""
        print(
            f"error: cannot list the repository's files with git ({exc}) "
            f"{detail.decode(errors='replace').strip()}\n"
            "Run this check inside a git checkout."
        )
        return 1

    violations = {}
    for f in files:
        tree = ast.parse((ROOT / f).read_text(encoding="utf-8"), filename=str(f))
        bad = []

        def check(mod):
            if mod and mod.startswith("personalclaw"):
                parts = mod.split(".")
                if not (len(parts) >= 2 and parts[1] == "sdk"):
                    bad.append(mod)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    check(a.name)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                check(node.module)
        if bad:
            violations[str(f)] = sorted(set(bad))
    if violations:
        for f, mods in sorted(violations.items()):
            print(f"  {f}: {mods}")
        print(
            "\nApps must import core only via personalclaw.sdk.* — promote the "
            "needed symbol to an sdk submodule instead of reaching around it."
        )
        return 1
    print("OK: all app imports go through personalclaw.sdk.*")
    return 0


if __name__ == "__main__":
    sys.exit(main())
