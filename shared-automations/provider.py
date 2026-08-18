"""Shared Automations — a team's trigger rows served from one shared JSON file.

Point several machines' shared-automations at the same file (a synced folder, an NFS share, a
checked-out team repo) and everybody sees the same automations on their Automations page. Each
machine then arms and fires ONLY the rows whose ``author`` matches its own owner; everybody else's
rows render read-only and structurally cannot arm — core drops them before the arm path sees them,
so there is no code path on your machine that could decide to run a teammate's automation.

**Rows, never execution.** This store answers "which automations exist". It never observes anything,
never executes anything, and is never handed a fire, a payload, a run or a credential. Your local
harness does all the firing, under every one of its own gates (capability allowlist, budget, quiet
hours, kill switch, injection screen).

**Write-back is real, and it has to be.** When core fires one of your rows it writes the row's next
schedule back HERE — that is what makes a shared automation fire once rather than once per tick — so
``upsert`` genuinely persists and ``get`` genuinely reads back. Core verifies it: a store that
accepted the write and kept the old ``next_fire_at`` would be quarantined (its rows stop arming for
the rest of the process, and they say so in the log) rather than allowed to fire every tick forever.

**The file format is core's own.** ``{"version": 1, "triggers": [...], "saved_at": …}``, and a bare
JSON list is read too — so a team can share a copy of somebody's ``triggers.json`` unchanged, and a
row hand-written in a review is the same shape core writes. Every write is atomic (temp file in the
same directory, then ``os.replace``): a synced folder mid-write is the one thing worse than a
missing one.
"""

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from personalclaw.sdk.triggers import (
    LoadedTrigger,
    Trigger,
    TriggerStoreProvider,
    parse_trigger,
)

#: The file-format version this app writes. Matches core's ``triggers.json`` envelope so a shared
#: file and a local store are literally interchangeable.
STORE_VERSION = 1

#: Prefix for the in-place temp file an atomic write creates, so a half-written file in a synced
#: folder is recognizable as scratch rather than as somebody's automations.
_TMP_PREFIX = ".tmp-automations-"


class SharedAutomationsStore(TriggerStoreProvider):
    """A ``trigger`` provider backed by one shared JSON file of trigger rows."""

    name = "shared-automations"
    display_name = "Shared Automations"

    def __init__(self, path: str = "") -> None:
        # Expand ``~`` and ``$VARS`` so a configured "~/synced/team/automations.json" resolves. An
        # empty path stays empty: the store still constructs and simply serves no rows, because an
        # unconfigured app that refused to build would fail its own install.
        expanded = os.path.expandvars(os.path.expanduser(path)) if path else ""
        self._path = Path(expanded) if expanded else None
        # The stamp ``changed_on_disk`` compares against — (mtime_ns, size) rather than mtime alone,
        # because two writes inside one filesystem mtime tick are exactly what a synced folder does.
        self._stamp: tuple[int, int] = (0, 0)

    # ── the contract's read side ───────────────────────────────────────────────────────────

    @property
    def base_dir(self) -> Path:
        """Root for this store's own sidecars — the shared file's directory.

        Core does NOT put claims here: a claim describes a local run, and collecting every machine's
        in-flight state in a shared folder would make one member's fire look overlapping to another.
        Core roots claims at the local store instead, so this answer is about this app's own files.
        With nothing configured, the system temp dir — an unconfigured store serves no rows, so
        nothing is ever written beside it.
        """
        return self._path.parent if self._path else Path(tempfile.gettempdir())

    def load(self) -> list[LoadedTrigger]:
        """Every row in the shared file, INCLUDING broken ones, each carrying its parse issues.

        Broken rows are returned rather than dropped for the reason core returns them: a row with a
        typo'd cron must be visible and inert, not absent and mysterious. ``parse_trigger`` already
        forces ``enabled=False`` on an error row.
        """
        return [
            LoadedTrigger(trigger=trigger, issues=list(issues))
            for trigger, issues in (parse_trigger(row) for row in self._read_rows())
        ]

    def list_triggers(self, *, kind: str = "", include_broken: bool = True) -> list[Trigger]:
        """The rows as a flat list — a LISTING view, so every author's rows are included."""
        out = []
        for row in self.load():
            if kind and row.trigger.kind != kind:
                continue
            if not include_broken and not row.ok:
                continue
            out.append(row.trigger)
        return out

    def get(self, trigger_id: str) -> "LoadedTrigger | None":
        """One row by id, or None. Core reads this back after every write it routes here."""
        if not trigger_id:
            return None
        for row in self.load():
            if row.trigger.id == trigger_id:
                return row
        return None

    def changed_on_disk(self) -> bool:
        """Has another writer — a teammate's machine, a git pull, a hand edit — touched the file?

        Answered from (mtime_ns, size) because that is what this backend has. A network store would
        answer the same QUESTION from an etag; the contract is the question, not the file stat.
        """
        return self._current_stamp() != self._stamp

    # ── the contract's write side (core's reschedule lands here) ───────────────────────────

    def upsert(self, trigger: Trigger) -> Trigger:
        """Insert or replace one row by id, atomically. Returns the row as stored.

        This is the method core calls to persist a fired row's next schedule, so it really writes and
        :meth:`get` really shows the result. Read-modify-write on the whole file: the shared file has
        no per-row locking, and a teammate's concurrent write is resolved by the file itself being
        replaced atomically — last writer wins on the row, never on half a file.
        """
        rows = [r for r in self._read_rows() if str(r.get("id") or "") != trigger.id]
        rows.append(trigger.to_dict())
        self._write_rows(rows)
        return trigger

    def delete(self, trigger_id: str) -> bool:
        """Remove one row. Returns whether it was there — core verifies it is gone afterwards."""
        rows = self._read_rows()
        keep = [r for r in rows if str(r.get("id") or "") != trigger_id]
        if len(keep) == len(rows):
            return False
        self._write_rows(keep)
        return True

    # ── file plumbing ─────────────────────────────────────────────────────────────────────

    def _current_stamp(self) -> tuple[int, int]:
        """(mtime_ns, size) of the shared file, or (0, 0) when it is missing/unreachable."""
        if self._path is None:
            return (0, 0)
        try:
            st = self._path.stat()
        except OSError:
            return (0, 0)
        return (st.st_mtime_ns, st.st_size)

    def _read_rows(self) -> list[dict[str, Any]]:
        """Raw row dicts from the shared file. Returns [] for missing, unreachable or malformed.

        Never raises. A shared folder that is not mounted yet, a file mid-sync, or a teammate's
        broken hand-edit costs THIS app's rows for that pass and nothing else — core logs the empty
        read and keeps arming the owner's local automations. Serving [] rather than raising is also
        what keeps a partially-synced file from looking like an outage.
        """
        self._stamp = self._current_stamp()
        if self._path is None or not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        rows = data.get("triggers") if isinstance(data, dict) else data
        return [r for r in (rows or []) if isinstance(r, dict)]

    def _write_rows(self, rows: list[dict[str, Any]]) -> None:
        """Replace the shared file with ``rows``, atomically. A partial write is worse than none."""
        if self._path is None:
            raise RuntimeError(
                "shared-automations has no file configured — set its 'path' setting before writing"
            )
        parent = self._path.parent
        parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": STORE_VERSION, "triggers": rows, "saved_at": time.time()}
        # Temp file in the SAME directory so os.replace is a rename and not a cross-device copy.
        fd, tmp = tempfile.mkstemp(prefix=_TMP_PREFIX, dir=str(parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self._stamp = self._current_stamp()


def create_provider(config: dict[str, Any] | None = None) -> SharedAutomationsStore:
    """Extension factory — builds the shared-automations store from user settings."""
    config = config or {}
    return SharedAutomationsStore(path=str(config.get("path", "") or ""))
