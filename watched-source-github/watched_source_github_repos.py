"""Which GitHub repositories the watcher's ``repos`` setting names.

Its own module, named after the app, because both the provider and the setup and doctor
steps read it, and a step that imported the provider by its bare name ``provider`` could get
another app's module of that name from the same process.
"""

from __future__ import annotations

import re

#: ``owner/name`` as GitHub spells it. Anything else is not a repository, and an entry like
#: ``../users`` must not be allowed to walk the request somewhere else on the API host, which
#: is also why a part that is only ``.`` or ``..`` is refused although the pattern allows dots.
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def parse_repos(raw: object) -> tuple[list[str], list[str]]:
    """``(repositories to watch, entries skipped as not owner/name)`` from the setting."""
    watched: list[str] = []
    skipped: list[str] = []
    for entry in str(raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        if _REPO_RE.match(entry) and not {".", ".."} & set(entry.split("/")):
            watched.append(entry)
        else:
            skipped.append(entry)
    return watched, skipped
