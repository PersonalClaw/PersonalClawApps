"""DiscordDelivery — splitting, throttled edit-streaming, button approvals.

The REST client is a fake implementing the ``DiscordAPI`` ABC (records calls, hands
back incrementing message ids); the throttle clock is injected so the edit-rate
contract is pinned without sleeping.

The centrepiece is the approval round-trip, because it spans both halves of the app:
a prompt goes out over REST as a button row, the press comes back as an
``INTERACTION_CREATE`` from the gateway, and the future the caller is awaiting has to
resolve from that — while the interaction is acknowledged inside Discord's 3-second
window and the decided prompt loses its buttons."""

from __future__ import annotations

import asyncio

import pytest

from discord_runtime.api import (
    BUTTON_STYLE_DANGER,
    BUTTON_STYLE_SUCCESS,
    COMPONENT_ACTION_ROW,
    COMPONENT_BUTTON,
    DISCORD_MAX_TEXT,
    DiscordAPI,
)
from discord_runtime.delivery import (
    _EDIT_MIN_INTERVAL,
    INTERACTION_TYPE_COMPONENT,
    DiscordDelivery,
    split_message,
)


class FakeAPI(DiscordAPI):
    def __init__(self, *, fail: set[str] | None = None):
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.deleted: list[dict] = []
        self.dms: list[str] = []
        self.uploads: list[dict] = []
        self.acks: list[dict] = []
        self.reactions: list[dict] = []
        self.typing: list[str] = []
        self.users: dict[str, dict] = {}
        self.channels: dict[str, dict] = {}
        self._fail = fail or set()
        self._id = 0

    def _next(self) -> str:
        self._id += 1
        return str(self._id)

    def _boom(self, name: str) -> None:
        if name in self._fail:
            raise RuntimeError(f"{name} failed")

    async def get_gateway_bot(self):
        return {"url": "wss://gateway.discord.gg", "session_start_limit": {"remaining": 1000}}

    async def create_message(self, channel_id, content, *, components=None, message_reference=None):
        self._boom("create_message")
        mid = self._next()
        self.sent.append({"channel_id": channel_id, "content": content,
                          "components": components, "id": mid})
        return {"id": mid}

    async def edit_message(self, channel_id, message_id, content, *, components=None):
        self._boom("edit_message")
        self.edits.append({"channel_id": channel_id, "message_id": message_id,
                           "content": content, "components": components})
        return {"id": message_id}

    async def delete_message(self, channel_id, message_id):
        self._boom("delete_message")
        self.deleted.append({"channel_id": channel_id, "message_id": message_id})

    async def create_dm(self, user_id):
        self._boom("create_dm")
        self.dms.append(str(user_id))
        return {"id": f"dm-{user_id}", "type": 1}

    async def upload_file(self, channel_id, file_path, *, filename="", content=""):
        mid = self._next()
        self.uploads.append({"channel_id": channel_id, "path": file_path,
                             "filename": filename, "content": content})
        return {"id": mid}

    async def get_channel(self, channel_id):
        self._boom("get_channel")
        return self.channels.get(str(channel_id), {"id": str(channel_id), "name": "general"})

    async def get_user(self, user_id):
        self._boom("get_user")
        return self.users.get(str(user_id), {"id": str(user_id), "username": "someone"})

    async def create_interaction_response(self, interaction_id, interaction_token, *,
                                          callback_type=6):
        self._boom("create_interaction_response")
        self.acks.append({"id": interaction_id, "token": interaction_token,
                          "type": callback_type})

    async def add_reaction(self, channel_id, message_id, emoji):
        self._boom("add_reaction")
        self.reactions.append({"channel_id": channel_id, "message_id": message_id,
                               "emoji": emoji})

    async def trigger_typing(self, channel_id):
        self._boom("trigger_typing")
        self.typing.append(str(channel_id))


def _delivery(owner="42", **kwargs):
    return DiscordDelivery(FakeAPI(**kwargs), lambda: owner)


class TestSplitMessage:
    def test_short_text_is_one_part(self):
        assert split_message("hi") == ["hi"]

    def test_empty_text_is_no_parts(self):
        assert split_message("") == []

    def test_splits_at_2000(self):
        parts = split_message("x" * 5000)
        assert len(parts) == 3
        assert all(len(p) <= DISCORD_MAX_TEXT for p in parts)

    def test_prefers_newline_boundaries(self):
        text = ("a" * 1500) + "\n" + ("b" * 1000)
        parts = split_message(text)
        assert parts[0] == "a" * 1500
        assert parts[1] == "b" * 1000

    def test_hard_splits_when_no_newline(self):
        parts = split_message("y" * 2500)
        assert len(parts[0]) == DISCORD_MAX_TEXT

    def test_a_code_block_cut_in_two_is_closed_and_reopened(self):
        """Discord renders each message's markdown on its own. A cut inside a code block left
        the first message with its fence open and the next showing the rest of the code as
        markdown — `*`, `_` and `#` lines formatted, indentation gone."""
        code = "    total = price * qty  # __init__ stays literal\n" * 120
        text = "Here is the fix:\n```python\n" + code + "```\nThat is all."
        parts = split_message(text)
        assert len(parts) >= 3
        assert all(len(p) <= DISCORD_MAX_TEXT for p in parts)
        for part in parts:
            fences = [line for line in part.split("\n") if line.lstrip().startswith("```")]
            assert len(fences) % 2 == 0, f"a part leaves a code block open: {part[-60:]!r}"
        for part in parts[1:]:
            assert part.startswith("```python\n"), "the code block was not reopened"
        joined = "\n".join(parts)
        assert joined.count("total = price * qty") == 120
        assert parts[-1].endswith("```\nThat is all.")

    def test_a_line_longer_than_a_part_is_cut_between_words(self):
        parts = split_message("words " * 800)
        assert len(parts) >= 3
        assert all(len(p) <= DISCORD_MAX_TEXT for p in parts)
        assert all(token == "words" for p in parts for token in p.split())
        assert sum(len(p.split()) for p in parts) == 800

    def test_a_code_line_longer_than_a_part_stays_code(self):
        parts = split_message("```\n" + "z" * 4500 + "\n```")
        assert len(parts) == 3
        assert all(len(p) <= DISCORD_MAX_TEXT for p in parts)
        assert all(p.startswith("```\n") and p.endswith("\n```") for p in parts)
        assert "".join(p[4:-4] for p in parts) == "z" * 4500


class TestTextDelivery:
    @pytest.mark.asyncio
    async def test_deliver_text_sends_markdown_as_is(self):
        """Discord renders standard markdown — no escaping layer, unlike Telegram."""
        d = _delivery()
        mid = await d.deliver_text("500", "Hello. **bold**")
        assert d._api.sent[0]["content"] == "Hello. **bold**"
        assert mid == d._api.sent[0]["id"]

    @pytest.mark.asyncio
    async def test_deliver_text_splits_long_body(self):
        d = _delivery()
        await d.deliver_text("500", "x" * 5000)
        assert len(d._api.sent) == 3
        assert all(len(s["content"]) <= DISCORD_MAX_TEXT for s in d._api.sent)

    @pytest.mark.asyncio
    async def test_deliver_notification_titles_its_text(self):
        d = _delivery()
        await d.deliver_notification("500", "Heartbeat", "all good")
        assert d._api.sent[0]["content"] == "**Heartbeat**\n\nall good"

    @pytest.mark.asyncio
    async def test_deliver_cron_result_headers_the_first_part_only(self):
        d = _delivery()
        await d.deliver_cron_result("500", "nightly", "job-1", "x" * 3000)
        assert "**Cron: nightly**" in d._api.sent[0]["content"]
        assert "**Cron: nightly**" not in d._api.sent[1]["content"]
        assert all(len(s["content"]) <= DISCORD_MAX_TEXT for s in d._api.sent)

    @pytest.mark.asyncio
    async def test_deliver_subagent_reply_appends_timing_footer(self):
        d = _delivery()
        await d.deliver_subagent_reply("500", "done", elapsed_secs=3.25)
        assert d._api.sent[0]["content"] == "done"
        assert d._api.sent[-1]["content"] == "_took 3.2s_"

    @pytest.mark.asyncio
    async def test_deliver_subagent_reply_without_elapsed_has_no_footer(self):
        d = _delivery()
        await d.deliver_subagent_reply("500", "done")
        assert len(d._api.sent) == 1

    @pytest.mark.asyncio
    async def test_deliver_rich_attaches_components_when_discord_shaped(self):
        d = _delivery()
        rows = [{"type": COMPONENT_ACTION_ROW, "components": []}]
        await d.deliver_rich("500", {"components": rows}, "fallback")
        assert d._api.sent[0]["components"] == rows

    @pytest.mark.asyncio
    async def test_deliver_rich_falls_back_for_foreign_payload(self):
        """A Slack Block Kit payload isn't renderable here — use the fallback text."""
        d = _delivery()
        await d.deliver_rich("500", {"blocks": [{"type": "section"}]}, "plain fallback")
        assert d._api.sent[0]["content"] == "plain fallback"
        assert d._api.sent[0]["components"] is None

    @pytest.mark.asyncio
    async def test_deliver_chat_mirror_renders_options_as_buttons(self):
        d = _delivery()
        await d.deliver_chat_mirror("500", "Pick one\n[OPTIONS: Yes | No]")
        rows = d._api.sent[-1]["components"]
        buttons = rows[0]["components"]
        assert [b["label"] for b in buttons] == ["Yes", "No"]
        assert [b["custom_id"] for b in buttons] == ["opt:0", "opt:1"]
        assert all(b["type"] == COMPONENT_BUTTON for b in buttons)

    @pytest.mark.asyncio
    async def test_deliver_chat_mirror_chunks_options_five_per_row(self):
        """Discord allows at most 5 buttons per action row."""
        d = _delivery()
        opts = " | ".join(f"o{i}" for i in range(7))
        await d.deliver_chat_mirror("500", f"pick\n[OPTIONS: {opts}]")
        rows = d._api.sent[-1]["components"]
        assert [len(r["components"]) for r in rows] == [5, 2]

    @pytest.mark.asyncio
    async def test_deliver_chat_mirror_without_options_sends_no_buttons(self):
        d = _delivery()
        await d.deliver_chat_mirror("500", "just text")
        assert all(s["components"] is None for s in d._api.sent)


class TestOpenDM:
    @pytest.mark.asyncio
    async def test_opens_and_returns_the_channel_id(self):
        """A Discord user id is NOT a channel id — the DM must be opened."""
        d = _delivery()
        assert await d.open_dm("42") == "dm-42"
        assert d._api.dms == ["42"]

    @pytest.mark.asyncio
    async def test_result_is_cached(self):
        d = _delivery()
        await d.open_dm("42")
        await d.open_dm("42")
        assert d._api.dms == ["42"]  # only one REST call

    @pytest.mark.asyncio
    async def test_empty_user_id_returns_empty(self):
        d = _delivery()
        assert await d.open_dm("") == ""
        assert d._api.dms == []

    @pytest.mark.asyncio
    async def test_failure_degrades_to_empty(self):
        d = _delivery(fail={"create_dm"})
        assert await d.open_dm("42") == ""


class TestBuildThreadLink:
    def test_dm_shape_uses_at_me(self):
        """A DM has no guild, and Discord's own link form for that is literally @me."""
        d = _delivery()
        assert d.build_thread_link("dm-42", "99") == "https://discord.com/channels/@me/dm-42/99"

    def test_guild_shape_uses_the_guild_id(self):
        d = _delivery()
        d.note_channel_guild("700", "g1")
        assert d.build_thread_link("700", "99") == "https://discord.com/channels/g1/700/99"

    def test_without_message_id_links_the_channel(self):
        d = _delivery()
        d.note_channel_guild("700", "g1")
        assert d.build_thread_link("700", "") == "https://discord.com/channels/g1/700"

    def test_empty_channel_has_no_link(self):
        d = _delivery()
        assert d.build_thread_link("", "1") == ""

    def test_guild_map_is_per_instance(self):
        """Two transports must not share a channel→guild map."""
        a, b = _delivery(), _delivery()
        a.note_channel_guild("700", "g1")
        assert b.build_thread_link("700", "9") == "https://discord.com/channels/@me/700/9"


class TestIdentityResolution:
    @pytest.mark.asyncio
    async def test_resolve_user_name_prefers_global_name(self):
        d = _delivery()
        d._api.users["7"] = {"id": "7", "username": "ada_h", "global_name": "Ada"}
        assert await d.resolve_user_name("7") == "Ada"

    @pytest.mark.asyncio
    async def test_resolve_user_name_falls_back_to_username(self):
        d = _delivery()
        d._api.users["7"] = {"id": "7", "username": "ada_h"}
        assert await d.resolve_user_name("7") == "ada_h"

    @pytest.mark.asyncio
    async def test_resolve_user_name_degrades_to_the_id(self):
        d = _delivery(fail={"get_user"})
        assert await d.resolve_user_name("7") == "7"

    @pytest.mark.asyncio
    async def test_resolve_user_profile_degrades_to_the_id(self):
        d = _delivery(fail={"get_user"})
        assert await d.resolve_user_profile("7") == {"id": "7"}

    @pytest.mark.asyncio
    async def test_channel_info_reports_dm_for_type_1(self):
        d = _delivery()
        d._api.channels["dm-1"] = {"id": "dm-1", "type": 1}
        info = await d.channel_info("dm-1")
        assert info["is_im"] is True

    @pytest.mark.asyncio
    async def test_channel_info_reports_guild_channel(self):
        d = _delivery()
        d._api.channels["700"] = {"id": "700", "type": 0, "name": "general", "guild_id": "g1"}
        info = await d.channel_info("700")
        assert info == {"name": "general", "is_im": False, "guild_id": "g1"}

    @pytest.mark.asyncio
    async def test_channel_info_degrades(self):
        d = _delivery(fail={"get_channel"})
        assert await d.channel_info("700") == {"name": "700", "is_im": False}

    def test_list_reply_channels_is_minimal(self):
        assert _delivery().list_reply_channels() == [{"id": "dm", "name": "Direct Message"}]

    def test_is_tracked_channel_delegates_to_core_seam(self):
        # No tracked channels in the isolated home → False, no crash.
        assert _delivery().is_tracked_channel("700") is False


class TestReactionsAndTyping:
    """Both are declared True in capabilities(), so both must actually work."""

    @pytest.mark.asyncio
    async def test_add_reaction_hits_the_api(self):
        d = _delivery()
        assert await d.add_reaction("500", "9", "✅") is True
        assert d._api.reactions == [{"channel_id": "500", "message_id": "9", "emoji": "✅"}]

    @pytest.mark.asyncio
    async def test_add_reaction_failure_is_reported_not_raised(self):
        d = _delivery(fail={"add_reaction"})
        assert await d.add_reaction("500", "9", "✅") is False

    @pytest.mark.asyncio
    async def test_show_typing_hits_the_api(self):
        d = _delivery()
        assert await d.show_typing("500") is True
        assert d._api.typing == ["500"]

    @pytest.mark.asyncio
    async def test_show_typing_failure_is_reported_not_raised(self):
        d = _delivery(fail={"trigger_typing"})
        assert await d.show_typing("500") is False


class TestUploadAttachment:
    @pytest.mark.asyncio
    async def test_uploads_with_caption(self, tmp_path):
        f = tmp_path / "data.csv"
        f.write_text("a,b")
        d = _delivery()
        mid = await d.upload_attachment("500", str(f), initial_comment="look")
        assert d._api.uploads[0]["content"] == "look"
        assert mid == "1"



class TestStreamThrottle:
    @pytest.mark.asyncio
    async def test_throttles_to_one_edit_per_interval_then_flushes_exact_final(self):
        d = _delivery()
        clock = {"t": 100.0}
        d._now = lambda: clock["t"]

        sts = await d.start_stream("500", initial_text="…")  # send #1
        assert d._api.sent[0]["id"] == sts
        assert d._api.edits == []

        # First append well within the interval → throttled (no edit).
        clock["t"] = 100.5
        await d.append_stream_task("500", sts, "t1", "Step one", "in_progress")
        assert d._api.edits == []

        # Second append still inside the interval → still throttled.
        clock["t"] = 100.9
        await d.append_stream_task("500", sts, "t2", "Step two", "in_progress")
        assert d._api.edits == []

        # Past the interval → exactly one edit fires, carrying the latest pending text.
        clock["t"] = 100.0 + _EDIT_MIN_INTERVAL + 0.01
        await d.append_stream_task("500", sts, "t3", "Step three", "complete")
        assert len(d._api.edits) == 1
        assert "Step three" in d._api.edits[-1]["content"]

        # stop_stream force-flushes even inside the interval — the exact final text.
        clock["t"] += 0.001  # basically no time passed
        await d.append_stream_task("500", sts, "t4", "Final step", "complete")  # throttled away
        assert len(d._api.edits) == 1  # confirm it was throttled
        await d.stop_stream("500", sts)
        assert len(d._api.edits) == 2  # forced flush
        assert "Final step" in d._api.edits[-1]["content"]

    @pytest.mark.asyncio
    async def test_stream_edit_is_capped_at_the_message_limit(self):
        d = _delivery()
        clock = {"t": 100.0}
        d._now = lambda: clock["t"]
        sts = await d.start_stream("500", initial_text="x" * 1990)
        clock["t"] += 10
        await d.append_stream_task("500", sts, "t1", "y" * 100, "complete")
        assert len(d._api.edits[-1]["content"]) <= DISCORD_MAX_TEXT

    @pytest.mark.asyncio
    async def test_stop_unknown_stream_is_noop(self):
        d = _delivery()
        await d.stop_stream("500", "999")  # never started
        assert d._api.edits == []

    @pytest.mark.asyncio
    async def test_append_to_unknown_stream_is_noop(self):
        d = _delivery()
        await d.append_stream_task("500", "999", "t", "title", "complete")
        assert d._api.edits == []

    @pytest.mark.asyncio
    async def test_failed_edit_does_not_raise_out(self):
        d = _delivery(fail={"edit_message"})
        clock = {"t": 100.0}
        d._now = lambda: clock["t"]
        sts = await d.start_stream("500", initial_text="…")
        clock["t"] += 10
        await d.append_stream_task("500", sts, "t1", "step", "complete")
        await d.stop_stream("500", sts)  # no raise


class TestNoPlaceholderIsLeftBehind:
    """Ledger 281's family: a turn's stream opens with "Thinking…" and its reply is a message of
    its own, so the flush left the placeholder above the reply for good."""

    @pytest.mark.asyncio
    async def test_a_stream_that_only_held_its_placeholder_is_removed(self):
        d = _delivery()
        sts = await d.start_stream("500", initial_text="Thinking…")
        await d.stop_stream("500", sts)
        assert d._api.deleted == [{"channel_id": "500", "message_id": sts}]
        assert d._api.edits == []

    @pytest.mark.asyncio
    async def test_a_stream_with_tasks_keeps_only_their_lines(self):
        d = _delivery()
        clock = {"t": 0.0}
        d._now = lambda: clock["t"]
        sts = await d.start_stream("500", initial_text="Thinking…")
        clock["t"] = 5.0
        await d.append_stream_task("500", sts, "tool_1", "Read notes.md", "in_progress")
        assert d._api.edits[-1]["content"] == "Thinking…\n⏳ Read notes.md"
        await d.append_stream_task("500", sts, "tool_1", "Read notes.md", "complete")
        await d.stop_stream("500", sts)
        assert d._api.edits[-1]["content"] == "✅ Read notes.md"
        assert d._api.deleted == []

    @pytest.mark.asyncio
    async def test_a_placeholder_that_cannot_be_removed_says_so(self, caplog):
        import logging

        d = _delivery(fail={"delete_message"})
        sts = await d.start_stream("500", initial_text="Thinking…")
        with caplog.at_level(logging.WARNING, logger="discord_runtime.delivery"):
            await d.stop_stream("500", sts)
        assert f"the stream placeholder {sts} in 500 could not be removed" in caplog.text


class _Event:
    def __init__(self, request_id="req1", title="delete files", tool_input="", brief=None):
        self.request_id = request_id
        self.title = title
        self.tool_input = tool_input
        self.tool_purpose = ""
        self.tool_meta = {} if brief is None else {"approval_brief": brief}


#: What core stamps on an approval it asks a channel: the call, masked, as the dashboard's card
#: shows it (``personalclaw.sdk.channel.approval_brief_for``).
_BRIEF = {
    "tool": "execute_bash",
    "input": '{"command": "deploy --token [REDACTED: credential] --env staging"}',
    "purpose": "ship the staging build",
    "risk": "destructive",
    "summary": "Can: runs a command · Risk: Destructive",
}


class TestApprovalRoundTrip:
    @pytest.mark.asyncio
    async def test_full_round_trip(self):
        """post prompt → INTERACTION_CREATE → future resolves → acked → buttons gone."""
        d = _delivery(owner="42")

        task = asyncio.ensure_future(d.request_approval(_Event("reqX", "rm -rf"), source="tool"))
        await asyncio.sleep(0)  # let it open the DM
        await asyncio.sleep(0)  # …and post the prompt
        await asyncio.sleep(0)

        # It prompted the owner's DM (opened, not the raw user id) with two buttons.
        prompt = d._api.sent[-1]
        assert prompt["channel_id"] == "dm-42"
        buttons = prompt["components"][0]["components"]
        assert {b["custom_id"] for b in buttons} == {"approve:reqX", "deny:reqX"}
        assert [b["style"] for b in buttons] == [BUTTON_STYLE_SUCCESS, BUTTON_STYLE_DANGER]

        # The press arrives as an INTERACTION_CREATE and resolves the same future.
        await d.resolve_interaction({
            "id": "i1", "token": "itok", "type": INTERACTION_TYPE_COMPONENT,
            "data": {"custom_id": "approve:reqX"}, "user": {"id": "42"},
        })
        assert await asyncio.wait_for(task, timeout=1.0) is True

        # The interaction was acknowledged (Discord's 3-second window).
        assert d._api.acks[-1]["id"] == "i1"
        assert d._api.acks[-1]["token"] == "itok"

        # And the prompt was edited to the outcome WITH its buttons stripped — a
        # decided request must not leave a clickable Approve behind.
        final = d._api.edits[-1]
        assert "Approved" in final["content"]
        assert final["components"] == []

    @pytest.mark.asyncio
    async def test_the_prompt_shows_what_will_run_as_the_dashboard_card_does(self):
        """The prompt was "Approve: execute_bash?" and nothing else, so a command was approved
        without being seen. It shows the tool, its arguments, the purpose and what the call can
        touch, from core's brief, as they are: core masked them."""
        d = _delivery(owner="42")
        task = asyncio.ensure_future(
            d.request_approval(_Event("reqB", "execute_bash", brief=_BRIEF), source="subagent")
        )
        for _ in range(4):
            await asyncio.sleep(0)
        (prompt,) = d._api.sent
        text = (
            "🔐 [subagent] Approve `execute_bash`?\n"
            "```\n"
            '{"command": "deploy --token [REDACTED: credential] --env staging"}\n'
            "```\n"
            "ship the staging build\n"
            "Can: runs a command · Risk: Destructive"
        )
        assert prompt["content"] == text
        await d.resolve_interaction({
            "id": "i", "token": "t", "type": INTERACTION_TYPE_COMPONENT,
            "data": {"custom_id": "approve:reqB"}, "user": {"id": "42"},
        })
        assert await asyncio.wait_for(task, timeout=1.0) is True
        # Answered, the prompt keeps what was approved, with the outcome under it.
        assert d._api.edits[-1]["content"] == f"{text}\n✅ Approved"

    @pytest.mark.asyncio
    async def test_a_long_prompt_is_split_like_a_reply_with_the_buttons_last(self):
        """It was cut at 2,000 characters, so the owner approved a command whose end they
        never saw. Every line of the arguments arrives, and the buttons ride the last part."""
        d = _delivery(owner="42")
        command = "\n".join(f"echo step-{i}" for i in range(400))
        task = asyncio.ensure_future(
            d.request_approval(_Event("reqL", "execute_bash", tool_input=command), source="tool")
        )
        for _ in range(4):
            await asyncio.sleep(0)
        sent = d._api.sent
        assert len(sent) >= 2 and all(len(m["content"]) <= DISCORD_MAX_TEXT for m in sent)
        assert [m["components"] is not None for m in sent] == [False] * (len(sent) - 1) + [True]
        shown = "\n".join(m["content"] for m in sent)
        for i in range(400):
            assert f"echo step-{i}\n" in shown, f"step {i} was not shown"

        await d.resolve_interaction({
            "id": "i", "token": "t", "type": INTERACTION_TYPE_COMPONENT,
            "data": {"custom_id": "approve:reqL"}, "user": {"id": "42"},
        })
        assert await asyncio.wait_for(task, timeout=1.0) is True
        final = d._api.edits[-1]
        assert final["message_id"] == sent[-1]["id"] and final["components"] == []
        assert final["content"].endswith("✅ Approved") and len(final["content"]) <= DISCORD_MAX_TEXT

    @pytest.mark.asyncio
    async def test_deny_resolves_false_and_strips_buttons(self):
        d = _delivery(owner="42")
        task = asyncio.ensure_future(d.request_approval(_Event("reqY"), source="tool"))
        for _ in range(4):
            await asyncio.sleep(0)
        await d.resolve_interaction({
            "id": "i2", "token": "t2", "type": INTERACTION_TYPE_COMPONENT,
            "data": {"custom_id": "deny:reqY"}, "user": {"id": "42"},
        })
        assert await asyncio.wait_for(task, timeout=1.0) is False
        assert "Rejected" in d._api.edits[-1]["content"]
        assert d._api.edits[-1]["components"] == []

    @pytest.mark.asyncio
    async def test_timeout_defaults_to_rejected(self, monkeypatch):
        """Fail closed: an unanswered approval is a rejection, never an approval."""
        monkeypatch.setattr("discord_runtime.delivery._APPROVAL_TIMEOUT", 0.01)
        d = _delivery(owner="42")
        assert await d.request_approval(_Event("reqZ"), source="tool") is False
        assert "Rejected" in d._api.edits[-1]["content"]

    @pytest.mark.asyncio
    async def test_prompts_the_linked_channel_when_there_is_one(self):
        class Sessions:
            def get_channel(self, key):
                return "700"

        d = _delivery(owner="42")
        task = asyncio.ensure_future(
            d.request_approval(_Event("reqL"), source="tool",
                               parent_session_key="s1", sessions=Sessions())
        )
        for _ in range(4):
            await asyncio.sleep(0)
        assert d._api.sent[-1]["channel_id"] == "700"
        assert d._api.dms == []  # no DM needed
        await d.resolve_interaction({
            "id": "i", "token": "t", "type": INTERACTION_TYPE_COMPONENT,
            "data": {"custom_id": "deny:reqL"}, "member": {"user": {"id": "42"}},
        })
        await asyncio.wait_for(task, timeout=1.0)

    @pytest.mark.asyncio
    async def test_no_owner_no_channel_returns_none(self):
        """None lets the gateway fall back to the dashboard prompt."""
        d = _delivery(owner="")
        assert await d.request_approval(_Event(), source="tool") is None

    @pytest.mark.asyncio
    async def test_on_prompted_hook_receives_the_pending(self):
        seen = {}
        d = _delivery(owner="42")
        task = asyncio.ensure_future(
            d.request_approval(_Event("reqH"), source="tool",
                               on_prompted=lambda p: seen.setdefault("p", p))
        )
        for _ in range(4):
            await asyncio.sleep(0)
        assert seen["p"].request_id == "reqH"
        # A dashboard click resolves the very same future.
        seen["p"].future.set_result("approved")
        assert await asyncio.wait_for(task, timeout=1.0) is True

    @pytest.mark.asyncio
    async def test_raising_on_prompted_hook_does_not_break_the_prompt(self):
        d = _delivery(owner="42")

        def boom(pending):
            raise RuntimeError("hook blew up")

        task = asyncio.ensure_future(
            d.request_approval(_Event("reqB"), source="tool", on_prompted=boom)
        )
        for _ in range(4):
            await asyncio.sleep(0)
        await d.resolve_interaction({
            "id": "i", "token": "t", "type": INTERACTION_TYPE_COMPONENT,
            "data": {"custom_id": "approve:reqB"}, "user": {"id": "42"},
        })
        assert await asyncio.wait_for(task, timeout=1.0) is True


class TestOnlyTheOwnerAnswers:
    """A prompt for a chat linked to a tracked channel is posted in that channel, where every
    member sees the buttons. A member's press must not approve what the owner's agent runs."""

    @staticmethod
    async def _linked_prompt(d, request_id):
        class Sessions:
            def get_channel(self, key):
                return "700"

        task = asyncio.ensure_future(
            d.request_approval(_Event(request_id), source="tool", parent_session_key="s1", sessions=Sessions())
        )
        for _ in range(4):
            await asyncio.sleep(0)
        assert d._api.sent[-1]["channel_id"] == "700"
        return task

    @pytest.mark.asyncio
    async def test_a_members_press_is_refused_and_the_owners_answers(self):
        d = _delivery(owner="42")
        task = await self._linked_prompt(d, "reqM")
        for who in ({"member": {"user": {"id": "5151"}}}, {"user": {"id": "5151"}}, {}):
            await d.resolve_interaction({
                "id": "im", "token": "tm", "type": INTERACTION_TYPE_COMPONENT,
                "data": {"custom_id": "approve:reqM"}, **who,
            })
        await asyncio.sleep(0)
        assert not task.done(), "a member's press answered the owner's approval"
        assert len(d._api.acks) == 3, "each refused press is still acknowledged"

        # Floor: the owner's press, on the same prompt, does answer it.
        await d.resolve_interaction({
            "id": "io", "token": "to", "type": INTERACTION_TYPE_COMPONENT,
            "data": {"custom_id": "approve:reqM"}, "member": {"user": {"id": "42"}},
        })
        assert await asyncio.wait_for(task, timeout=1.0) is True

    @pytest.mark.asyncio
    async def test_a_refused_press_is_a_security_event(self, monkeypatch):
        import discord_runtime.delivery as mod

        events = []
        monkeypatch.setattr(mod, "sel", lambda: type("S", (), {"log_api_access": lambda self, **kw: events.append(kw)})())
        d = _delivery(owner="42")
        task = await self._linked_prompt(d, "reqS")
        await d.resolve_interaction({
            "id": "im", "token": "tm", "type": INTERACTION_TYPE_COMPONENT,
            "data": {"custom_id": "deny:reqS"}, "member": {"user": {"id": "5151"}},
        })
        assert [(e["caller"], e["outcome"], e["resources"]) for e in events] == [
            ("discord:5151", "denied", "reqS")
        ]
        await d.resolve_interaction({
            "id": "io", "token": "to", "type": INTERACTION_TYPE_COMPONENT,
            "data": {"custom_id": "deny:reqS"}, "member": {"user": {"id": "42"}},
        })
        assert await asyncio.wait_for(task, timeout=1.0) is False
        assert len(events) == 1, "the owner's press is not an event"


class TestResolveInteraction:
    @pytest.mark.asyncio
    async def test_unknown_custom_id_is_still_acknowledged(self):
        """Discord shows 'interaction failed' if nothing answers within 3s — ack
        regardless of whether the press was ours."""
        d = _delivery()
        await d.resolve_interaction({
            "id": "i9", "token": "t9", "type": INTERACTION_TYPE_COMPONENT,
            "data": {"custom_id": "approve:ghost"},
        })
        assert d._api.acks[-1]["id"] == "i9"

    @pytest.mark.asyncio
    async def test_non_component_interaction_is_ignored(self):
        """A slash command (type 2) is not ours and must not be answered here."""
        d = _delivery()
        await d.resolve_interaction({"id": "i1", "token": "t", "type": 2,
                                     "data": {"name": "ping"}})
        assert d._api.acks == []

    @pytest.mark.asyncio
    async def test_option_button_press_is_acked_but_resolves_nothing(self):
        d = _delivery()
        await d.resolve_interaction({
            "id": "i3", "token": "t3", "type": INTERACTION_TYPE_COMPONENT,
            "data": {"custom_id": "opt:1"},
        })
        assert d._api.acks[-1]["id"] == "i3"

    @pytest.mark.asyncio
    async def test_failed_ack_does_not_raise_out(self):
        d = _delivery(fail={"create_interaction_response"})
        await d.resolve_interaction({
            "id": "i4", "token": "t4", "type": INTERACTION_TYPE_COMPONENT,
            "data": {"custom_id": "approve:x"},
        })  # no raise

    @pytest.mark.asyncio
    async def test_second_press_on_a_resolved_request_is_harmless(self):
        d = _delivery(owner="42")
        task = asyncio.ensure_future(d.request_approval(_Event("reqD"), source="tool"))
        for _ in range(4):
            await asyncio.sleep(0)
        payload = {"id": "i", "token": "t", "type": INTERACTION_TYPE_COMPONENT,
                   "data": {"custom_id": "approve:reqD"}, "user": {"id": "42"}}
        await d.resolve_interaction(payload)
        assert await asyncio.wait_for(task, timeout=1.0) is True
        await d.resolve_interaction(payload)  # double press — no InvalidStateError


# ── core masks what it hands this channel ──────────────────────────────────────────────────

#: A key, assembled at runtime so the literal is not in the file, and its tail, which no
#: rendering escapes: the wire is searched for the tail, since a channel's markup may escape
#: the key's punctuation and hide the key from a search for all of it.
TAIL = "A" * 20 + "B" * 20 + "C" * 15
SECRET = "sk-" + "ant-api03-" + TAIL

#: Every way core hands this channel text, each carrying a key.
_HANDED = {
    "deliver_text": lambda h: h.deliver_text("500", f"token {SECRET}"),
    "deliver_notification": lambda h: h.deliver_notification(
        "500", f"Nightly {SECRET}", f"result {SECRET}"
    ),
    "deliver_rich": lambda h: h.deliver_rich(
        "500",
        {"components": [{"type": COMPONENT_ACTION_ROW, "components": [
            {"type": COMPONENT_BUTTON, "style": BUTTON_STYLE_SUCCESS,
             "label": f"Open {SECRET}", "custom_id": "open"},
        ]}]},
        f"fallback {SECRET}",
    ),
    "deliver_cron_result": lambda h: h.deliver_cron_result(
        "500", f"backup {SECRET}", "job-1", f"done {SECRET}"
    ),
    "deliver_chat_mirror": lambda h: h.deliver_chat_mirror("500", f"answer {SECRET}"),
    "deliver_subagent_reply": lambda h: h.deliver_subagent_reply(
        "500", f"reply {SECRET}", elapsed_secs=1.5
    ),
    "upload_attachment": lambda h: h.upload_attachment(
        "500", "report.txt", title=f"report {SECRET}"
    ),
    "start_stream": lambda h: h.start_stream("500", initial_text=f"thinking {SECRET}"),
    "request_approval": lambda h: h.request_approval(
        _Event("reqK", f"deploy {SECRET}"), source="tool"
    ),
}


@pytest.fixture
def through_core(monkeypatch):
    """This delivery as core holds it: registered with core, and read back the way core reads it."""
    from personalclaw import channel_delivery

    monkeypatch.setattr("discord_runtime.delivery._APPROVAL_TIMEOUT", 0.01)
    d = _delivery(owner="42")
    channel_delivery.register(d, provider="discord")
    yield d, channel_delivery.delivery_for("discord")
    channel_delivery.register(None, provider="discord")


class TestCoreMasksWhatItHandsThisChannel:
    """Core masks every text it hands a channel, so this app masks nothing again: a key in anything
    core sends through it never reaches the REST API."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", sorted(_HANDED))
    async def test_no_key_reaches_the_rest_api(self, through_core, method):
        d, handle = through_core
        await _HANDED[method](handle)
        wire = repr((d._api.sent, d._api.edits, d._api.uploads))
        assert TAIL not in wire
        assert "REDACTED" in wire, "nothing was sent"
