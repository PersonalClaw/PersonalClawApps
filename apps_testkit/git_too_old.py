"""A git too old for PersonalClaw's git to run, first on ``PATH``, for an app's tests.

PersonalClaw's git (``personalclaw.sdk.git.git_argv``) refuses a git older than 2.12, which
ignores some of the settings that stop a repository's own configuration from running a program.
An app that runs git says that refusal where it says a missing git. This stand-in answers
``git version`` with an old version and records every other run, so a test sees both what the app
said and that no git ran.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

#: What the stand-in reports.
OLD_VERSION = "2.11.0"

#: How PersonalClaw's refusal of it begins.
REFUSAL_START = f"PersonalClaw needs git 2.12 or newer, and this machine has git {OLD_VERSION}."


def put_old_git_on_path(tmp_path: Path, monkeypatch) -> Path:
    """Put the stand-in first on ``PATH``; return the file its runs other than ``version`` go to
    (absent while none has run)."""
    bin_dir = tmp_path / "old-git-bin"
    bin_dir.mkdir(exist_ok=True)
    ran = tmp_path / "old-git-ran"
    git = bin_dir / "git"
    git.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = version ] || [ "$1" = --version ]; then\n'
        f"  echo 'git version {OLD_VERSION}'\n"
        "  exit 0\n"
        "fi\n"
        f'echo "$*" >> {shlex.quote(str(ran))}\n'
        "exit 1\n",
        encoding="utf-8",
    )
    git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return ran
