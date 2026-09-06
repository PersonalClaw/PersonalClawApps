"""The companion's own store: reminders, a watchlist, a day brief — and the trigger ROWS
those items become.

Three surfaces, each independently opt-in and **all three off out of the box**. Installing
this app arms nothing; a surface starts contributing automations only once the user turns it
on in Settings, and turning it back off (or disabling the app, or uninstalling it) removes
every row it contributed — because the rows are never stored anywhere core owns. Core reads
them out of this store on each pass, so a store that is not being read serves nothing.

**The file persists ITEMS, never rows.** That is the load-bearing decision in this module,
and it is what keeps a companion automation from ever becoming an instruction. A reminder on
disk is a title, a note and a time; the trigger row — its ``kind``, its ``workflow`` action,
its frozen ``capabilities`` — is synthesized here, in code, on every read. There is no field
in the file where an action could be written, so no hand edit, no agent tool call and no
tampered sync copy can turn one of these rows into an LLM run. The action is always
``notify``, and ``capabilities`` is always ``{"providers": ["notify"]}``.

That is deliberately smaller than "the companion drafts your morning brief". A row that
fired ``run-prompt`` with the user's own reminder text would be untrusted text becoming a
durable, scheduled instruction with no human in the loop — the one shape this app refuses to
author. What the day brief actually does is *nudge*: it fires a notification at the hour the
user chose, and the plan itself is rendered on demand by ``companion_day_plan``, in a session
a human is present for.

Everything else here is input validation, because every value in this store arrives from
outside: a watch path becomes a filesystem glob core walks, a cron expression becomes a
schedule, and a reminder title becomes the body of a ``notify`` template — which is a
``$``-substituting template, so ``$`` is excluded from user text at row-synthesis time rather
than trusted to be inert.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from personalclaw.sdk.util import app_data_dir, atomic_write, config_dir

APP_NAME = "companion"

#: The file-format version this app writes.
STORE_VERSION = 1
STORE_FILE = "companion.json"

#: The three surfaces, in the order the doctor and ``companion_status`` report them.
SURFACES: tuple[str, ...] = ("reminders", "watchlist", "day_brief")

#: The one action every companion row is allowed to fire. Not configurable, by design.
NOTIFY_PROVIDER = "notify"

# ── caps, not trust ────────────────────────────────────────────────────────────────────

MAX_REMINDERS = 200
MAX_WATCHES = 50
MAX_DROPPED = 500
TITLE_MAX = 200
NOTE_MAX = 4_000
LABEL_MAX = 80
PATH_MAX = 400
MAX_PATH_SEGMENTS = 24
CRON_MAX = 100
#: Floor on a RECURRING reminder's cadence, in minutes. Mirrors core's own
#: ``MIN_CLOCK_INTERVAL_SECS`` (900s) so a companion reminder cannot be the thing that
#: out-ticks the platform's own guidance.
MIN_CRON_INTERVAL_MINS = 15
#: How far ahead a one-shot reminder may be set. A date in 2087 is a typo, not a plan.
MAX_AT_HORIZON_DAYS = 1826

_ID_BYTES = 4

# ── grammars ───────────────────────────────────────────────────────────────────────────

#: A control character, including newline and tab. Stripped from every stored string: a
#: newline inside a title would forge a second line in every log and notification the item is
#: read through (ARCC SAX-06), and a NUL would truncate a path at the syscall boundary.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

#: One path segment of a watch pattern: at most ONE leading dot, then an alphanumeric. That
#: makes `.`, `..`, `...` and an `-oSomething=` lookalike UNREPRESENTABLE rather than filtered
#: out, while still allowing the dotfiles people legitimately watch (`.config`, `.zshrc`) —
#: a watchlist that refused `~/.config/nvim` would read as arbitrary rather than careful.
_SEGMENT_RE = re.compile(r"^\.?[A-Za-z0-9][A-Za-z0-9._+@ -]*$")

#: The FINAL segment may additionally carry a shell-style glob. `**` is refused separately —
#: a recursive glob on a hand-typed path is how a watchlist becomes a filesystem crawl.
_GLOB_SEGMENT_RE = re.compile(r"^\.?[A-Za-z0-9*?][A-Za-z0-9._+@ *?-]*$")

#: One cron field. Digits, `*`, ranges, lists and steps, plus the 3-letter day/month names
#: croniter accepts. Anything else is refused rather than stored: `expr` is persisted and
#: re-read forever, so it is exactly the field that must not carry free text.
_CRON_FIELD_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z,/*-]*$|^\*(/[0-9]+)?$")

#: `HH:MM`, 24-hour, for the day brief.
_HHMM_RE = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")

#: The runtime rollups core writes BACK through ``upsert`` when it fires one of these rows.
#: Round-tripped verbatim: a store that accepts the write and serves the old value is
#: quarantined by core (and rightly — it would fire every tick forever).
RUNTIME_FIELDS: tuple[str, ...] = (
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


# ── failures ───────────────────────────────────────────────────────────────────────────


class CompanionError(Exception):
    """Base for everything this module refuses to do."""


class InvalidInput(CompanionError):
    """A title, path, time or expression this app will not store."""


class ItemMissing(CompanionError):
    """No reminder or watch with that id."""


class SurfaceOff(CompanionError):
    """The surface a tool needs is switched off, so the tool declines rather than pretends."""


class StoreFull(CompanionError):
    """A cap was reached. Refusing is the honest answer; silently dropping is not."""


# ── records ────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Reminder:
    """One reminder. ``at`` is epoch seconds for a one-shot; ``cron`` is set instead for a
    recurring one. Exactly one of the two is ever populated.

    ``note`` is the free-text field, and it is the reason this record has two text fields
    instead of one: the title is what a notification says, and the note is what only a human
    reading ``companion_list`` sees. The note NEVER enters a trigger row, so however long or
    however pasted-in it is, it cannot reach an automation payload.
    """

    id: str
    title: str
    note: str = ""
    at: float = 0.0
    cron: str = ""
    created_at: float = 0.0

    @property
    def recurring(self) -> bool:
        return bool(self.cron)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "note": self.note,
            "at": self.at,
            "cron": self.cron,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class Watch:
    """One watched path or glob. ``label`` is what a notification says; ``path`` is what core
    globs, and it never appears in a template."""

    id: str
    path: str
    label: str = ""
    created_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": self.path,
            "label": self.label,
            "created_at": self.created_at,
        }


@dataclass
class _State:
    """Everything on disk. Deliberately four flat collections and nothing row-shaped."""

    reminders: list[Reminder] = field(default_factory=list)
    watches: list[Watch] = field(default_factory=list)
    runtime: dict[str, dict[str, Any]] = field(default_factory=dict)
    dropped: list[str] = field(default_factory=list)


# ── sanitisers and validators ──────────────────────────────────────────────────────────


def one_line(text: str, cap: int) -> str:
    """``text`` with control characters removed, whitespace collapsed, truncated to ``cap``.

    Not a security boundary on its own — it is what keeps a stored string from forging a
    second log line or a second notification.
    """
    flat = _CONTROL_RE.sub(" ", str(text or ""))
    return " ".join(flat.split())[:cap].strip()


def template_safe(text: str) -> str:
    """``text`` with every ``$`` removed, for a string about to become a ``notify`` template.

    ``notify`` renders ``title_template``/``body_template`` through core's
    ``$EVENT``/``$CONTEXT``/``$<payload-key>`` substituter. A reminder titled ``$CONTEXT``
    would therefore expand into whatever the fire payload happens to hold — a small leak with
    a long life, because the row is durable and re-read on every pass. Excluding ``$`` at
    synthesis (rather than at storage) keeps the user's own text intact everywhere a human
    reads it, and makes expansion impossible everywhere a machine does.
    """
    return str(text or "").replace("$", "")


def validate_title(raw: str) -> str:
    """A reminder's title: one printable line, non-empty, capped."""
    title = one_line(raw, TITLE_MAX)
    if not title:
        raise InvalidInput("a reminder needs a title — one short line of what to be reminded of")
    return title


def validate_note(raw: str) -> str:
    """A reminder's note: free text, control characters other than newline preserved as-is.

    Newlines ARE kept here (unlike a title) because a note is prose the user reads. It is
    fenced on the way out and never reaches a trigger row, which is what makes that safe.
    """
    note = str(raw or "")
    if len(note) > NOTE_MAX:
        raise InvalidInput(f"that note is longer than {NOTE_MAX} characters — trim it or use notes")
    return note.replace("\x00", "")


def validate_label(raw: str) -> str:
    """A watch's label: one printable line, may be empty."""
    return one_line(raw, LABEL_MAX)


def local_zone_name() -> str:
    """The machine's IANA timezone name, or ``""`` when it cannot be determined.

    Read from the ``/etc/localtime`` symlink, which is where macOS and every mainstream Linux
    keep it. ``time.tzname`` is deliberately not used: it yields an abbreviation (``CEST``),
    and ``ZoneInfo`` cannot take one — a value that looks right and resolves to nothing is
    worse than an empty answer, because core's arm path silently falls back to UTC.
    """
    try:
        target = os.readlink("/etc/localtime")
    except OSError:
        return ""
    _, marker, tail = target.partition("zoneinfo/")
    if not marker:
        return ""
    name = tail.strip("/")
    if not name or ".." in name.split("/"):
        return ""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return ""
    return name


def validate_zone(raw: str) -> str:
    """An IANA zone name, or ``""``. Refuses an unknown zone instead of storing it.

    Core's arm path treats an unknown ``spec.timezone`` as UTC and logs at debug — which is
    the right fail-safe there, and exactly why it has to be caught here: a user who typed
    ``Europe/Pariss`` would otherwise get UTC reminders and no explanation.
    """
    name = one_line(raw, 64)
    if not name:
        return ""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise InvalidInput(
            f"{name!r} is not an IANA timezone name (e.g. 'Europe/Berlin', 'America/Denver')"
        ) from exc
    return name


def validate_brief_time(raw: str) -> str:
    """``HH:MM`` (24-hour), or ``""`` meaning the day brief is off.

    Flattened but NOT truncated before matching. Truncating first would quietly turn
    ``08:30:00`` into a valid ``08:30`` — a value the user did not type, accepted silently,
    which is the whole failure mode this function exists to prevent.
    """
    value = one_line(raw, 32)
    if not value:
        return ""
    if not _HHMM_RE.match(value):
        raise InvalidInput(f"{value!r} is not a time of day — use 24-hour HH:MM, e.g. '08:30'")
    return value


def validate_cron(raw: str) -> str:
    """A five-field cron expression, with a floor on how often it may fire.

    The floor targets the two spellings that produce a runaway by accident — a bare ``*``
    minute field and ``*/n`` with ``n`` under fifteen. A hand-written minute LIST
    (``0,15,30,45``) is allowed and is not second-guessed: a list is a deliberate choice, and
    a rule that tried to reason about every list would refuse legitimate schedules.
    """
    expr = one_line(raw, CRON_MAX)
    if not expr:
        raise InvalidInput("a recurring reminder needs a cron expression, e.g. '30 8 * * 1-5'")
    fields = expr.split()
    if len(fields) != 5:
        raise InvalidInput(
            f"a cron expression has five fields (minute hour day month weekday); got {len(fields)}"
        )
    for pos, value in enumerate(fields):
        if not _CRON_FIELD_RE.match(value):
            raise InvalidInput(
                f"cron field {pos + 1} ({value!r}) is not a cron field — digits, '*', "
                "'a-b', 'a,b', '*/n' and 3-letter day/month names only"
            )
    minute = fields[0]
    if minute == "*":
        raise InvalidInput(
            "a reminder every minute is almost never what was meant — set a minute, or use "
            f"'*/{MIN_CRON_INTERVAL_MINS}' for the fastest cadence this app will schedule"
        )
    if minute.startswith("*/"):
        step = minute[2:]
        if not step.isdigit() or int(step) < MIN_CRON_INTERVAL_MINS:
            raise InvalidInput(
                f"the fastest recurring reminder this app schedules is every "
                f"{MIN_CRON_INTERVAL_MINS} minutes (got {minute!r})"
            )
    return expr


def validate_at(raw: str, *, zone: str = "", now: float | None = None) -> float:
    """An ISO-8601 date-time for a one-shot reminder → epoch seconds.

    A value with no offset is read in ``zone`` (the companion's configured zone, else UTC),
    because "remind me at 09:00" means nine in the morning where the user is. Core stores
    ``spec.at`` as an epoch, so the ambiguity is resolved here, once, rather than at fire
    time when nobody is watching.
    """
    text = one_line(raw, 40)
    if not text:
        raise InvalidInput("a one-shot reminder needs a time, e.g. '2026-09-07T09:00'")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise InvalidInput(
            f"{text!r} is not an ISO-8601 date-time — try '2026-09-07T09:00' or "
            "'2026-09-07T09:00+02:00'"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(zone) if zone else timezone.utc)
    stamp = parsed.timestamp()
    current = time.time() if now is None else now
    if stamp <= current:
        raise InvalidInput(
            f"{text!r} is in the past — a one-shot reminder set behind the clock never fires"
        )
    if stamp > current + MAX_AT_HORIZON_DAYS * 86400:
        raise InvalidInput(
            f"{text!r} is more than {MAX_AT_HORIZON_DAYS // 365} years out — check the year"
        )
    return stamp


def validate_watch_path(raw: str) -> str:
    """A watch pattern → the absolute path core will glob, or a refusal.

    Validated per SEGMENT before it is ever a path, which is what makes the refusals
    structural rather than a denylist:

    * ``~`` is expanded; ``$VARS`` deliberately are **not**. Expanding an environment
      variable out of a hand-typed pattern is a way to make this app read a value the user
      did not type, and a watchlist has no need of it.
    * every segment is ``[one optional dot]alphanumeric…``, so ``.``, ``..`` and a
      ``-oSomething=`` lookalike are unrepresentable rather than filtered — while
      ``~/.config/nvim``, which people really do want to watch, still works;
    * a glob (``*``, ``?``) is allowed only in the FINAL segment, and ``**`` never — a
      recursive glob on a typed path turns a watchlist into a filesystem crawl;
    * the result must be absolute, inside neither PersonalClaw's own home nor deeper than
      ``MAX_PATH_SEGMENTS``.

    PersonalClaw's config dir is excluded for a behavioral reason rather than a secrecy one:
    the platform writes there continuously, so a watch on it fires on the platform's own
    bookkeeping every single pass — an automation that can never be quiet.
    """
    text = str(raw or "")
    if not text.strip():
        raise InvalidInput("a watch needs a path, e.g. '~/work/inbox' or '~/work/*.md'")
    if _CONTROL_RE.search(text):
        raise InvalidInput("a watch path cannot contain control characters or newlines")
    if len(text) > PATH_MAX:
        raise InvalidInput(f"that path is longer than {PATH_MAX} characters")
    if "**" in text:
        raise InvalidInput(
            "'**' is not supported — watch one folder (or 'folder/*.ext'), not a whole tree"
        )
    expanded = os.path.expanduser(text.strip())
    if not expanded.startswith("/"):
        raise InvalidInput(
            f"{text!r} is not an absolute path — start it with '/' or '~/' so there is no "
            "question which folder is meant"
        )
    segments = [s for s in expanded.split("/")]
    if segments and segments[0] == "":
        segments = segments[1:]
    if segments and segments[-1] == "":
        segments = segments[:-1]  # a trailing slash is a directory, not an empty segment
    if not segments:
        raise InvalidInput("'/' is not a useful thing to watch — name a folder or a file")
    if len(segments) > MAX_PATH_SEGMENTS:
        raise InvalidInput(f"that path is more than {MAX_PATH_SEGMENTS} segments deep")
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        pattern = _GLOB_SEGMENT_RE if last else _SEGMENT_RE
        if not pattern.match(segment):
            raise InvalidInput(
                f"path segment {segment!r} is not allowed — each segment must start with a "
                "letter or digit, and a '*' or '?' may only appear in the last one"
            )
    resolved = "/" + "/".join(segments)
    try:
        home = str(config_dir())
    except OSError:  # pragma: no cover - a home that cannot be created is core's problem
        home = ""
    if home and (resolved == home or resolved.startswith(home.rstrip("/") + "/")):
        raise InvalidInput(
            "that is inside PersonalClaw's own folder, which changes constantly — a watch "
            "there would fire on the platform's bookkeeping and never go quiet"
        )
    return resolved


# ── the store ──────────────────────────────────────────────────────────────────────────


class Companion:
    """The companion's items, the surface switches, and the trigger rows they synthesise.

    One instance is shared by the tool provider and the trigger store, so a reminder added
    through ``companion_remind`` is a row on the very next read. State is re-read from disk on
    every access rather than cached: the two providers are separate objects in core's
    registry, and a cache in one would not see the other's write.
    """

    def __init__(self, settings: dict[str, Any] | None = None, *, root: Path | None = None) -> None:
        cfg = dict(settings or {})
        self._reminders_on = bool(cfg.get("reminders", False))
        self._watchlist_on = bool(cfg.get("watchlist", False))
        # Both of these can be REFUSED at construction time, and a refusal must not stop the
        # app mounting — an unmountable app cannot be reconfigured. So a bad value degrades to
        # off/UTC and the doctor says so out loud.
        try:
            self._brief_at = validate_brief_time(str(cfg.get("day_brief", "") or ""))
        except InvalidInput:
            self._brief_at = ""
            self._brief_error = str(cfg.get("day_brief") or "")
        else:
            self._brief_error = ""
        try:
            self._zone = validate_zone(str(cfg.get("timezone", "") or ""))
        except InvalidInput:
            self._zone = ""
            self._zone_error = str(cfg.get("timezone") or "")
        else:
            self._zone_error = ""
        self._root_override = Path(root) if root is not None else None
        self._root_impl: Path | None = None

    # ── location ───────────────────────────────────────────────────────────────────────

    @property
    def root(self) -> Path:
        """This app's data dir, bound on first use.

        Lazy for the reason ``notes`` is lazy: core constructs a provider just to read its
        tool list (the Settings → Tools round-trip), and that must not mkdir under the user's
        home. The directory appears the first time an item is actually touched.
        """
        if self._root_impl is None:
            self._root_impl = (
                self._root_override if self._root_override is not None else app_data_dir(APP_NAME)
            )
            self._root_impl.mkdir(parents=True, exist_ok=True)
        return self._root_impl

    @property
    def path(self) -> Path:
        return self.root / STORE_FILE

    # ── surfaces ───────────────────────────────────────────────────────────────────────

    @property
    def reminders_on(self) -> bool:
        return self._reminders_on

    @property
    def watchlist_on(self) -> bool:
        return self._watchlist_on

    @property
    def brief_at(self) -> str:
        return self._brief_at

    @property
    def brief_error(self) -> str:
        """The rejected ``day_brief`` setting, if one was rejected. For the doctor."""
        return self._brief_error

    @property
    def zone_error(self) -> str:
        """The rejected ``timezone`` setting, if one was rejected. For the doctor."""
        return self._zone_error

    @property
    def zone(self) -> str:
        """The configured zone, else the machine's, else ``""`` (which core reads as UTC)."""
        return self._zone or local_zone_name()

    def surface_state(self) -> dict[str, bool]:
        return {
            "reminders": self._reminders_on,
            "watchlist": self._watchlist_on,
            "day_brief": bool(self._brief_at),
        }

    @property
    def any_surface_on(self) -> bool:
        return any(self.surface_state().values())

    def tzinfo(self) -> Any:
        name = self.zone
        if not name:
            return timezone.utc
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, OSError):  # pragma: no cover - validated
            return timezone.utc

    # ── reading and writing the file ───────────────────────────────────────────────────

    def _load(self) -> _State:
        """Everything on disk. Never raises on a malformed file — an unreadable store costs
        this app's rows for that pass and nothing else, which is the direction core's own
        provider-read contract asks for."""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return _State()
        if not isinstance(raw, dict):
            return _State()
        state = _State()
        for row in raw.get("reminders") or []:
            item = _reminder_from(row)
            if item is not None:
                state.reminders.append(item)
        for row in raw.get("watches") or []:
            item = _watch_from(row)
            if item is not None:
                state.watches.append(item)
        runtime = raw.get("runtime")
        if isinstance(runtime, dict):
            for key, value in runtime.items():
                if isinstance(key, str) and isinstance(value, dict):
                    state.runtime[key] = {
                        k: v for k, v in value.items() if k in set(RUNTIME_FIELDS)
                    }
        dropped = raw.get("dropped")
        if isinstance(dropped, list):
            state.dropped = [d for d in dropped if isinstance(d, str)][-MAX_DROPPED:]
        return state

    def _save(self, state: _State) -> None:
        payload = {
            "version": STORE_VERSION,
            "reminders": [r.to_dict() for r in state.reminders],
            "watches": [w.to_dict() for w in state.watches],
            "runtime": state.runtime,
            "dropped": state.dropped[-MAX_DROPPED:],
            "saved_at": time.time(),
        }
        # 0600: these are the user's own reminders and the paths they care about — personal
        # data, on a machine that may have other accounts on it.
        atomic_write(self.path, json.dumps(payload, indent=2), mode=0o600)

    # ── items ──────────────────────────────────────────────────────────────────────────

    def reminders(self) -> list[Reminder]:
        return self._load().reminders

    def watches(self) -> list[Watch]:
        return self._load().watches

    def add_reminder(self, *, title: str, note: str = "", at: str = "", cron: str = "") -> Reminder:
        """Store one reminder. Exactly one of ``at`` / ``cron`` must be given."""
        if not self._reminders_on:
            raise SurfaceOff(
                "the Reminders surface is off — turn it on in Settings → Tools → Companion "
                "and this app will start scheduling them"
            )
        if bool(at) == bool(cron):
            raise InvalidInput(
                "give exactly one of `at` (a one-shot, e.g. '2026-09-07T09:00') or `cron` "
                "(recurring, e.g. '30 8 * * 1-5')"
            )
        clean_title = validate_title(title)
        clean_note = validate_note(note)
        when = validate_at(at, zone=self._zone) if at else 0.0
        expr = validate_cron(cron) if cron else ""
        state = self._load()
        if len(state.reminders) >= MAX_REMINDERS:
            raise StoreFull(
                f"{MAX_REMINDERS} reminders is the cap — dismiss some with companion_dismiss"
            )
        item = Reminder(
            id=_new_id(),
            title=clean_title,
            note=clean_note,
            at=when,
            cron=expr,
            created_at=time.time(),
        )
        state.reminders.append(item)
        self._save(state)
        return item

    def add_watch(self, *, path: str, label: str = "") -> Watch:
        """Store one watch. The parent folder must already exist, so a typo is caught now."""
        if not self._watchlist_on:
            raise SurfaceOff(
                "the Watchlist surface is off — turn it on in Settings → Tools → Companion "
                "and this app will start watching what you add"
            )
        resolved = validate_watch_path(path)
        parent = os.path.dirname(resolved) or "/"
        if not os.path.isdir(parent):
            raise InvalidInput(
                f"{parent} does not exist, so nothing there can change — check the path"
            )
        clean_label = validate_label(label)
        state = self._load()
        if len(state.watches) >= MAX_WATCHES:
            raise StoreFull(f"{MAX_WATCHES} watches is the cap — remove one with companion_dismiss")
        if any(w.path == resolved for w in state.watches):
            raise InvalidInput("that path is already on the watchlist")
        item = Watch(id=_new_id(), path=resolved, label=clean_label, created_at=time.time())
        state.watches.append(item)
        self._save(state)
        return item

    def dismiss(self, item_id: str) -> dict[str, Any]:
        """Remove one reminder or watch by its item id, and forget its runtime record."""
        wanted = one_line(item_id, 64)
        if not wanted:
            raise InvalidInput("pass the id companion_list shows for the item")
        state = self._load()
        for kind, items in (("reminder", state.reminders), ("watch", state.watches)):
            for index, item in enumerate(items):
                if item.id == wanted:
                    items.pop(index)
                    row_id = f"companion:{kind}:{wanted}"
                    state.runtime.pop(row_id, None)
                    self._save(state)
                    return {"id": wanted, "kind": kind, "trigger_id": row_id}
        raise ItemMissing(f"no reminder or watch with id {wanted!r}")

    # ── rows ───────────────────────────────────────────────────────────────────────────

    def rows(self) -> list[dict[str, Any]]:
        """Every trigger row this app currently contributes.

        Filtered by the surface switches, so a surface that is off contributes nothing, and
        with all three off this returns ``[]`` — the out-of-the-box state.
        """
        state = self._load()
        dropped = set(state.dropped)
        out: list[dict[str, Any]] = []
        if self._reminders_on:
            for item in state.reminders:
                row = self._reminder_row(item, state)
                if row is not None and row["id"] not in dropped:
                    out.append(row)
        if self._watchlist_on:
            for watch in state.watches:
                row = self._watch_row(watch, state)
                if row["id"] not in dropped:
                    out.append(row)
        if self._brief_at:
            row = self._brief_row(state)
            if row["id"] not in dropped:
                out.append(row)
        return out

    def row_for(self, trigger_id: str) -> dict[str, Any] | None:
        """One row by trigger id, or None. What core reads back after every write."""
        for row in self.rows():
            if row["id"] == trigger_id:
                return row
        return None

    def record_runtime(self, trigger_id: str, fields: dict[str, Any]) -> None:
        """Persist core's write-back for one row, so the next read shows it."""
        state = self._load()
        kept = {k: v for k, v in fields.items() if k in set(RUNTIME_FIELDS)}
        merged = dict(state.runtime.get(trigger_id) or {})
        merged.update(kept)
        state.runtime[trigger_id] = merged
        self._save(state)

    def drop_row(self, trigger_id: str) -> bool:
        """Retire one row: remove the item behind it if there is one, and remember the id.

        The ``dropped`` list exists for the ONE row that has no item — the day brief, which is
        driven by a setting this app cannot write. Without it, a brief the user retired from
        the Automations page would be served again on the next read, core's post-delete
        read-back would still find it, and core would quarantine this store. The brief's row id
        carries its time (``companion:day-brief:0830``), so changing the time in Settings mints
        a fresh id and the retirement does not become permanent.
        """
        served = self.row_for(trigger_id) is not None
        state = self._load()
        for kind, items in (("reminder", state.reminders), ("watch", state.watches)):
            for index, item in enumerate(list(items)):
                if trigger_id == f"companion:{kind}:{item.id}":
                    items.pop(index)
        state.runtime.pop(trigger_id, None)
        if trigger_id not in state.dropped:
            state.dropped.append(trigger_id)
        self._save(state)
        return served

    # ── row synthesis ──────────────────────────────────────────────────────────────────

    def _reminder_row(self, item: Reminder, state: _State) -> dict[str, Any] | None:
        row_id = f"companion:reminder:{item.id}"
        runtime = state.runtime.get(row_id) or {}
        if not item.recurring and _has_fired(runtime):
            # A delivered one-shot stops being an automation. Gated on run_count/last_fired_at
            # rather than on next_fire_at: core persists the NEXT fire time BEFORE it executes,
            # so gating on that would cancel the very fire that is about to happen.
            return None
        if item.recurring:
            spec: dict[str, Any] = {"kind": "cron", "expr": item.cron}
        else:
            spec = {"kind": "at", "at": item.at, "delete_after_run": True}
        if self.zone:
            spec["timezone"] = self.zone
        return _overlay(
            {
                "id": row_id,
                "name": f"Reminder — {item.title}",
                "kind": "clock",
                "enabled": True,
                "created_by": "user",
                "author": "",
                "spec": spec,
                "workflow": _notify("Companion reminder", item.title),
                "capabilities": {"providers": [NOTIFY_PROVIDER]},
                "overlap": "skip",
                "session": "fresh",
                "model_tier": "background",
                "delivery": "none",
                "failure_delivery": "inbox",
            },
            runtime,
        )

    def _watch_row(self, item: Watch, state: _State) -> dict[str, Any]:
        row_id = f"companion:watch:{item.id}"
        shown = item.label or item.path.rsplit("/", 1)[-1]
        return _overlay(
            {
                "id": row_id,
                "name": f"Watch — {shown}",
                "kind": "file",
                "enabled": True,
                "created_by": "user",
                "author": "",
                # `content` dedup, not `mtime`: a watchlist should speak up when a file really
                # changed, not when something touched it.
                "spec": {"paths": [item.path], "dedup": "content"},
                "workflow": _notify("Companion watch", shown),
                "capabilities": {"providers": [NOTIFY_PROVIDER]},
                "overlap": "skip",
                "session": "fresh",
                "model_tier": "background",
                "delivery": "none",
                "failure_delivery": "inbox",
            },
            state.runtime.get(row_id) or {},
        )

    def _brief_row(self, state: _State) -> dict[str, Any]:
        hour, minute = self._brief_at.split(":")
        row_id = f"companion:day-brief:{hour}{minute}"
        spec: dict[str, Any] = {"kind": "cron", "expr": f"{int(minute)} {int(hour)} * * *"}
        if self.zone:
            spec["timezone"] = self.zone
        return _overlay(
            {
                "id": row_id,
                "name": f"Day brief — {self._brief_at}",
                "kind": "clock",
                "enabled": True,
                "created_by": "user",
                "author": "",
                "spec": spec,
                "workflow": _notify(
                    "Day brief",
                    "Ask for companion_day_plan when you are ready to plan the day.",
                ),
                "capabilities": {"providers": [NOTIFY_PROVIDER]},
                "overlap": "skip",
                "session": "fresh",
                "model_tier": "background",
                "delivery": "none",
                "failure_delivery": "inbox",
            },
            state.runtime.get(row_id) or {},
        )

    # ── the day plan ───────────────────────────────────────────────────────────────────

    def day_plan(self, *, now: float | None = None) -> dict[str, Any]:
        """Today, as data: what is due, what is overdue, what recurs, what is watched.

        Rendering is the provider's job; deciding what "today" means is this one's, because it
        needs the configured zone.
        """
        current = time.time() if now is None else now
        tz = self.tzinfo()
        local = datetime.fromtimestamp(current, tz=tz)
        end_of_day = (
            (local + timedelta(days=1))
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )
        state = self._load()
        due: list[dict[str, Any]] = []
        overdue: list[dict[str, Any]] = []
        recurring: list[dict[str, Any]] = []
        delivered = 0
        for item in state.reminders:
            runtime = state.runtime.get(f"companion:reminder:{item.id}") or {}
            entry = {
                "id": item.id,
                "title": item.title,
                "note": item.note,
                "at": item.at,
                "cron": item.cron,
                "when": (
                    datetime.fromtimestamp(item.at, tz=tz).strftime("%H:%M") if item.at else ""
                ),
                "date": (
                    datetime.fromtimestamp(item.at, tz=tz).strftime("%Y-%m-%d") if item.at else ""
                ),
            }
            if item.recurring:
                recurring.append(entry)
            elif _has_fired(runtime):
                delivered += 1
            elif item.at < current:
                overdue.append(entry)
            elif item.at < end_of_day:
                due.append(entry)
        due.sort(key=lambda e: e["at"])
        overdue.sort(key=lambda e: e["at"])
        return {
            "date": local.strftime("%Y-%m-%d"),
            "timezone": self.zone or "UTC (no zone configured)",
            "surfaces": self.surface_state(),
            "brief_at": self._brief_at,
            "due_today": due,
            "overdue": overdue,
            "recurring": recurring,
            "delivered": delivered,
            "watches": [w.to_dict() for w in state.watches],
            "later": [
                r.to_dict()
                for r in state.reminders
                if not r.recurring
                and r.at >= end_of_day
                and not _has_fired(state.runtime.get(f"companion:reminder:{r.id}") or {})
            ],
        }


# ── module helpers ─────────────────────────────────────────────────────────────────────


def _new_id() -> str:
    return secrets.token_hex(_ID_BYTES)


def _notify(title: str, body: str) -> dict[str, Any]:
    """The only ``workflow`` block this app ever produces.

    Two things are structural here rather than configurable: the action is ``notify``, and
    both template fields have ``$`` excluded. A companion row cannot be made to run a prompt,
    a workflow or an agent — there is no field in the store where such a thing could be
    written, and this function is the only place a ``workflow`` block is minted.
    """
    return {
        "provider": NOTIFY_PROVIDER,
        "config": {
            "kind": "info",
            "title_template": template_safe(title),
            "body_template": template_safe(body),
        },
    }


def _overlay(row: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    """``row`` with core's persisted runtime rollups written over it."""
    out = dict(row)
    for key in RUNTIME_FIELDS:
        if key in runtime:
            out[key] = runtime[key]
    return out


def _has_fired(runtime: dict[str, Any]) -> bool:
    """Whether core has recorded a fire for this row."""
    try:
        count = int(runtime.get("run_count") or 0)
    except (TypeError, ValueError):
        count = 0
    return count > 0 or bool(str(runtime.get("last_fired_at") or "").strip())


def _reminder_from(row: Any) -> Reminder | None:
    if not isinstance(row, dict):
        return None
    item_id = one_line(str(row.get("id") or ""), 64)
    title = one_line(str(row.get("title") or ""), TITLE_MAX)
    if not item_id or not title:
        return None
    return Reminder(
        id=item_id,
        title=title,
        note=str(row.get("note") or "")[:NOTE_MAX],
        at=_float(row.get("at")),
        cron=one_line(str(row.get("cron") or ""), CRON_MAX),
        created_at=_float(row.get("created_at")),
    )


def _watch_from(row: Any) -> Watch | None:
    if not isinstance(row, dict):
        return None
    item_id = one_line(str(row.get("id") or ""), 64)
    raw_path = str(row.get("path") or "")
    if not item_id or not raw_path:
        return None
    try:
        # Re-validated on the way IN, not just on the way out: a hand-edited or synced store
        # is untrusted input, and the path is about to become a glob core walks.
        path = validate_watch_path(raw_path)
    except InvalidInput:
        return None
    return Watch(
        id=item_id,
        path=path,
        label=one_line(str(row.get("label") or ""), LABEL_MAX),
        created_at=_float(row.get("created_at")),
    )


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
