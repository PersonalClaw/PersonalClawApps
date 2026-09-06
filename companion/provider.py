"""The `companion` app's two providers: a tool surface, and the trigger store behind it.

``CompanionProvider`` is the ``tool`` provider — six tools on the agent tool layer that add,
list, plan and dismiss the companion's items. ``CompanionTriggerStore`` is the ``trigger``
provider — it serves those same items to core's automation substrate as trigger ROWS, so a
reminder the user asked for in chat actually goes off, and a watched folder actually speaks up.

**Why two providers rather than one.** They answer different questions. The tool answers
"change my day", the store answers "which automations exist". Core's automation substrate only
reads the second, and it reads it fresh on every pass — which is what makes the app's central
promise structural rather than a policy: **disabling the app removes every trigger it
contributed**, because a store nobody reads serves nothing, and there is no companion row
anywhere in core's own ``triggers.json`` to leave behind.

**Rows, never execution.** This app never fires anything. It has no scheduler, no run journal,
no retry policy. Core does all the firing, under all of its own gates, and every row this app
serves is frozen to a single action (``notify``) at synthesis time.

Every string that came from a person or a file is fenced with
``personalclaw.sdk.security.fence_untrusted`` before a model sees it — reminder titles and
notes, watch labels, watch paths. A reminder is exactly where a pasted line from a web page or
a ticket ends up.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from personalclaw.sdk.security import fence_untrusted
from personalclaw.sdk.tool import RiskLevel, ToolDefinition, ToolProvider, ToolResult
from personalclaw.sdk.triggers import LoadedTrigger, Trigger, TriggerStoreProvider, parse_trigger

from companion import (
    APP_NAME,
    Companion,
    CompanionError,
    InvalidInput,
    ItemMissing,
    StoreFull,
    SurfaceOff,
)

logger = logging.getLogger(APP_NAME)

_SETTINGS_HINT = "Settings → Tools → Companion is where the three surfaces are turned on."


class CompanionProvider(ToolProvider):
    """The companion's tool surface: reminders, a watchlist and a day plan."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = dict(config or {})
        self._book = Companion(self._config)

    # ── Identity ────────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "companion"

    @property
    def display_name(self) -> str:
        return "Companion"

    def info(self) -> dict[str, Any]:
        # Reports the SURFACES and the zone, never a count — a count would need the store,
        # and reading the store binds (and creates) this app's data dir. Core calls info()
        # on a provider it built only to render Settings.
        return {
            "surfaces": self._book.surface_state(),
            "timezone": self._book.zone or "UTC (no zone configured)",
            "day_brief_at": self._book.brief_at or "(off)",
        }

    # ── Tool surface ────────────────────────────────────────────────────────────

    async def list_tools(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name="companion_status",
                description=(
                    "What the companion is currently doing: which of its three surfaces "
                    "(reminders, watchlist, day brief) are switched on, how many automations "
                    "each one contributes, and which timezone its schedules use. Every surface "
                    "is off until the user turns it on."
                ),
                provider=self.name,
                parameters={"type": "object", "properties": {}},
                requires_approval=False,
                risk_level=RiskLevel.SAFE,
            ),
            ToolDefinition(
                name="companion_remind",
                description=(
                    "Add a reminder. Give `at` for a one-shot ('2026-09-07T09:00') or `cron` "
                    "for a recurring one ('30 8 * * 1-5') — exactly one of the two. The "
                    "reminder becomes a real automation that raises a notification at that "
                    "time; it never runs a prompt or an agent."
                ),
                provider=self.name,
                parameters={
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "One short line — what to be reminded of.",
                        },
                        "at": {
                            "type": "string",
                            "description": (
                                "ISO-8601 date-time for a one-shot. No offset means the "
                                "companion's configured timezone."
                            ),
                        },
                        "cron": {
                            "type": "string",
                            "description": (
                                "Five-field cron for a recurring reminder. Fastest cadence "
                                "accepted is every 15 minutes."
                            ),
                        },
                        "note": {
                            "type": "string",
                            "description": (
                                "Longer context, for the user to read. Deliberately never part "
                                "of the automation — only the title reaches the notification."
                            ),
                        },
                    },
                    "required": ["title"],
                },
                requires_approval=False,
                risk_level=RiskLevel.CAUTION,
            ),
            ToolDefinition(
                name="companion_watch",
                description=(
                    "Watch a file, a folder or a 'folder/*.ext' glob and raise a notification "
                    "when its contents really change. Absolute or '~/' paths only; no "
                    "recursive '**'."
                ),
                provider=self.name,
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "What to watch: '~/work/inbox' or '~/work/*.md'.",
                        },
                        "label": {
                            "type": "string",
                            "description": "What the notification should call it. Optional.",
                        },
                    },
                    "required": ["path"],
                },
                requires_approval=False,
                risk_level=RiskLevel.CAUTION,
            ),
            ToolDefinition(
                name="companion_list",
                description=(
                    "Everything the companion holds — every reminder and every watch, with the "
                    "id companion_dismiss takes, whether each one is currently armed, and "
                    "whether a one-shot has already been delivered."
                ),
                provider=self.name,
                parameters={"type": "object", "properties": {}},
                requires_approval=False,
                risk_level=RiskLevel.SAFE,
                max_output=60_000,
            ),
            ToolDefinition(
                name="companion_day_plan",
                description=(
                    "Today, as the companion sees it: what is due before midnight, what is "
                    "overdue, what recurs, and what is being watched. A rendering of what is "
                    "already stored — it schedules nothing and changes nothing."
                ),
                provider=self.name,
                parameters={"type": "object", "properties": {}},
                requires_approval=False,
                risk_level=RiskLevel.SAFE,
                max_output=60_000,
            ),
            ToolDefinition(
                name="companion_dismiss",
                description=(
                    "Remove one reminder or watch by the id companion_list shows. Its "
                    "automation disappears with it."
                ),
                provider=self.name,
                parameters={
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "The item id from companion_list."}
                    },
                    "required": ["id"],
                },
                requires_approval=True,
                risk_level=RiskLevel.DESTRUCTIVE,
            ),
        ]

    async def invoke(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        handlers = {
            "companion_status": self._status,
            "companion_remind": self._remind,
            "companion_watch": self._watch,
            "companion_list": self._list,
            "companion_day_plan": self._day_plan,
            "companion_dismiss": self._dismiss,
        }
        handler = handlers.get(tool_name)
        if handler is None:
            return ToolResult(
                success=False,
                error=f"Unknown tool: {tool_name!r}",
                recovery_hints=[f"This provider exposes: {', '.join(sorted(handlers))}."],
            )
        try:
            return await handler(arguments)
        except SurfaceOff as exc:
            return ToolResult(success=False, error=str(exc), recovery_hints=[_SETTINGS_HINT])
        except ItemMissing as exc:
            return ToolResult(
                success=False,
                error=str(exc),
                recovery_hints=["Call companion_list to see what is there and what its id is."],
            )
        except StoreFull as exc:
            return ToolResult(success=False, error=str(exc))
        except InvalidInput as exc:
            return ToolResult(success=False, error=str(exc))
        except CompanionError as exc:  # pragma: no cover - the base is never raised directly
            return ToolResult(success=False, error=str(exc))
        except OSError as exc:
            # A full disk, a read-only mount, a data dir the gateway user cannot write: a
            # legible failure, not a traceback out of the tool layer.
            logger.warning("companion store I/O failed for %s: %s", tool_name, exc)
            return ToolResult(
                success=False,
                error=f"The companion's store could not be read or written: {exc}",
                recovery_hints=["Check that PersonalClaw's apps data directory is writable."],
            )

    # ── Handlers ────────────────────────────────────────────────────────────────

    async def _status(self, _args: dict[str, Any]) -> ToolResult:
        state = self._book.surface_state()
        if not self._book.any_surface_on:
            return ToolResult(
                success=True,
                output=(
                    "The companion is installed and completely quiet — all three surfaces are "
                    "off, which is how it ships. It contributes no automations at all until "
                    f"one is turned on.\n\n{_SETTINGS_HINT}"
                ),
                metadata={"surfaces": state, "rows": 0, "any_surface_on": False},
            )
        rows = await asyncio.to_thread(self._book.rows)
        counts = {"reminders": 0, "watchlist": 0, "day_brief": 0}
        for row in rows:
            if row["id"].startswith("companion:reminder:"):
                counts["reminders"] += 1
            elif row["id"].startswith("companion:watch:"):
                counts["watchlist"] += 1
            else:
                counts["day_brief"] += 1
        lines = [
            "| surface | state | automations |",
            "|---|---|---|",
            f"| Reminders | {'on' if state['reminders'] else 'off'} | {counts['reminders']} |",
            f"| Watchlist | {'on' if state['watchlist'] else 'off'} | {counts['watchlist']} |",
            (f"| Day brief | {self._book.brief_at or 'off'} | " f"{counts['day_brief']} |"),
        ]
        tail = (
            f"\n\nSchedules use **{self._book.zone or 'UTC'}**. "
            f"{len(rows)} automation(s) in total — all of them raise a notification and "
            "nothing else. Disabling this app removes every one of them."
        )
        if self._book.brief_at and counts["day_brief"] == 0:
            # The one state a user cannot explain from the table alone: the setting says
            # 08:30 and the automation is not there, because it was retired from the
            # Automations page. Say which switch brings it back.
            tail += (
                f"\n\nThe {self._book.brief_at} day brief was retired from the Automations "
                "page. Changing its time in Settings starts a fresh one."
            )
        return ToolResult(
            success=True,
            output="\n".join(lines) + tail,
            metadata={
                "surfaces": state,
                "rows": len(rows),
                "counts": counts,
                "timezone": self._book.zone or "UTC",
                "any_surface_on": True,
            },
        )

    async def _remind(self, args: dict[str, Any]) -> ToolResult:
        item = await asyncio.to_thread(
            self._book.add_reminder,
            title=str(args.get("title") or ""),
            note=str(args.get("note") or ""),
            at=str(args.get("at") or ""),
            cron=str(args.get("cron") or ""),
        )
        # The id and the schedule, never the title: a reminder's text is the user's own
        # content and has no business in a log file.
        logger.info("reminder %s scheduled (%s)", item.id, "cron" if item.recurring else "one-shot")
        if item.recurring:
            when = f"on `{item.cron}`"
        else:
            when = datetime.fromtimestamp(item.at, tz=self._book.tzinfo()).strftime(
                "%Y-%m-%d %H:%M"
            )
            when = f"at {when} ({self._book.zone or 'UTC'})"
        return ToolResult(
            success=True,
            output=(
                f"Reminder `{item.id}` is set {when}. It will raise a notification — nothing "
                "else runs."
            ),
            metadata={
                "id": item.id,
                "trigger_id": f"companion:reminder:{item.id}",
                "recurring": item.recurring,
                "at": item.at,
                "cron": item.cron,
            },
        )

    async def _watch(self, args: dict[str, Any]) -> ToolResult:
        item = await asyncio.to_thread(
            self._book.add_watch,
            path=str(args.get("path") or ""),
            label=str(args.get("label") or ""),
        )
        # The id and the DEPTH, never the path: a watched path is the user's own filesystem and
        # a folder name is exactly the sort of thing that should not end up in a log file.
        depth = item.path.count("/")
        logger.info("watch %s added (%d segments)", item.id, depth)
        fenced = fence_untrusted(
            item.path,
            source="companion watch path",
            source_type="companion_watch",
            source_id=item.id,
        )
        return ToolResult(
            success=True,
            output=(
                f"Watch `{item.id}` added. It notifies when the contents really change "
                f"(not merely when something touches the file):\n\n{fenced}"
            ),
            metadata={
                "id": item.id,
                "trigger_id": f"companion:watch:{item.id}",
                "path": item.path,
                "label": item.label,
            },
        )

    async def _list(self, _args: dict[str, Any]) -> ToolResult:
        reminders, watches, rows = await asyncio.to_thread(self._snapshot)
        armed = {row["id"] for row in rows if row.get("enabled", True)}
        if not reminders and not watches:
            return ToolResult(
                success=True,
                output=(
                    "The companion holds nothing yet — add a reminder with companion_remind or "
                    "a folder with companion_watch."
                ),
                metadata={"reminders": 0, "watches": 0},
            )
        blocks: list[str] = []
        if reminders:
            lines = ["| id | when | state | what |", "|---|---|---|---|"]
            for item in reminders:
                row_id = f"companion:reminder:{item.id}"
                if item.recurring:
                    when = f"`{item.cron}`"
                else:
                    when = datetime.fromtimestamp(item.at, tz=self._book.tzinfo()).strftime(
                        "%Y-%m-%d %H:%M"
                    )
                state = self._row_state(row_id, rows, armed, one_shot=not item.recurring)
                note = f" — {item.note.splitlines()[0]}" if item.note.strip() else ""
                lines.append(f"| `{item.id}` | {when} | {state} | {item.title}{note} |")
            blocks.append(f"**{len(reminders)} reminder(s)**\n\n" + "\n".join(lines))
        if watches:
            lines = ["| id | state | path | label |", "|---|---|---|---|"]
            for watch in watches:
                row_id = f"companion:watch:{watch.id}"
                state = self._row_state(row_id, rows, armed, one_shot=False)
                lines.append(f"| `{watch.id}` | {state} | `{watch.path}` | {watch.label or '—'} |")
            blocks.append(f"**{len(watches)} watch(es)**\n\n" + "\n".join(lines))
        # Titles, notes, labels and paths are all the user's own text, and a reminder is
        # exactly where a pasted line from a web page ends up. One fence over the whole
        # rendering rather than per cell: a table stitched from fenced fragments is not a
        # table a model can read.
        fenced = fence_untrusted(
            "\n\n".join(blocks),
            source="companion items",
            source_type="companion_items",
            source_id=APP_NAME,
        )
        return ToolResult(
            success=True,
            output=fenced,
            metadata={
                "reminders": len(reminders),
                "watches": len(watches),
                "armed": len(armed),
                "surfaces": self._book.surface_state(),
            },
        )

    async def _day_plan(self, _args: dict[str, Any]) -> ToolResult:
        plan = await asyncio.to_thread(self._book.day_plan)
        head = f"# {plan['date']} — {plan['timezone']}"
        sections: list[str] = []
        if plan["overdue"]:
            sections.append(
                "**Overdue**\n"
                + "\n".join(f"- {e['date']} {e['when']} — {e['title']}" for e in plan["overdue"])
            )
        if plan["due_today"]:
            sections.append(
                "**Due today**\n"
                + "\n".join(f"- {e['when']} — {e['title']}" for e in plan["due_today"])
            )
        if plan["recurring"]:
            sections.append(
                "**Recurring**\n"
                + "\n".join(f"- `{e['cron']}` — {e['title']}" for e in plan["recurring"])
            )
        if plan["watches"]:
            sections.append(
                "**Watching**\n"
                + "\n".join(
                    f"- `{w['path']}`" + (f" ({w['label']})" if w["label"] else "")
                    for w in plan["watches"]
                )
            )
        if not sections:
            sections.append(
                "Nothing on the calendar and nothing being watched. That is a real answer, "
                "not an empty render."
            )
        tail: list[str] = []
        if plan["later"]:
            tail.append(f"{len(plan['later'])} reminder(s) further out.")
        if plan["delivered"]:
            tail.append(f"{plan['delivered']} already delivered.")
        if plan["brief_at"]:
            tail.append(f"The day brief nudges at {plan['brief_at']}.")
        off = [name for name, on in plan["surfaces"].items() if not on]
        if off:
            tail.append(f"Off: {', '.join(off)} — nothing from those is scheduled.")
        body = "\n\n".join(sections)
        fenced = fence_untrusted(
            body, source="companion day plan", source_type="companion_plan", source_id=plan["date"]
        )
        parts = [head, fenced]
        if tail:
            parts.append(" ".join(tail))
        return ToolResult(success=True, output="\n\n".join(parts), metadata=plan)

    async def _dismiss(self, args: dict[str, Any]) -> ToolResult:
        result = await asyncio.to_thread(self._book.dismiss, str(args.get("id") or ""))
        logger.info("companion %s %s dismissed", result["kind"], result["id"])
        return ToolResult(
            success=True,
            output=(
                f"Dismissed {result['kind']} `{result['id']}`. Its automation "
                f"(`{result['trigger_id']}`) is gone from the Automations page too."
            ),
            metadata=result,
        )

    # ── helpers ─────────────────────────────────────────────────────────────────

    def _snapshot(self) -> tuple[list[Any], list[Any], list[dict[str, Any]]]:
        """Items and rows in ONE worker hop, so a listing cannot show a torn view."""
        return self._book.reminders(), self._book.watches(), self._book.rows()

    def _row_state(
        self,
        row_id: str,
        rows: list[dict[str, Any]],
        armed: set[str],
        *,
        one_shot: bool,
    ) -> str:
        """How an item's automation stands, in one word a person can act on."""
        present = any(row["id"] == row_id for row in rows)
        if present:
            return "armed" if row_id in armed else "paused"
        if one_shot:
            return "delivered"
        return "surface off"


class CompanionTriggerStore(TriggerStoreProvider):
    """The companion's items, served to core's automation substrate as trigger rows.

    Read-mostly, and deliberately narrow: it answers "which automations exist", persists the
    schedule core writes back after a fire, and retires a row core deletes. It never fires
    anything and is never handed a payload, a run or a credential.
    """

    name = APP_NAME
    display_name = "Companion"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._book = Companion(dict(config or {}))
        # (mtime_ns, size) rather than mtime alone: two writes inside one filesystem mtime
        # tick is exactly what "add three reminders in one turn" looks like.
        self._stamp: tuple[int, int] = (0, 0)

    # ── the contract's read side ────────────────────────────────────────────────

    @property
    def base_dir(self) -> Path:
        """Root for this store's own sidecars — this app's data dir.

        The file-watch runtime keeps each `file` trigger's seen-state here, so it has to be a
        real, writable, per-install directory. It is the same directory the items live in,
        which is what makes "uninstall the app and its automations are gone" one fact rather
        than two.
        """
        return self._book.root

    def load(self) -> list[LoadedTrigger]:
        """Every row this app currently contributes, each carrying its parse issues.

        Rows are SYNTHESISED from the items on each call, never read from the file as rows —
        so a hand edit, a stale sync copy or an agent write cannot introduce a row shape this
        app would not have minted. Issues are still surfaced rather than swallowed: a row this
        app builds wrong must be visible and inert, not absent and mysterious.
        """
        self._stamp = self._current_stamp()
        out: list[LoadedTrigger] = []
        for row in self._rows():
            trigger, issues = parse_trigger(row)
            out.append(LoadedTrigger(trigger=trigger, issues=list(issues)))
        return out

    def list_triggers(self, *, kind: str = "", include_broken: bool = True) -> list[Trigger]:
        """The rows as a flat list, for a listing view."""
        out: list[Trigger] = []
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
        row = self._book.row_for(str(trigger_id))
        if row is None:
            return None
        trigger, issues = parse_trigger(row)
        return LoadedTrigger(trigger=trigger, issues=list(issues))

    def changed_on_disk(self) -> bool:
        """Has anything moved since the last :meth:`load`?

        Answered from the store file's (mtime_ns, size). A surface being switched off in
        Settings is NOT visible here — core rebuilds the provider from its settings when they
        change, so that path is covered by construction rather than by this stamp.
        """
        return self._current_stamp() != self._stamp

    # ── the contract's write side ───────────────────────────────────────────────

    def upsert(self, trigger: Trigger) -> Trigger:
        """Persist core's write-back for one row, and hand back the row as stored.

        This is the method core calls to save a fired row's NEXT schedule, and it verifies the
        result by re-reading through :meth:`get` — a store that accepted the write and served
        the old ``next_fire_at`` is quarantined, which is the right outcome, because it would
        otherwise fire every tick forever. So the runtime rollups really are written here, and
        :meth:`get` really does overlay them.

        Only the rollups are taken. The item's own fields — the title, the note, the path, the
        schedule the user chose — are the user's, and core has no reason to rewrite them; a
        store that let a write-back edit them would let a fire path silently retitle a
        reminder.
        """
        fields = {key: getattr(trigger, key) for key in _RUNTIME_KEYS if hasattr(trigger, key)}
        self._book.record_runtime(trigger.id, fields)
        self._stamp = self._current_stamp()
        stored = self.get(trigger.id)
        return stored.trigger if stored is not None else trigger

    def delete(self, trigger_id: str) -> bool:
        """Retire one row — and the item behind it. Returns whether it was being served.

        Core verifies the row is gone afterwards, so this has to be real. A one-shot reminder
        carries ``delete_after_run``, which is what routes it here once it has been delivered.
        """
        gone = self._book.drop_row(str(trigger_id))
        self._stamp = self._current_stamp()
        if gone:
            logger.info("companion row %s retired", trigger_id)
        return gone

    # ── plumbing ────────────────────────────────────────────────────────────────

    def _rows(self) -> list[dict[str, Any]]:
        """The row dicts, or [] if the store cannot be read.

        Never raises: core logs an empty provider read and keeps arming the owner's own local
        automations, whereas a raise here would cost a tick that is also rescheduling them.
        """
        try:
            return self._book.rows()
        except OSError as exc:
            logger.warning("companion store unreadable this pass: %s", exc)
            return []

    def _current_stamp(self) -> tuple[int, int]:
        try:
            st = self._book.path.stat()
        except OSError:
            return (0, 0)
        return (st.st_mtime_ns, st.st_size)


#: The rollup attributes core writes back. Mirrors ``companion.RUNTIME_FIELDS`` minus
#: ``enabled``, which is included here too — core's Automations page writes a user's pause
#: through this same seam, and a store that dropped it would un-pause on the next read.
_RUNTIME_KEYS: tuple[str, ...] = (
    "enabled",
    "next_fire_at",
    "last_run_id",
    "run_count",
    "last_success_at",
    "last_failure_at",
    "last_fired_at",
    "park_retry_after",
    "last_alert_hash",
    "last_alert_at",
    "health_status",
    "last_error_summary",
    "state",
)


def create_provider(config: dict[str, Any] | None = None) -> CompanionProvider:
    """Manifest factory for the ``tool`` provider — core calls this with saved settings."""
    return CompanionProvider(config)


def create_trigger_store(config: dict[str, Any] | None = None) -> CompanionTriggerStore:
    """Manifest factory for the ``trigger`` provider — same settings, same items."""
    return CompanionTriggerStore(config)
