"""Every app declares the core features it relies on, by names PersonalClaw offers.

An approval prompt's answers come from PersonalClaw (``approval_brief_for(event)["answers"]``), so
an app whose prompt offers them works only on a PersonalClaw that hands them over. Telegram
Channel's update that offered them installed on a PersonalClaw built before them, every approval on
the channel then arrived as a notice with nothing to press, and nothing had checked the update
fitted that PersonalClaw. An app names the core features it relies on in ``requiresCoreFeatures``,
and PersonalClaw refuses to review, install, update or switch on one it cannot host.

The rails over every bundle, against the installed core:

1. every name an app declares is a core feature the installed PersonalClaw offers: a misspelt name,
   or one from a PersonalClaw that does not exist yet, would make the app installable nowhere;
2. every app whose shipped code reads the approval brief's answers declares ``approval-answers``;
3. every app whose shipped code downloads with ``personalclaw.sdk.net.open_url`` declares
   ``guarded-download``: on a PersonalClaw without it the app would not load at all;
4. every app whose shipped code gives or reads a chat's Trust for a conversation it runs itself
   (``answer_in_chat``, ``chat_grant`` from ``personalclaw.sdk.channel``) declares ``chat-trust``:
   on a PersonalClaw without it the app would not load at all;
5. every app whose shipped code links a chat on its channel or asks which channel a chat is on
   (``link_channel(…, provider=…)``, ``set_channel_link(…, channel_provider=…)``,
   ``get_channel_provider``) declares ``links-name-their-channel``: on a PersonalClaw without it
   the link is refused, and a chat resumed or imported there is linked nowhere;
6. every app whose shipped code saves a conversation's turns or takes lines into a chat with where
   they came from (``save_conversation_turn``, ``arrived_on`` from ``personalclaw.sdk.channel``)
   declares ``turns-name-their-channel``, and every turn it saves names its sender and its channel
   (``source_user=``, ``source_channel=``): memory takes a line as the owner's own words only when
   its sender is the owner its channel keeps, so a turn saved without them is nobody's, and on a
   PersonalClaw without the feature the save is refused.
7. every app whose shipped code offers a message to core's answer to the Morning triage digest
   (``services.answer_channel_reply``) declares ``digest-replies``: on a PersonalClaw without it the
   services handle has no such method, so every direct message the app offers would fail;
8. every app whose shipped code asks PersonalClaw's deny-list about a call before it approves or
   asks about it (``screen_tool_call`` from ``personalclaw.sdk.channel``) declares
   ``tool-call-screen``: on a PersonalClaw without it the app would not load at all;
9. every app whose shipped code reads a model's stream inside ``closing_stream`` (imported from
   ``personalclaw.sdk.model``) declares ``closing-streams``: on a PersonalClaw without it the app
   would not load at all;
10. every app whose shipped code saves a conversation's turns runs them itself, so it names whose
    message each turn answers (``turn_asked_by`` from ``personalclaw.sdk.channel``) and declares
    ``turns-name-who-asked``: what the turn's tools would change of the owner's memory waits for
    her own word unless she sent it, and on a PersonalClaw without the feature the app does not
    load.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # for apps_testkit

from apps_testkit import sdk_contract  # noqa: E402

BUNDLES = sorted(manifest.parent for manifest in ROOT.glob("*/app.json"))

#: The apps whose prompts offer the brief's answers as this rail lands: a reader the scan stopped
#: recognising would leave rail 2 checking nothing.
KNOWN_READERS = {"telegram-channel", "slack-channel", "discord-channel", "email-channel"}

#: The apps that download with ``open_url``, for rail 3 the same way.
KNOWN_DOWNLOADERS = {"diarization-onnx"}

#: The apps that keep a conversation's Trust in PersonalClaw's chat for it, for rail 4.
KNOWN_CHAT_TRUSTERS = {"slack-channel"}

#: What a channel imports to give and read the chat's Trust (rail 4).
_CHAT_TRUST_NAMES = frozenset({"answer_in_chat", "chat_grant"})

#: The apps that link a chat on their own channel, for rail 5.
KNOWN_CHANNEL_LINKERS = {"slack-channel"}

#: The apps that save a conversation's turns or take lines into a chat themselves, for rail 6.
KNOWN_TURN_WRITERS = {"slack-channel"}

#: What a channel calls to save its turns or record where a line came from (rail 6).
_TURN_SOURCE_NAMES = frozenset({"save_conversation_turn", "arrived_on"})

#: The apps that offer their direct messages to core's answer to the Morning triage digest, for
#: rail 7.
KNOWN_DIGEST_ANSWERERS = {"slack-channel"}

#: The apps that ask PersonalClaw's deny-list about a call before they approve or ask, for rail 8.
KNOWN_SCREENERS = {"slack-channel"}

#: The apps that read a model's stream inside ``closing_stream``, for rail 9.
KNOWN_STREAM_CLOSERS = {"code-review", "issue-radar", "slack-channel"}


@pytest.fixture(autouse=True)
def _scratch_home(tmp_path, monkeypatch):
    """The SDK resolves core's home when it loads. Not the real one."""
    monkeypatch.setenv("PERSONALCLAW_HOME", str(tmp_path / "home"))


def _declared(bundle: Path) -> list:
    manifest = json.loads((bundle / "app.json").read_text(encoding="utf-8"))
    declared = manifest.get("requiresCoreFeatures", [])
    return declared if isinstance(declared, list) else [declared]


def _reads_the_answers(bundle: Path) -> bool:
    """Whether the bundle's shipped code takes the approval brief from the SDK and reads its
    ``answers``: ``brief.get("answers")`` or ``brief["answers"]``."""
    imports = reads = False
    for path in sdk_contract.shipped_sources(bundle):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "personalclaw.sdk.channel":
                imports = imports or any(a.name == "approval_brief_for" for a in node.names)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                reads = reads or (
                    node.func.attr == "get"
                    and bool(node.args)
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == "answers"
                )
            elif isinstance(node, ast.Subscript):
                key = node.slice
                reads = reads or (isinstance(key, ast.Constant) and key.value == "answers")
    return imports and reads


def test_every_declared_feature_is_one_the_installed_core_offers():
    from personalclaw.sdk.features import CORE_FEATURES

    unknown = {
        bundle.name: [f for f in _declared(bundle) if f not in CORE_FEATURES]
        for bundle in BUNDLES
    }
    unknown = {name: names for name, names in unknown.items() if names}
    assert unknown == {}, (
        f"these apps declare core features the installed PersonalClaw does not offer: {unknown} "
        f"(it offers {sorted(CORE_FEATURES)})"
    )


def test_every_app_that_offers_the_briefs_answers_declares_them():
    from personalclaw.sdk.features import APPROVAL_ANSWERS

    readers = {bundle.name for bundle in BUNDLES if _reads_the_answers(bundle)}
    assert KNOWN_READERS <= readers, (
        f"the scan no longer sees these apps read the brief's answers: "
        f"{sorted(KNOWN_READERS - readers)}"
    )
    undeclared = sorted(
        name for name in readers
        if APPROVAL_ANSWERS not in _declared(ROOT / name)
    )
    assert undeclared == [], (
        f"these apps offer the approval brief's answers without declaring "
        f"'requiresCoreFeatures': ['{APPROVAL_ANSWERS}'] in app.json: {undeclared}"
    )


def test_the_scan_tells_a_reader_from_an_app_that_reads_nothing(tmp_path):
    """Positive and negative control, in the shape the rail reads."""
    reader = tmp_path / "reader-app"
    reader.mkdir()
    (reader / "app.json").write_text('{"name": "reader-app", "version": "0.1.0"}')
    (reader / "delivery.py").write_text(
        "from personalclaw.sdk.channel import approval_brief_for\n\n\n"
        "def answers(event):\n"
        "    return (approval_brief_for(event) or {}).get('answers') or []\n",
        encoding="utf-8",
    )
    other = tmp_path / "other-app"
    other.mkdir()
    (other / "app.json").write_text('{"name": "other-app", "version": "0.1.0"}')
    (other / "provider.py").write_text("def answers(d):\n    return d.get('answers')\n")
    assert _reads_the_answers(reader) is True
    assert _reads_the_answers(other) is False


def _downloads_through_the_guard(bundle: Path) -> bool:
    """Whether the bundle's shipped code takes ``open_url`` from the SDK."""
    for path in sdk_contract.shipped_sources(bundle):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "personalclaw.sdk.net":
                if any(alias.name == "open_url" for alias in node.names):
                    return True
    return False


def test_every_app_that_downloads_through_the_guard_declares_it():
    from personalclaw.sdk.features import GUARDED_DOWNLOAD

    downloaders = {bundle.name for bundle in BUNDLES if _downloads_through_the_guard(bundle)}
    assert KNOWN_DOWNLOADERS <= downloaders, (
        f"the scan no longer sees these apps download with open_url: "
        f"{sorted(KNOWN_DOWNLOADERS - downloaders)}"
    )
    undeclared = sorted(
        name for name in downloaders if GUARDED_DOWNLOAD not in _declared(ROOT / name)
    )
    assert undeclared == [], (
        f"these apps download with personalclaw.sdk.net.open_url without declaring "
        f"'requiresCoreFeatures': ['{GUARDED_DOWNLOAD}'] in app.json: {undeclared}"
    )


def test_the_scan_tells_a_downloader_from_an_app_that_only_fetches(tmp_path):
    """Positive and negative control for rail 3."""
    downloader = tmp_path / "downloader-app"
    downloader.mkdir()
    (downloader / "app.json").write_text('{"name": "downloader-app", "version": "0.1.0"}')
    (downloader / "provider.py").write_text(
        "from personalclaw.sdk.net import EgressBlocked, open_url\n", encoding="utf-8"
    )
    other = tmp_path / "other-app"
    other.mkdir()
    (other / "app.json").write_text('{"name": "other-app", "version": "0.1.0"}')
    (other / "provider.py").write_text("from personalclaw.sdk.net import fetch\n")
    assert _downloads_through_the_guard(downloader) is True
    assert _downloads_through_the_guard(other) is False


def _keeps_the_chats_trust(bundle: Path) -> bool:
    """Whether the bundle's shipped code takes ``answer_in_chat`` or ``chat_grant`` from the SDK."""
    for path in sdk_contract.shipped_sources(bundle):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "personalclaw.sdk.channel":
                if any(alias.name in _CHAT_TRUST_NAMES for alias in node.names):
                    return True
    return False


def test_every_app_that_keeps_a_conversations_trust_in_its_chat_declares_it():
    from personalclaw.sdk.features import CHAT_TRUST

    trusters = {bundle.name for bundle in BUNDLES if _keeps_the_chats_trust(bundle)}
    assert KNOWN_CHAT_TRUSTERS <= trusters, (
        f"the scan no longer sees these apps give or read a chat's Trust: "
        f"{sorted(KNOWN_CHAT_TRUSTERS - trusters)}"
    )
    undeclared = sorted(name for name in trusters if CHAT_TRUST not in _declared(ROOT / name))
    assert undeclared == [], (
        f"these apps give or read a chat's Trust without declaring "
        f"'requiresCoreFeatures': ['{CHAT_TRUST}'] in app.json: {undeclared}"
    )


def test_the_scan_tells_an_app_that_keeps_the_chats_trust_from_one_that_does_not(tmp_path):
    """Positive and negative control for rail 4."""
    truster = tmp_path / "truster-app"
    truster.mkdir()
    (truster / "app.json").write_text('{"name": "truster-app", "version": "0.1.0"}')
    (truster / "handler.py").write_text(
        "from personalclaw.sdk.channel import approval_brief_for, chat_grant\n", encoding="utf-8"
    )
    other = tmp_path / "other-app"
    other.mkdir()
    (other / "app.json").write_text('{"name": "other-app", "version": "0.1.0"}')
    (other / "handler.py").write_text("from personalclaw.sdk.channel import approval_brief_for\n")
    assert _keeps_the_chats_trust(truster) is True
    assert _keeps_the_chats_trust(other) is False


def _links_on_its_channel(bundle: Path) -> bool:
    """Whether the bundle's shipped code links a chat naming the channel, or asks a link's channel:
    a ``link_channel`` call given ``provider``, a ``set_channel_link`` call given
    ``channel_provider``, or a ``get_channel_provider`` call."""
    for path in sdk_contract.shipped_sources(bundle):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            named = {kw.arg for kw in node.keywords}
            if (
                node.func.attr == "get_channel_provider"
                or (node.func.attr == "link_channel" and "provider" in named)
                or (node.func.attr == "set_channel_link" and "channel_provider" in named)
            ):
                return True
    return False


def test_every_app_that_links_a_chat_on_its_channel_declares_it():
    from personalclaw.sdk.features import LINKS_NAME_THEIR_CHANNEL

    linkers = {bundle.name for bundle in BUNDLES if _links_on_its_channel(bundle)}
    assert KNOWN_CHANNEL_LINKERS <= linkers, (
        f"the scan no longer sees these apps link a chat on their channel: "
        f"{sorted(KNOWN_CHANNEL_LINKERS - linkers)}"
    )
    undeclared = sorted(
        name for name in linkers if LINKS_NAME_THEIR_CHANNEL not in _declared(ROOT / name)
    )
    assert undeclared == [], (
        f"these apps link a chat on their channel without declaring "
        f"'requiresCoreFeatures': ['{LINKS_NAME_THEIR_CHANNEL}'] in app.json: {undeclared}"
    )


def test_the_scan_tells_an_app_that_links_on_its_channel_from_one_that_does_not(tmp_path):
    """Positive and negative control for rail 5."""
    linker = tmp_path / "linker-app"
    linker.mkdir()
    (linker / "app.json").write_text('{"name": "linker-app", "version": "0.1.0"}')
    (linker / "interactions.py").write_text(
        "def resume(ds, chat, ts, channel):\n"
        "    ds.link_channel(chat, ts, channel, provider='linker')\n",
        encoding="utf-8",
    )
    other = tmp_path / "other-app"
    other.mkdir()
    (other / "app.json").write_text('{"name": "other-app", "version": "0.1.0"}')
    (other / "handler.py").write_text(
        "def own(sessions, key, channel):\n    sessions.set_channel_link(key, key, channel)\n",
        encoding="utf-8",
    )
    assert _links_on_its_channel(linker) is True
    assert _links_on_its_channel(other) is False


def _calls(bundle: Path, name: str) -> list[tuple[str, ast.Call]]:
    """Every call of *name* in the bundle's shipped code, by the file it is in."""
    found = []
    for path in sdk_contract.shipped_sources(bundle):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if called == name:
                    found.append((f"{path.relative_to(bundle)}:{node.lineno}", node))
    return found


def _writes_turns(bundle: Path) -> bool:
    """Whether the bundle's shipped code takes ``save_conversation_turn`` or ``arrived_on`` from
    the SDK."""
    for path in sdk_contract.shipped_sources(bundle):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "personalclaw.sdk.channel":
                if any(alias.name in _TURN_SOURCE_NAMES for alias in node.names):
                    return True
    return False


def test_every_app_that_saves_its_turns_declares_that_they_name_their_channel():
    from personalclaw.sdk.features import TURNS_NAME_THEIR_CHANNEL

    writers = {bundle.name for bundle in BUNDLES if _writes_turns(bundle)}
    assert KNOWN_TURN_WRITERS <= writers, (
        f"the scan no longer sees these apps save their turns: "
        f"{sorted(KNOWN_TURN_WRITERS - writers)}"
    )
    undeclared = sorted(
        name for name in writers if TURNS_NAME_THEIR_CHANNEL not in _declared(ROOT / name)
    )
    assert undeclared == [], (
        f"these apps save a conversation's turns without declaring "
        f"'requiresCoreFeatures': ['{TURNS_NAME_THEIR_CHANNEL}'] in app.json: {undeclared}"
    )


def test_every_turn_an_app_saves_names_its_sender_and_its_channel():
    nameless = [
        f"{bundle.name}/{where}"
        for bundle in BUNDLES
        for where, call in _calls(bundle, "save_conversation_turn")
        if not {"source_user", "source_channel"} <= {kw.arg for kw in call.keywords}
    ]
    assert nameless == [], (
        f"these turns are saved without their sender and channel, so memory reads them as "
        f"nobody's: {nameless}"
    )
    assert _calls(ROOT / "slack-channel", "save_conversation_turn"), "the scan sees no turn saved"


def test_the_scan_tells_a_turn_that_names_its_channel_from_one_that_does_not(tmp_path):
    """Positive and negative control for rail 6."""
    writer = tmp_path / "writer-app"
    writer.mkdir()
    (writer / "app.json").write_text('{"name": "writer-app", "version": "0.1.0"}')
    (writer / "handler.py").write_text(
        "from personalclaw.sdk.channel import save_conversation_turn\n\n\n"
        "def keep(log, key, text, reply, user):\n"
        "    save_conversation_turn(log, key, text, reply, source_thread=key, source_user=user,\n"
        "                           source_channel='writer')\n"
        "    save_conversation_turn(log, key, text, reply, source_thread=key)\n",
        encoding="utf-8",
    )
    other = tmp_path / "other-app"
    other.mkdir()
    (other / "app.json").write_text('{"name": "other-app", "version": "0.1.0"}')
    (other / "handler.py").write_text("from personalclaw.sdk.channel import redact\n")
    assert _writes_turns(writer) is True
    assert _writes_turns(other) is False
    named = [
        {"source_user", "source_channel"} <= {kw.arg for kw in call.keywords}
        for _where, call in _calls(writer, "save_conversation_turn")
    ]
    assert named == [True, False]


def _offers_digest_replies(bundle: Path) -> bool:
    """Whether the bundle's shipped code offers a message to ``answer_channel_reply``."""
    return bool(_calls(bundle, "answer_channel_reply"))


def test_every_app_that_offers_its_messages_to_the_digests_answer_declares_it():
    from personalclaw.sdk.features import DIGEST_REPLIES

    answerers = {bundle.name for bundle in BUNDLES if _offers_digest_replies(bundle)}
    assert KNOWN_DIGEST_ANSWERERS <= answerers, (
        f"the scan no longer sees these apps offer a message to answer_channel_reply: "
        f"{sorted(KNOWN_DIGEST_ANSWERERS - answerers)}"
    )
    undeclared = sorted(
        name for name in answerers if DIGEST_REPLIES not in _declared(ROOT / name)
    )
    assert undeclared == [], (
        f"these apps offer a message to answer_channel_reply without declaring "
        f"'requiresCoreFeatures': ['{DIGEST_REPLIES}'] in app.json: {undeclared}"
    )


def test_the_scan_tells_an_app_that_offers_the_digests_answer_from_one_that_does_not(tmp_path):
    """Positive and negative control for rail 7."""
    answerer = tmp_path / "answerer-app"
    answerer.mkdir()
    (answerer / "app.json").write_text('{"name": "answerer-app", "version": "0.1.0"}')
    (answerer / "handler.py").write_text(
        "async def on_dm(services, msg):\n"
        "    return await services.answer_channel_reply('answerer', msg, is_dm=True)\n",
        encoding="utf-8",
    )
    other = tmp_path / "other-app"
    other.mkdir()
    (other / "app.json").write_text('{"name": "other-app", "version": "0.1.0"}')
    (other / "handler.py").write_text(
        "async def on_dm(services, msg):\n"
        "    return await services.deliver_channel_inbound('other', msg)\n",
        encoding="utf-8",
    )
    assert _offers_digest_replies(answerer) is True
    assert _offers_digest_replies(other) is False


def _screens_each_call(bundle: Path) -> bool:
    """Whether the bundle's shipped code takes ``screen_tool_call`` from the SDK."""
    for path in sdk_contract.shipped_sources(bundle):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "personalclaw.sdk.channel":
                if any(alias.name == "screen_tool_call" for alias in node.names):
                    return True
    return False


def test_every_app_that_screens_a_call_with_the_deny_list_declares_it():
    from personalclaw.sdk.features import TOOL_CALL_SCREEN

    screeners = {bundle.name for bundle in BUNDLES if _screens_each_call(bundle)}
    assert KNOWN_SCREENERS <= screeners, (
        f"the scan no longer sees these apps screen a call with the deny-list: "
        f"{sorted(KNOWN_SCREENERS - screeners)}"
    )
    undeclared = sorted(
        name for name in screeners if TOOL_CALL_SCREEN not in _declared(ROOT / name)
    )
    assert undeclared == [], (
        f"these apps screen a call with personalclaw.sdk.channel.screen_tool_call without "
        f"declaring 'requiresCoreFeatures': ['{TOOL_CALL_SCREEN}'] in app.json: {undeclared}"
    )


def test_the_scan_tells_an_app_that_screens_a_call_from_one_that_does_not(tmp_path):
    """Positive and negative control for rail 8."""
    screener = tmp_path / "screener-app"
    screener.mkdir()
    (screener / "app.json").write_text('{"name": "screener-app", "version": "0.1.0"}')
    (screener / "handler.py").write_text(
        "from personalclaw.sdk.channel import chat_grant, screen_tool_call\n", encoding="utf-8"
    )
    other = tmp_path / "other-app"
    other.mkdir()
    (other / "app.json").write_text('{"name": "other-app", "version": "0.1.0"}')
    (other / "handler.py").write_text("from personalclaw.sdk.channel import chat_grant\n")
    assert _screens_each_call(screener) is True
    assert _screens_each_call(other) is False


def _closes_its_streams(bundle: Path) -> bool:
    """Whether the bundle's shipped code takes ``closing_stream`` from the SDK."""
    for path in sdk_contract.shipped_sources(bundle):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "personalclaw.sdk.model":
                if any(alias.name == "closing_stream" for alias in node.names):
                    return True
    return False


def test_every_app_that_reads_a_stream_inside_closing_stream_declares_it():
    from personalclaw.sdk.features import CLOSING_STREAMS

    closers = {bundle.name for bundle in BUNDLES if _closes_its_streams(bundle)}
    assert KNOWN_STREAM_CLOSERS <= closers, (
        f"the scan no longer sees these apps read a stream inside closing_stream: "
        f"{sorted(KNOWN_STREAM_CLOSERS - closers)}"
    )
    undeclared = sorted(name for name in closers if CLOSING_STREAMS not in _declared(ROOT / name))
    assert undeclared == [], (
        f"these apps read a model's stream inside closing_stream without declaring "
        f"'requiresCoreFeatures': ['{CLOSING_STREAMS}'] in app.json: {undeclared}"
    )


def test_the_scan_tells_an_app_that_closes_its_streams_from_one_that_does_not(tmp_path):
    """Positive and negative control for rail 9."""
    closer = tmp_path / "closer-app"
    closer.mkdir()
    (closer / "app.json").write_text('{"name": "closer-app", "version": "0.1.0"}')
    (closer / "provider.py").write_text(
        "async def review(provider, prompt):\n"
        "    from personalclaw.sdk.model import EVENT_TEXT_CHUNK, closing_stream\n\n"
        "    async with closing_stream(provider.stream(prompt)) as events:\n"
        "        return [e.text async for e in events if e.kind == EVENT_TEXT_CHUNK]\n",
        encoding="utf-8",
    )
    other = tmp_path / "other-app"
    other.mkdir()
    (other / "app.json").write_text('{"name": "other-app", "version": "0.1.0"}')
    (other / "provider.py").write_text("from personalclaw.sdk.model import EVENT_TEXT_CHUNK\n")
    assert _closes_its_streams(closer) is True
    assert _closes_its_streams(other) is False


def _names_who_asked(bundle: Path) -> bool:
    """Whether the bundle's shipped code takes ``turn_asked_by`` from the SDK."""
    for path in sdk_contract.shipped_sources(bundle):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "personalclaw.sdk.channel":
                if any(alias.name == "turn_asked_by" for alias in node.names):
                    return True
    return False


def test_every_app_that_runs_its_turns_names_who_asked_for_each_and_declares_it():
    from personalclaw.sdk.features import TURNS_NAME_WHO_ASKED

    runners = {b.name for b in BUNDLES if _calls(b, "save_conversation_turn")}
    assert KNOWN_TURN_WRITERS <= runners, (
        f"the scan no longer sees these apps run their turns: {sorted(KNOWN_TURN_WRITERS - runners)}"
    )
    unnamed = sorted(name for name in runners if not _names_who_asked(ROOT / name))
    assert unnamed == [], (
        f"these apps run a conversation's turns without saying whose message each answers "
        f"(turn_asked_by), so the owner's memory takes what a colleague's turn asks for: {unnamed}"
    )
    undeclared = sorted(
        name for name in runners if TURNS_NAME_WHO_ASKED not in _declared(ROOT / name)
    )
    assert undeclared == [], (
        f"these apps name who asked for their turns without declaring "
        f"'requiresCoreFeatures': ['{TURNS_NAME_WHO_ASKED}'] in app.json: {undeclared}"
    )


def test_the_scan_tells_an_app_that_names_who_asked_from_one_that_does_not(tmp_path):
    """Positive and negative control for rail 10."""
    runner = tmp_path / "runner-app"
    runner.mkdir()
    (runner / "app.json").write_text('{"name": "runner-app", "version": "0.1.0"}')
    (runner / "handler.py").write_text(
        "from personalclaw.sdk.channel import arrived_on, turn_asked_by\n", encoding="utf-8"
    )
    other = tmp_path / "other-app"
    other.mkdir()
    (other / "app.json").write_text('{"name": "other-app", "version": "0.1.0"}')
    (other / "handler.py").write_text("from personalclaw.sdk.channel import arrived_on\n")
    assert _names_who_asked(runner) is True
    assert _names_who_asked(other) is False
