"""The publication-hygiene rail reds on a planted internal reference, and the tree is clean.

The rail is ``.github/scripts/check_publication_hygiene.py``, which CI runs as a script. These
tests drive that same script, unmodified, against seeded git repositories: a copy sits at the
same place in each, so its ``git ls-files`` input is exactly the seeded tree and its denylist is
the shipped one.

The planted strings are the CONTROL entries that denylist carries for each kind, assembled at
runtime. This file is inside the rail's own input set, so a literal control would make the real
repository red, and the rail would be right.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github" / "scripts" / "check_publication_hygiene.py"

_PLANTED = {
    "word": "# Validated per the " + "ZZ" + "HYGIENECONTROL" + "ZZ" + " guidance.\n",
    "phrase": "# Sign in with " + "zzcontrol" + "head\n#   " + "zzcontrol" + "tail before a run.\n",
    "host": "See https://docs." + "zzcontrol" + ".invalid/page for the rule.\n",
    "code": "# (" + "ZZQ" + "-42 boundary validation)\n",
    "id": "# cited as ``" + "zzq" + "_" + "Ab3dE5gH7jK9mN" + "``\n",
}


def _seeded(tmp_path: Path, files: dict[str, str]) -> Path:
    """A real git repository holding the rail's script and exactly *files*, all tracked."""
    repo = tmp_path / "repo"
    script = repo / ".github" / "scripts" / SCRIPT.name
    script.parent.mkdir(parents=True)
    shutil.copy2(SCRIPT, script)
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "add", "-A", "-f"], cwd=repo, check=True)
    return repo


def _check(repo: Path) -> subprocess.CompletedProcess:
    script = repo / ".github" / "scripts" / SCRIPT.name
    return subprocess.run([sys.executable, str(script)], cwd=repo, capture_output=True, text=True)


def test_the_tracked_tree_is_fit_to_publish():
    """The real repository through the real script, exactly as CI runs it."""
    run = _check(ROOT)
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.startswith("OK: "), run.stdout


def test_a_clean_seeded_tree_is_green(tmp_path):
    """Vacuity. The seeded harness must be able to go green, or every red below proves nothing."""
    run = _check(_seeded(tmp_path, {"docs/notes.md": "Never trust an upload's declared type.\n"}))
    assert run.returncode == 0, run.stdout


@pytest.mark.parametrize("kind", sorted(_PLANTED))
def test_a_planted_internal_reference_reds(tmp_path, kind):
    """One positive control per kind. The planted line is line 2, and the phrase, wrapped across
    a line break, must be placed on its first word's line."""
    run = _check(_seeded(tmp_path, {"docs/notes.md": "An ordinary line.\n" + _PLANTED[kind]}))
    assert run.returncode == 1, run.stdout
    assert f"internal-reference: docs/notes.md:2 names a denied {kind} " in run.stdout, run.stdout


def test_a_planted_name_in_a_path_reds(tmp_path):
    """A file NAME is published exactly as its content is."""
    name = "docs/" + "zz" + "hygienecontrol" + "zz" + "-notes.md"
    run = _check(_seeded(tmp_path, {name: "Ordinary content.\n"}))
    assert run.returncode == 1, run.stdout
    assert f"internal-reference: {name} (its PATH)" in run.stdout, run.stdout
