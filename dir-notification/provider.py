"""Folder Notification Drop — the reference ``type=notification`` provider.

This is the smallest bundle that implements the delivery contract honestly. It exists
because ``notification`` was the one provider type with no example anywhere: no first-party
app, no published exemplar, and not even a core reference implementation — core ships the
ABC and the registry and nothing that implements them.

**What the contract is for.** ``DashboardState.notify`` is the single choke point for every
notification, and every destination behind it is local: a toast on this dashboard, a row in
this digest, a push to this owner's phone. Once a shared store contributes rows somebody else
owns (`TSE2-1`..`TSE2-3`), a note can be *about* a teammate, and firing it here spends the
wrong person's attention. So a foreign-addressed note is recorded locally, fired nowhere
locally, and offered to whichever registered backend says it can reach the addressee. This
is that backend.

**Why a folder is an honest delivery and not a toy.** The note has to leave this machine, and
PersonalClaw already ships four sync transports whose whole job is making one folder appear on
two machines (`dir-sync`, `git-sync`, `rsync-sync`, `s3-sync`). Writing into that folder
therefore really does reach the other person, with no credential, no network permission, and
no vendor. It is also the right shape for a *synchronous* contract: ``deliver`` is called from
``notify``, which is not async and must never block, and a local file write is the one delivery
that finishes immediately. A network backend would have to hand off to its own thread.

**The three things a delivery backend has to get right**, all visible below:

1. ``addresses`` is the ROUTING decision and it belongs to the provider. Core knows the
   addressee string and nothing else about who that is. Answering ``True`` for a username this
   folder cannot actually reach would consume a note that a second backend could have
   delivered — first acceptance wins, so an over-eager ``addresses`` is a silent black hole.
2. ``deliver`` returns whether the note was ACCEPTED, and the registry records the accepting
   backend on the note. Returning ``True`` after a failed write would stamp
   ``routed_to: dir-notification`` on something that went nowhere, which reads as delivered.
   Every failure path below returns ``False``.
3. ``delivery_name`` is the provider's OWN name, not the app's, because it is both the
   registry key and the string the note records — and it is the key a disable removes. A
   phantom route is worse than a visibly withheld note.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Mapping

from personalclaw.sdk.notification import NotificationDeliveryProvider

logger = logging.getLogger(__name__)

#: Everything outside this becomes ``_`` in a filename. A note's title is arbitrary text and
#: the addressee arrives from a shared store, so neither is allowed to shape a path: `..` and
#: separators are what turn a delivery into a write outside the drop folder.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(text: str, *, limit: int = 60) -> str:
    cleaned = _UNSAFE.sub("_", str(text or "")).strip("._-")
    return cleaned[:limit] or "note"


def _normalize(username: object) -> str:
    """The comparison form of a username. Core lowercases the addressee before asking, so a
    roster entry typed ``Sam`` must match ``sam`` or the roster silently addresses nobody."""
    return str(username or "").strip().lower()


class DirNotificationProvider(NotificationDeliveryProvider):
    """Writes each accepted note to ``<drop_dir>/<addressee>/<ts>-<title>.json``."""

    def __init__(self, drop_dir: str, roster: set[str]) -> None:
        self._drop_dir = drop_dir
        self._roster = roster

    @property
    def delivery_name(self) -> str:
        # The provider's own name, and stable: it is what a delivered note records in
        # `routed_to` and the key the registry removes on disable.
        return "dir-notification"

    def addresses(self, username: str) -> bool:
        """``True`` only for a rostered username, and only when a folder is configured.

        Unconfigured declines rather than accepting-and-failing. A backend that says yes and
        then cannot write has taken the note out of every other backend's reach for nothing.
        """
        if not self._drop_dir or not self._roster:
            return False
        return _normalize(username) in self._roster

    def deliver(self, note: Mapping[str, Any]) -> bool:
        """Write the note. ``False`` on any failure — never claim a delivery that did not land.

        The addressee is re-read from the note rather than taken on trust from the preceding
        ``addresses`` call: the registry passes the note, and the file must land in the folder
        of the person it actually names.
        """
        addressee = _normalize(note.get("addressee"))
        if addressee not in self._roster:
            return False
        try:
            target = Path(os.path.expandvars(self._drop_dir)).expanduser() / _slug(addressee)
            target.mkdir(parents=True, exist_ok=True)
            stamp = str(note.get("ts") or "") or time.strftime("%Y%m%dT%H%M%S")
            path = target / f"{_slug(stamp, limit=32)}-{_slug(note.get('title'))}.json"
            # Written whole: an app the owner installed sees the same title and body an
            # `inbox` or `channel` provider already handles. Written via a temp file + rename
            # so a syncing folder never publishes a half-written note.
            tmp = path.with_suffix(".json.part")
            tmp.write_text(json.dumps(dict(note), indent=2, default=str), encoding="utf-8")
            tmp.replace(path)
            return True
        except Exception:
            # Logged, not raised. `deliver_to_addressee` skips a backend that raises, so
            # raising here would work too — but returning False is the contract's own way of
            # saying "not accepted", and it keeps the reason in this app's log.
            logger.warning("dir-notification: could not write note for %r", addressee, exc_info=True)
            return False


def create_provider(config: dict[str, Any]) -> DirNotificationProvider:
    """Build the provider from the instance settings.

    Both settings this reads are DECLARED in ``app.json`` — the config form renders only
    declared properties, so an undeclared setting is one the operator can never set, and this
    read would take its default forever. The ``settings-declared-reads`` CI rail holds that.
    """
    roster = {
        _normalize(part) for part in str(config.get("roster") or "").split(",") if _normalize(part)
    }
    return DirNotificationProvider(str(config.get("drop_dir") or "").strip(), roster)
