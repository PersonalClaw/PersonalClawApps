"""Bedrock names the real fix for a refused call, and sees credentials that come back.

Measured before this was written:

* An ``AccessDeniedException`` passed through ``_friendly_bedrock_error`` unchanged. The chat
  showed the raw botocore dump, or core's "rejected the API key" line whenever the text said
  "permission" or the account id held the digits 401. The credentials are fine in both cases:
  the identity's IAM policy lacks the action, or the account has no access to the model.
* ``_creds_ok`` cached its answer for the life of the process. A profile whose credentials
  were missing once (an SSO session that had lapsed) kept embeddings, images, video and speech
  unavailable after ``aws sso login`` until the gateway restarted.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from personalclaw.sdk.model import ProviderResolutionError

MODEL = "amazon.nova-pro-v1:0"


class _ClientError(Exception):
    """The shape of botocore's ``ClientError``: the message, and the parsed ``response``."""

    def __init__(self, code: str, message: str, operation: str = "ConverseStream") -> None:
        self.response = {"Error": {"Code": code, "Message": message}}
        super().__init__(
            f"An error occurred ({code}) when calling the {operation} operation: {message}"
        )


def _mapped(error: Exception, **kw) -> str:
    from provider import _friendly_bedrock_error

    out = _friendly_bedrock_error(error, MODEL, **kw)
    assert isinstance(out, ProviderResolutionError), f"passed through unchanged: {out!r}"
    return str(out)


def test_a_missing_iam_action_names_the_action_not_the_key():
    """🔴 Red on main: the raw dump came through."""
    sentence = _mapped(
        _ClientError(
            "AccessDeniedException",
            "User: arn:aws:sts::340140123401:assumed-role/Dev/alice is not authorized to perform: "
            "bedrock:InvokeModelWithResponseStream on resource: "
            f"arn:aws:bedrock:us-west-2::foundation-model/{MODEL} because no identity-based "
            "policy allows the bedrock:InvokeModelWithResponseStream action",
        )
    )
    assert sentence == (
        "Your AWS credentials aren't allowed to call bedrock:InvokeModelWithResponseStream on the "
        f"Bedrock model '{MODEL}'. Add that action to the IAM policy of the identity Bedrock "
        "signs in as, or pick a different model in Settings → Models."
    )


def test_a_model_the_account_has_no_access_to_says_where_to_get_it():
    """🔴 Red on main: the raw dump came through."""
    sentence = _mapped(
        _ClientError(
            "AccessDeniedException",
            "You don't have access to the model with the specified model ID.",
        )
    )
    assert sentence == (
        f"This AWS account has no access to the Bedrock model '{MODEL}'. Request access to it in "
        "the Amazon Bedrock console for that region, or pick a different model in Settings → "
        "Models."
    )


def test_the_region_is_named_when_the_provider_knows_it():
    sentence = _mapped(
        _ClientError("AccessDeniedException", "You don't have access to the model."),
        region="eu-central-1",
    )
    assert f"'{MODEL}' in eu-central-1." in sentence


def test_a_message_without_the_parsed_response_is_read_from_its_text():
    """botocore's EventStreamError reaches the stream as its string too."""
    raw = Exception(
        "An error occurred (AccessDeniedException) when calling the ConverseStream operation: "
        "Model access is denied due to IAM user or service role is not authorized to perform the "
        "required AWS Marketplace actions."
    )
    assert _mapped(raw).startswith(f"This AWS account has no access to the Bedrock model '{MODEL}'")


@pytest.mark.parametrize(
    "error",
    [
        _ClientError(
            "UnrecognizedClientException", "The security token included in the request is invalid."
        ),
        _ClientError(
            "ExpiredTokenException", "The security token included in the request is expired"
        ),
    ],
)
def test_credentials_aws_turns_down_say_to_sign_in_again(error):
    """🔴 Red on main: the raw dump came through."""
    assert _mapped(error) == (
        "AWS turned down the credentials Bedrock used: their security token is invalid or has "
        "expired. Sign in to AWS again (`aws sso login` for an SSO profile), or pick a different "
        "model in Settings → Models."
    )


def test_the_sentences_carry_no_word_the_chat_would_rewrite_as_an_api_key_problem():
    """Core shows a ``ProviderResolutionError`` as written; an older core ran it through a
    substring map first, where "permission", "unauthorized" or a bare 401/403 read as a bad
    API key. None of these sentences says any of them."""
    import re

    needles = (
        "permission",
        "unauthorized",
        "authentication",
        "forbidden",
        "access denied",
        "not authorized to",
        "don't have access",
        "do not have access",
    )
    for error in (
        _ClientError("AccessDeniedException", "is not authorized to perform: bedrock:InvokeModel"),
        _ClientError("AccessDeniedException", "You don't have access to the model."),
        _ClientError(
            "ExpiredTokenException", "The security token included in the request is expired"
        ),
    ):
        low = _mapped(error, region="us-east-1").lower()
        assert not [n for n in needles if n in low], low
        assert not re.search(r"(?<![\d.,])(401|403)(?![\d])", low), low


def test_other_errors_still_pass_through():
    from provider import _friendly_bedrock_error

    raw = _ClientError("ThrottlingException", "Too many requests, please wait before trying again.")
    assert _friendly_bedrock_error(raw, MODEL) is raw


# ── credentials that come back are seen ──


def _boto3(state: dict) -> types.ModuleType:
    """A boto3 whose default credential chain resolves only while ``state["ok"]``."""
    mod = types.ModuleType("boto3")

    class Session:
        def __init__(self, profile_name=None):
            state["probes"] = state.get("probes", 0) + 1

        def get_credentials(self):
            return object() if state["ok"] else None

    mod.Session = Session
    return mod


def test_credentials_that_come_back_are_seen_without_a_restart(monkeypatch):
    """🔴 Red on main: the first "no" stood for the life of the process."""
    import provider

    state = {"ok": False}
    monkeypatch.setitem(sys.modules, "boto3", _boto3(state))
    monkeypatch.setattr(provider, "_cred_cache", {})
    clock = [1000.0]
    monkeypatch.setattr(provider._time, "monotonic", lambda: clock[0])

    assert asyncio.run(provider._creds_ok("us-east-1", "work")) is False
    state["ok"] = True  # `aws sso login`
    assert asyncio.run(provider._creds_ok("us-east-1", "work")) is False, "a burst is answered once"

    clock[0] += provider._CRED_MISSING_TTL + 1
    assert asyncio.run(provider._creds_ok("us-east-1", "work")) is True


def test_credentials_that_lapse_are_noticed(monkeypatch):
    import provider

    state = {"ok": True}
    monkeypatch.setitem(sys.modules, "boto3", _boto3(state))
    monkeypatch.setattr(provider, "_cred_cache", {})
    clock = [1000.0]
    monkeypatch.setattr(provider._time, "monotonic", lambda: clock[0])

    assert asyncio.run(provider._creds_ok("us-east-1", "")) is True
    state["ok"] = False
    clock[0] += provider._CRED_OK_TTL + 1
    assert asyncio.run(provider._creds_ok("us-east-1", "")) is False
    assert state["probes"] == 2
