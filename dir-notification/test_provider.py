"""Folder Notification Drop tests — pure filesystem, no network, no real drop folder.

Every case drives a ``tmp_path`` drop folder. The cases are chosen to pin the three things
the delivery contract can get wrong rather than to cover lines: an over-eager ``addresses``
(which silently swallows a note a second backend could deliver), a ``deliver`` that returns
True on a failed write (which stamps ``routed_to`` on nothing), and a path built from
attacker-shaped text (the addressee arrives from a shared store).
"""

from __future__ import annotations

import json

from provider import DirNotificationProvider, create_provider


def _note(addressee: str, title: str = "Run finished", ts: str = "20260921T101500") -> dict:
    return {
        "kind": "workflow",
        "title": title,
        "body": "The nightly digest run completed.",
        "ts": ts,
        "addressee": addressee,
        "withheld_reason": "foreign_addressee",
    }


# ── addresses: the routing decision ──────────────────────────────────────────────────


def test_addresses_only_rostered_usernames(tmp_path):
    p = create_provider({"drop_dir": str(tmp_path), "roster": "sam, dana"})
    assert p.addresses("sam") is True
    assert p.addresses("dana") is True
    assert p.addresses("kim") is False


def test_addresses_is_case_insensitive_both_ways(tmp_path):
    # Core lowercases the addressee before asking, so a roster typed in mixed case must still
    # match or the setting silently addresses nobody.
    p = create_provider({"drop_dir": str(tmp_path), "roster": "Sam, DANA"})
    assert p.addresses("sam") is True
    assert p.addresses("dana") is True


def test_unconfigured_declines_rather_than_accepting_and_failing(tmp_path):
    # An accept-then-fail takes the note out of every other backend's reach for nothing:
    # `deliver_to_addressee` stops at the first backend that says yes.
    assert create_provider({"roster": "sam"}).addresses("sam") is False
    assert create_provider({"drop_dir": str(tmp_path)}).addresses("sam") is False
    assert create_provider({}).addresses("sam") is False


# ── deliver: acceptance must mean the note landed ────────────────────────────────────


def test_deliver_writes_one_file_per_note_under_the_addressee(tmp_path):
    p = create_provider({"drop_dir": str(tmp_path), "roster": "sam"})
    assert p.deliver(_note("sam")) is True
    files = sorted((tmp_path / "sam").glob("*.json"))
    assert len(files) == 1
    written = json.loads(files[0].read_text(encoding="utf-8"))
    assert written["title"] == "Run finished"
    assert written["body"] == "The nightly digest run completed."
    assert written["addressee"] == "sam"


def test_deliver_leaves_no_partial_file_behind(tmp_path):
    p = create_provider({"drop_dir": str(tmp_path), "roster": "sam"})
    assert p.deliver(_note("sam")) is True
    assert list((tmp_path / "sam").glob("*.part")) == []


def test_deliver_re_reads_the_addressee_and_declines_a_foreign_note(tmp_path):
    # The registry hands over a note, not a username. A backend that trusted the preceding
    # `addresses` call would file somebody else's note in a rostered person's folder.
    p = create_provider({"drop_dir": str(tmp_path), "roster": "sam"})
    assert p.deliver(_note("kim")) is False
    assert not (tmp_path / "kim").exists()


def test_deliver_returns_false_when_the_write_cannot_land(tmp_path):
    # A file where the addressee folder must go: mkdir fails, so nothing was delivered and the
    # note must NOT be stamped `routed_to: dir-notification`.
    blocker = tmp_path / "sam"
    blocker.write_text("not a directory", encoding="utf-8")
    p = create_provider({"drop_dir": str(tmp_path), "roster": "sam"})
    assert p.deliver(_note("sam")) is False


def test_deliver_never_writes_outside_the_drop_folder(tmp_path):
    # The addressee arrives from a shared store and the title is arbitrary text; neither is
    # allowed to shape a path.
    root = tmp_path / "drop"
    p = DirNotificationProvider(str(root), {"../../etc/passwd"})
    assert p.deliver(_note("../../etc/passwd", title="../../../oops")) is True
    written = list(root.rglob("*.json"))
    assert len(written) == 1
    assert root in written[0].parents
    assert ".." not in str(written[0].relative_to(root))


# ── delivery_name: the registry key and the recorded route ───────────────────────────


def test_delivery_name_is_the_providers_own_stable_name(tmp_path):
    p = create_provider({"drop_dir": str(tmp_path), "roster": "sam"})
    assert p.delivery_name == "dir-notification"
