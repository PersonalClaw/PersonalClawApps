"""slack-channel app: the inbox MessageSourceProvider.

The app registers TWO providers — the ``channel`` transport (interactive chat) and
this ``inbox`` source — so Slack messages reach the generic Inbox through core's
vendor-neutral message-source seam instead of a Slack-shaped path in core.

Drives the provider through the bundle's own ``SlackClientOps`` ABC (a stub client),
which is exactly what production's ``RealSlackClient`` satisfies — so these tests
never touch the Slack API and never need a live token.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# App dir on sys.path so this root-level test imports the app's slack_runtime
# package the way the gateway's app loader does (mirrors test_provider.py, which
# inlines this because it lives at the app root rather than under tests/).
_APP_DIR = Path(__file__).resolve().parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

import pytest  # noqa: E402

from slack_runtime.client import SlackClientOps  # noqa: E402
from slack_runtime.inbox_source import SlackInboxSource, create_provider  # noqa: E402


@pytest.fixture(autouse=True)
def _scratch_home(tmp_path, monkeypatch):
    """The source reads this app's store for its token, so this root-level file (which
    ``tests/conftest.py`` does not cover) points the home at a scratch directory."""
    monkeypatch.setenv("PERSONALCLAW_HOME", str(tmp_path / "home"))


class StubClient(SlackClientOps):
    """Minimal SlackClientOps: records calls, replays canned history.

    Only the members the inbox source actually uses are implemented; the rest of
    the ABC's abstract methods get trivial bodies so the class instantiates.
    """

    def __init__(self, history=None, users=None, fail_channels=(), raw=None):
        self._history = history or {}
        # `raw` bypasses this stub's own ts filtering/sorting and is returned
        # verbatim — needed to hand the provider a payload the stub itself could
        # not order (e.g. a malformed ts).
        self._raw = raw or {}
        self._users = users or {}
        self._fail = set(fail_channels)
        self.posts: list[tuple] = []
        self.reactions: list[tuple] = []
        self.history_calls: list[tuple] = []
        self.user_info_calls: list[str] = []

    async def fetch_history(self, channel, oldest, limit=200, *, latest=""):
        self.history_calls.append((channel, oldest, latest, limit))
        if channel in self._fail:
            raise RuntimeError("slack api down")
        if channel in self._raw:
            return list(self._raw[channel]), False
        # What conversations.history does with exclusive bounds: the page of messages strictly
        # between `oldest` and `latest` (now, when absent) closest to `latest`, newest-first,
        # and whether there are more below it.
        top = float(latest) if latest else float("inf")
        msgs = [
            m for m in self._history.get(channel, []) if float(oldest) < float(m["ts"]) < top
        ]
        msgs.sort(key=lambda m: float(m["ts"]), reverse=True)
        return msgs[:limit], len(msgs) > limit

    async def post_message(self, channel, text, thread_ts=None, unfurl_links=None, unfurl_media=None):
        if channel == "C_BAD":
            raise RuntimeError("cannot post")
        self.posts.append((channel, text, thread_ts))
        return "1700000099.000000"

    async def add_reaction(self, channel, ts, emoji):
        if channel == "C_BAD":
            raise RuntimeError("cannot react")
        self.reactions.append((channel, ts, emoji))

    async def get_user_info(self, user_id):
        self.user_info_calls.append(user_id)
        return self._users.get(user_id, {})

    # ── unused ABC surface ────────────────────────────────────────────────────
    async def post_blocks(self, *a, **k): return ""
    async def update_message(self, *a, **k): return None
    async def delete_message(self, *a, **k): return None
    async def remove_reaction(self, *a, **k): return None
    async def upload_file(self, *a, **k): return None
    async def open_dm(self, *a, **k): return ""
    async def post_ephemeral(self, *a, **k): return None
    async def views_publish(self, *a, **k): return None
    async def get_prompts(self, *a, **k): return []


def _msg(ts, user="U_ALICE", text="hello", **extra):
    return {"ts": ts, "user": user, "text": text, **extra}


def _source(**kw):
    return SlackInboxSource({"bot_token": "fake-bot-token-test"}, client=StubClient(**kw))


#: A channel this source polled before, empty then: where the steady-state tests start. A
#: channel's first poll (no cursor at all) only records where it is — see the tests below.
_POLLED_BEFORE = {"C1": "0"}


# ── manifest ──────────────────────────────────────────────────────────────────


def test_manifest_declares_at_least_two_providers():
    """The core claim: a channel app that only registers a channel leaves its
    messages outside the generic inbox. The manifest must declare BOTH.

    Asserted as a SUBSET, not as set equality. The original spelling was
    ``types == {"channel", "inbox"}``, which froze the vendor-completeness checklist at the
    bar available when CE-8 shipped — so the next seam this app adopted turned the own
    test red for succeeding. It did, at CE-10 (``trigger_source``). What this test owns is
    that the inbox seam is declared and points at THIS module; which other seams the bundle
    has grown is the completeness rail's business, not this file's.
    """
    manifest = json.loads((_APP_DIR / "app.json").read_text(encoding="utf-8"))
    declared = ([manifest["provider"]] if manifest.get("provider") else []) + manifest.get(
        "providers", []
    )
    assert len(declared) >= 2, declared
    types = {p["type"] for p in declared}
    assert {"channel", "inbox"} <= types, types
    inbox = next(p for p in declared if p["type"] == "inbox")
    assert inbox["implementation"] == "slack_runtime.inbox_source:create_provider"


def test_create_provider_returns_the_inbox_source():
    assert type(create_provider({})).__name__ == "SlackInboxSource"


def test_source_name_is_the_vendor_neutral_key():
    """Core resolves a source BY NAME; `source` is stamped on every inbox item."""
    assert _source().source_name == "slack"


# Which token the source's client is built with (the app's store, a token saved after enable,
# the shared credential as the fallback) is proven in tests/test_inbox_source_saved_token.py.


# ── poll ──────────────────────────────────────────────────────────────────────


def test_poll_maps_messages_and_advances_the_checkpoint():
    src = _source(
        history={"C1": [_msg("1700000001.000100"), _msg("1700000002.000200", text="second")]},
        users={"U_ALICE": {"real_name": "Alice Example"}},
    )
    msgs, cursors = asyncio.run(src.poll(["C1"], _POLLED_BEFORE, "U_ME"))

    assert [m.id for m in msgs] == ["1700000001.000100", "1700000002.000200"]  # oldest-first
    assert [m.text for m in msgs] == ["hello", "second"]
    first = msgs[0]
    assert first.channel_id == "C1"
    assert first.sender_id == "U_ALICE"
    assert first.sender_name == "Alice Example"
    assert first.timestamp == 1700000001.0001
    assert first.is_dm is False
    # Cursor is the NEWEST ts seen, so the next poll resumes past it.
    assert cursors["C1"] == "1700000002.000200"


def test_poll_passes_the_checkpoint_as_oldest_and_does_not_redeliver():
    """`oldest` is exclusive, so the message AT the cursor must not come back —
    the property that makes the ts a correct resume cursor."""
    src = _source(history={"C1": [_msg("1700000001.000100"), _msg("1700000002.000200")]})
    msgs, cursors = asyncio.run(src.poll(["C1"], {"C1": "1700000001.000100"}, "U_ME"))

    assert src._client.history_calls == [("C1", "1700000001.000100", "", 200)]
    assert [m.id for m in msgs] == ["1700000002.000200"]
    assert cursors["C1"] == "1700000002.000200"


def test_poll_with_nothing_new_keeps_the_checkpoint():
    src = _source(history={"C1": [_msg("1700000001.000100")]})
    msgs, cursors = asyncio.run(src.poll(["C1"], {"C1": "1700000001.000100"}, "U_ME"))
    assert msgs == []
    assert cursors["C1"] == "1700000001.000100"


def test_poll_skips_bot_own_and_authorless_messages_but_still_advances():
    """The filtered messages were SEEN and judged, so the cursor moves past them —
    otherwise a chatty bot would re-deliver the same window forever."""
    src = _source(
        history={
            "C1": [
                _msg("1700000001.000100", user="U_ME"),               # our own
                _msg("1700000002.000200", user="U_BOT", bot_id="B1"),  # a bot
                _msg("1700000003.000300", user=""),                    # join/topic, no author
                _msg("1700000004.000400", user="U_ALICE", text="real"),
            ]
        }
    )
    msgs, cursors = asyncio.run(src.poll(["C1"], _POLLED_BEFORE, "U_ME"))

    assert [m.text for m in msgs] == ["real"]
    assert cursors["C1"] == "1700000004.000400"


def test_poll_keeps_the_old_checkpoint_when_a_channel_errors():
    """A transient API error must NOT look like 'nothing new' — advancing here
    would silently consume the unread window."""
    src = _source(
        history={"C_OK": [_msg("1700000005.000500")]},
        fail_channels=["C_ERR"],
    )
    checkpoints = {"C_OK": "1700000000.000000", "C_ERR": "1699999999.000000"}
    msgs, cursors = asyncio.run(src.poll(["C_OK", "C_ERR"], checkpoints, "U_ME"))

    assert [m.channel_id for m in msgs] == ["C_OK"]
    assert cursors["C_OK"] == "1700000005.000500"
    assert cursors["C_ERR"] == "1699999999.000000"  # untouched, so the next poll retries


def test_poll_carries_thread_id_and_marks_dms():
    src = _source(history={"D9": [_msg("1700000001.000100", thread_ts="1700000000.000000")]})
    msgs, _ = asyncio.run(src.poll(["D9"], {"D9": "0"}, "U_ME"))
    assert msgs[0].thread_id == "1700000000.000000"
    assert msgs[0].is_dm is True  # D-prefixed channel


def test_poll_ignores_a_malformed_ts_for_the_cursor():
    """An unparseable ts must never win the cursor comparison, else it poisons the
    checkpoint and every later poll re-reads or skips."""
    src = _source(
        raw={
            "C1": [
                {"ts": "not-a-ts", "user": "U_ALICE", "text": "junk"},
                _msg("1700000001.000100"),
            ]
        }
    )
    msgs, cursors = asyncio.run(src.poll(["C1"], _POLLED_BEFORE, "U_ME"))
    # The malformed message still surfaces (timestamp 0.0), but never wins the cursor.
    assert {m.id for m in msgs} == {"not-a-ts", "1700000001.000100"}
    assert cursors["C1"] == "1700000001.000100"


# ── nothing is skipped, however much arrived (ledger: 50 per poll) ─────────────


def _burst(n, start=1700001000):
    """``n`` messages, a second apart, after ``start``."""
    return [_msg(f"{start + i}.000100", text=f"m{i}") for i in range(n)]


def test_more_new_messages_than_one_page_all_arrive_in_one_poll():
    """A poll read one page of 50, newest-first, and moved the cursor past the rest: 450 new
    messages surfaced 50 and skipped 400. It pages down to the cursor now."""
    src = _source(history={"C1": _burst(450)})
    msgs, cursors = asyncio.run(src.poll(["C1"], {"C1": "1700000000.000000"}, "U_ME"))

    assert [m.text for m in msgs] == [f"m{i}" for i in range(450)]  # every one, oldest first
    assert cursors["C1"] == "1700001449.000100"
    assert "slack-gap:C1" not in cursors


def test_more_than_a_poll_reads_arrive_over_the_next_polls_none_skipped(monkeypatch):
    """Past the per-poll bound, the newest arrive now, the rest is the channel's gap, and the next
    polls read the gap before anything newer: every message arrives once."""
    import slack_runtime.inbox_source as mod

    monkeypatch.setattr(mod, "_PAGE_SIZE", 10)
    monkeypatch.setattr(mod, "_PAGES_PER_POLL", 2)
    history = _burst(55)
    src = _source(history={"C1": history})
    cursors = {"C1": "1700000000.000000"}
    seen: list[str] = []
    polls = 0
    while True:
        msgs, cursors = asyncio.run(src.poll(["C1"], cursors, "U_ME"))
        polls += 1
        if not msgs:
            break
        seen += [m.text for m in msgs]
        if polls == 1:
            assert [m.text for m in msgs] == [f"m{i}" for i in range(35, 55)]  # the newest 20
            assert cursors["slack-gap:C1"] == "1700000000.000000:1700001035.000100"
        if polls == 2:
            # Arrived while the gap is open: read after it closes, never skipped.
            history.append(_msg("1700009999.000100", text="late"))
    assert sorted(seen) == sorted([f"m{i}" for i in range(55)] + ["late"])
    assert len(seen) == len(set(seen)), "a message arrived twice"
    assert cursors["slack-gap:C1"] == "", "the gap never closed"
    assert cursors["C1"] == "1700009999.000100"


def test_a_failed_read_keeps_the_gap_and_the_cursor():
    src = _source(fail_channels=["C1"], history={"C_OK": [_msg("1700000005.000500")]})
    before = {
        "C1": "1700001050.000100",
        "slack-gap:C1": "1700000000.000000:1700001030.000100",
        "C_OK": "0",
    }
    _, cursors = asyncio.run(src.poll(["C1", "C_OK"], before, "U_ME"))
    assert cursors["C1"] == before["C1"]
    assert cursors["slack-gap:C1"] == before["slack-gap:C1"]


# ── reply / react / history / names ───────────────────────────────────────────


def test_send_reply_posts_into_the_thread():
    src = _source()
    assert asyncio.run(src.send_reply("C1", "ack", "1700000000.000000")) is True
    assert src._client.posts == [("C1", "ack", "1700000000.000000")]


def test_send_reply_says_why_slack_did_not_take_it():
    """Falsy, and its ``str()`` is what core's inbox shows the owner who pressed Send."""
    src = _source()
    result = asyncio.run(src.send_reply("C_BAD", "ack"))
    assert not result
    assert str(result) == "Slack refused it in C_BAD (cannot post)."


# ── a channel's first poll, and a poll that reads nothing (ledgers 250, 279, 280) ──


def test_a_channels_first_poll_surfaces_none_of_its_backlog():
    """Every message surfaced raises an inbox event, so a newly watched channel's history
    would fire the owner's inbox automations at once. It records where the channel is."""
    src = _source(history={"C1": [_msg("1700000001.000100"), _msg("1700000002.000200")]})
    msgs, cursors = asyncio.run(src.poll(["C1"], {}, "U_ME"))
    assert msgs == []
    assert cursors == {"C1": "1700000002.000200"}
    assert src._client.history_calls == [("C1", "0", "", 1)], "it read more than where it is"

    src._client._history["C1"].append(_msg("1700000003.000300", text="after"))
    msgs, _ = asyncio.run(src.poll(["C1"], cursors, "U_ME"))
    assert [m.text for m in msgs] == ["after"]


def test_an_empty_channels_first_poll_starts_at_the_beginning():
    src = _source(history={"C1": []})
    msgs, cursors = asyncio.run(src.poll(["C1"], {}, "U_ME"))
    assert msgs == [] and cursors == {"C1": "0"}
    src._client._history["C1"].append(_msg("1700000001.000100", text="first"))
    msgs, _ = asyncio.run(src.poll(["C1"], cursors, "U_ME"))
    assert [m.text for m in msgs] == ["first"]


def test_a_newly_watched_channel_starts_on_its_own():
    src = _source(history={"C1": [_msg("1700000001.000100")], "C2": [_msg("1700000005.000500")]})
    msgs, cursors = asyncio.run(
        src.poll(["C1", "C2"], {"C1": "1700000000.000000"}, "U_ME")
    )
    assert [m.channel_id for m in msgs] == ["C1"]
    assert cursors == {"C1": "1700000001.000100", "C2": "1700000005.000500"}


def test_a_poll_that_reads_no_channel_says_why():
    """It returned nothing and logged at debug, so a revoked token read like a quiet
    workspace. Core shows the sentence as this source's health."""
    from slack_runtime.inbox_source import SlackUnreadable

    src = _source(fail_channels=["C1", "C2"])
    with pytest.raises(SlackUnreadable) as failed:
        asyncio.run(src.poll(["C1", "C2"], {"C1": "0", "C2": "0"}, "U_ME"))
    assert str(failed.value) == (
        "none of the watched Slack channels could be read: C1 (slack api down), "
        "C2 (slack api down)"
    )


def test_one_channel_failing_is_a_warning_and_the_rest_arrive(caplog):
    import logging

    src = _source(history={"C_OK": [_msg("1700000005.000500")]}, fail_channels=["C_ERR"])
    with caplog.at_level(logging.WARNING, logger="slack_runtime.inbox_source"):
        msgs, _ = asyncio.run(src.poll(["C_OK", "C_ERR"], {"C_OK": "0", "C_ERR": "0"}, "U_ME"))
    assert [m.channel_id for m in msgs] == ["C_OK"]
    assert "channel C_ERR could not be read (slack api down)" in caplog.text


def test_slacks_own_word_for_a_failure_is_the_reason():
    from slack_runtime.inbox_source import _slack_reason

    class _ApiError(Exception):
        response = {"ok": False, "error": "not_in_channel"}

    assert _slack_reason(_ApiError("The request to the Slack API failed.\n...")) == "not_in_channel"
    assert _slack_reason(RuntimeError("boom")) == "boom"


def test_add_reaction_reports_success_and_failure():
    src = _source()
    assert asyncio.run(src.add_reaction("C1", "1700000001.000100", "eyes")) is True
    assert src._client.reactions == [("C1", "1700000001.000100", "eyes")]
    assert asyncio.run(src.add_reaction("C_BAD", "1700000001.000100", "eyes")) is False


def test_get_channel_history_returns_raw_dicts_and_degrades_to_empty():
    src = _source(history={"C1": [_msg("1700000001.000100")]}, fail_channels=["C_ERR"])
    rows = asyncio.run(src.get_channel_history("C1", "0", 10))
    assert [r["ts"] for r in rows] == ["1700000001.000100"]
    # Not cursor-bearing: best-effort context degrades to "no history".
    assert asyncio.run(src.get_channel_history("C_ERR", "0", 10)) == []


def test_resolve_user_name_prefers_real_name_and_caches():
    src = _source(users={"U_ALICE": {"real_name": "Alice Example", "name": "alice"}})
    assert asyncio.run(src.resolve_user_name("U_ALICE")) == "Alice Example"
    assert asyncio.run(src.resolve_user_name("U_ALICE")) == "Alice Example"
    assert src._client.user_info_calls == ["U_ALICE"]  # second call served from cache


def test_resolve_user_name_falls_back_to_handle_then_id():
    src = _source(users={"U_BOB": {"name": "bob"}})
    assert asyncio.run(src.resolve_user_name("U_BOB")) == "bob"
    # Unknown user: the id itself, never an empty label in the UI.
    assert asyncio.run(src.resolve_user_name("U_GHOST")) == "U_GHOST"
