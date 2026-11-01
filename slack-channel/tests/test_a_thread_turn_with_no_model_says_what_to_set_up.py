"""A thread's turn with no model set up says so, and where to set one up.

With no model set up, a Slack thread was answered "🔧 Something went wrong. Please try again." and
the gateway logged a traceback. The thread's runtime couldn't be built, the catch-all took core's
refusal for a defect, and the footer then read a runtime the turn never got. The dashboard's chat
says no model is connected yet and where to set one up. The thread now says the same, and isn't
told to try again, which can't help.

Driven through the app's handler with the refusal core's resolver raises on an empty home.
"""

from __future__ import annotations

import pytest
from slack_helpers import MockSlackClient, set_owner
from test_slack_handler import FakeSessionManager

import slack_runtime.handler as H
from personalclaw.config.credentials import owner_id_credential
from personalclaw.config.loader import CRED_OWNER_ID
from personalclaw.errors import AgentError
from personalclaw.providers.provider_bridge import (
    ProviderResolutionError as ResolverRefusal,
    resolve_provider_for_use_case,
)
from personalclaw.sdk.model import ProviderResolutionError

OWNER = "U0MIRAOWNR"
CHANNEL = "C0EXAMPLE02"
THREAD = "1712793600.000500"


@pytest.fixture(autouse=True)
def _an_owner(monkeypatch):
    monkeypatch.delenv(CRED_OWNER_ID, raising=False)
    monkeypatch.setenv(owner_id_credential("slack"), OWNER)
    set_owner(OWNER)
    H.set_allowed_users({OWNER})
    yield
    set_owner("")
    H.set_allowed_users(set())


class _NoRuntime(FakeSessionManager):
    """Sessions whose runtime can't be built, for the reason given."""

    def __init__(self, refusal: BaseException) -> None:
        super().__init__()
        self._refusal = refusal
        self.failures: list[str] = []

    async def get_or_create(self, key, agent=None, channel_id=None):
        raise self._refusal

    async def record_failure(self, key):
        self.failures.append(key)


def _the_refusal_an_empty_home_raises() -> BaseException:
    with pytest.raises(ResolverRefusal) as caught:
        resolve_provider_for_use_case("chat")
    assert caught.value.agent_error.code == "ERR_MODEL_UNRESOLVED"
    return caught.value


async def _what_the_thread_is_told(refusal: BaseException) -> tuple[str, MockSlackClient, list]:
    slack, sessions = MockSlackClient(), _NoRuntime(refusal)
    await H.handle_message(slack, sessions, CHANNEL, "hello", THREAD, "m1", OWNER)
    said = "\n".join(
        str(detail.get("text") or "") for kind, detail in slack.actions if kind in ("post", "update")
    )
    return said, slack, sessions.failures


@pytest.mark.asyncio
async def test_a_turn_with_no_model_set_up_says_what_to_set_up():
    """🔴 Red before: "Something went wrong. Please try again.", and the footer raised on the
    runtime the turn never got."""
    said, slack, failures = await _what_the_thread_is_told(_the_refusal_an_empty_home_raises())

    assert H.NO_MODEL_CONNECTED in said, said
    assert "Something went wrong" not in said
    assert "try again" not in said.lower()
    assert failures == [], "no runtime failed: there was none to build"
    assert any(kind == "blocks" for kind, _ in slack.actions), "the turn ends with its footer"


@pytest.mark.asyncio
async def test_a_provider_with_no_model_chosen_is_named():
    """A provider is connected, but no model is chosen for it: the dashboard names it, and so
    does the thread."""
    refusal = ResolverRefusal(
        "No provider configured for use case 'chat'.",
        AgentError(
            code="ERR_MODEL_UNRESOLVED",
            what="no model provider resolves for use case 'chat'",
            why="no model is chosen for “Ollama”",
            fix="choose one of its models in Settings → Models",
        ),
    )
    said, _, _ = await _what_the_thread_is_told(refusal)

    assert (
        "No model is chosen for Ollama yet. Choose which of its models to chat with in "
        "Settings → Models."
    ) in said, said


@pytest.mark.asyncio
async def test_the_provider_is_named_from_the_refusals_why_not_its_words():
    """The refusal's ``why`` is the contract; how its message is worded is not."""

    class _Coded(Exception):
        def __init__(self) -> None:
            super().__init__("No provider configured for use case 'chat'.")
            self.agent_error = AgentError(
                code="ERR_MODEL_UNRESOLVED",
                what="no model provider resolves for use case 'chat'",
                why="no model is chosen for “Ollama”",
                fix="choose one of its models in Settings → Models",
            )

    said, _, _ = await _what_the_thread_is_told(_Coded())

    assert "No model is chosen for Ollama yet." in said, said


@pytest.mark.asyncio
async def test_the_registrys_own_refusal_is_said_as_written():
    """The registry's refusal is written for the person reading it."""
    words = "No model is chosen for this call. Choose one in Settings → Models."
    said, _, failures = await _what_the_thread_is_told(ProviderResolutionError(words))

    assert words in said, said
    assert "Something went wrong" not in said
    assert failures == []


@pytest.mark.asyncio
async def test_any_other_failure_still_says_something_went_wrong():
    said, _, failures = await _what_the_thread_is_told(RuntimeError("a defect"))

    assert "Something went wrong" in said
    assert failures == [THREAD]
