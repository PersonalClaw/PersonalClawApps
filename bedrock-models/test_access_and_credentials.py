"""Bedrock names the real fix for a refused call, and sees credentials that come back.

Measured before this was written:

* An ``AccessDeniedException`` passed through ``_friendly_bedrock_error`` unchanged. The chat
  showed the raw botocore dump, or core's "rejected the API key" line whenever the text said
  "permission" or the account id held the digits 401. The credentials are fine in both cases:
  the identity's IAM policy lacks the action, or the account has no access to the model.
* ``_creds_ok`` cached its answer for the life of the process. A profile whose credentials
  were missing once (an SSO session that had lapsed) kept embeddings, images, video and speech
  unavailable after ``aws sso login`` until the gateway restarted.
* An AWS setup that could not sign in — no credentials, a profile's ``credential_process``
  that failed, an SSO sign-in never made, a profile or a region that isn't one — reached the
  chat, the connection test and the image tool as botocore's own words ("Unable to locate
  credentials", or what the credential command printed) with no next step, and the connection
  test added that it was "showing a fallback catalog" that does not exist.

The last group is driven through real boto3 over a scratch AWS config, with every AWS variable
cleared and botocore's HTTP send replaced by a recorder that fails the call: the credential
chain is AWS's own, a credential command is a script written here, and no request leaves.
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
import types
from pathlib import Path

import pytest
from botocore import exceptions as aws_errors

from personalclaw.sdk.model import ModelDiscoveryError, ProviderResolutionError

MODEL = "amazon.nova-pro-v1:0"

#: Where every setup sentence says the instance's AWS Profile is set.
SET_PROFILE = "set AWS Profile on this Amazon Bedrock instance in Settings → Providers (under Advanced)"


@pytest.fixture(autouse=True)
def _no_profile_from_the_environment(monkeypatch):
    """The sentences name the profile boto3 signs in with, which the environment can name too;
    whoever runs this suite may have one set."""
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)


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
            "User: arn:aws:sts::111122223333:assumed-role/Dev/alice is not authorized to perform: "
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


# ── an AWS setup that cannot sign in says what is missing, and what to do ──

#: What the chat, the connection test and the image tool say when the chain has no credentials.
NO_CREDENTIALS = (
    "No AWS credentials were found: this Amazon Bedrock instance names no AWS profile, and the "
    "default credential chain has none. Sign in with your AWS tool (for example `aws sso login`, "
    f"or `aws configure` to enter access keys), then try again, or {SET_PROFILE} to a profile "
    "that has credentials. Details: Unable to locate credentials"
)
#: What the profile's own credential command printed to stderr before it exited non-zero.
PRINTED = "fixture-helper: session expired, run fixture-helper login"
#: What is said when that command fails, for the profile ``work``.
CREDENTIAL_COMMAND_FAILED = (
    "The command the AWS profile 'work' runs to get its AWS credentials (its credential_process) "
    "failed. Run that command in a terminal to see why, fix it or sign in again, then try again, "
    f"or {SET_PROFILE} to a different profile. Details: Error when retrieving credentials from "
    f"custom-process: {PRINTED}"
)
#: How every SSO case starts, for the profile ``work``.
SSO_SIGN_IN = (
    "The AWS SSO sign-in for the AWS profile 'work' has expired or hasn't been made. Sign in "
    "with `aws sso login --profile work`, then try again."
)


def _command_failed(printed: str = PRINTED + "\n") -> Exception:
    """botocore's error for a credential command that exited non-zero."""
    return aws_errors.CredentialRetrievalError(provider="custom-process", error_msg=printed)


def test_no_credentials_say_what_is_missing_and_how_to_sign_in():
    """🔴 Red on main: "Unable to locate credentials" was the whole message."""
    assert _mapped(aws_errors.NoCredentialsError()) == NO_CREDENTIALS


def test_no_credentials_for_a_named_profile_name_it_and_the_sign_in_for_it():
    assert _mapped(aws_errors.NoCredentialsError(), profile="work") == (
        "No AWS credentials were found for the AWS profile 'work'. Sign in with your AWS tool (for "
        "example `aws sso login --profile work`, or `aws configure --profile work` to enter access "
        f"keys), then try again, or {SET_PROFILE} to a profile that has credentials. Details: "
        "Unable to locate credentials"
    )


def test_a_failing_credential_command_says_so_and_keeps_what_it_printed():
    """🔴 Red on main: botocore's line, with the command's stderr in it, was the whole message."""
    assert _mapped(_command_failed(), profile="work") == CREDENTIAL_COMMAND_FAILED


def test_a_credential_command_that_printed_nothing_is_still_named():
    """A command such as ``/usr/bin/false`` exits non-zero and prints nothing at all."""
    assert _mapped(_command_failed(""), profile="work").startswith(
        "The command the AWS profile 'work' runs to get its AWS credentials (its "
        "credential_process) failed."
    )


def test_the_profile_the_environment_names_is_the_one_the_sentence_names(monkeypatch):
    """With no AWS Profile on the instance, boto3 signs in with the profile the environment
    names, so that is the one to sign in again or fix."""
    monkeypatch.setenv("AWS_PROFILE", "from-env")

    sentence = _mapped(_command_failed())

    assert sentence.startswith(
        "The command the AWS profile 'from-env' runs to get its AWS credentials"
    ), sentence


@pytest.mark.parametrize(
    ("error", "start"),
    [
        (
            aws_errors.ProfileNotFound(profile="absent"),
            "The AWS profile 'absent' isn't in your AWS config (the config and credentials files "
            "in ~/.aws). Add it with your AWS tool (for example `aws configure --profile "
            f"absent`), then try again, or {SET_PROFILE} to a profile you have.",
        ),
        (aws_errors.UnauthorizedSSOTokenError(), SSO_SIGN_IN),
        (
            aws_errors.SSOTokenLoadError(
                error_msg="Token for https://sso.example.com/start does not exist"
            ),
            SSO_SIGN_IN,
        ),
        (
            aws_errors.TokenRetrievalError(
                provider="sso", error_msg="Token has expired and refresh failed"
            ),
            SSO_SIGN_IN,
        ),
        (
            aws_errors.PartialCredentialsError(provider="env", cred_var="AWS_SECRET_ACCESS_KEY"),
            "Only part of a set of AWS credentials was found, so Amazon Bedrock can't sign in with "
            "them. Complete or remove that set with your AWS tool, then try again, or "
            f"{SET_PROFILE} to a profile that has credentials.",
        ),
        (
            aws_errors.InvalidConfigError(
                error_msg='The profile "work" is configured to use SSO but is missing required '
                "configuration: sso_region"
            ),
            "The AWS configuration of the AWS profile 'work' can't be used. Correct it in the "
            f"config file in ~/.aws, then try again, or {SET_PROFILE} to a different profile.",
        ),
        (
            aws_errors.RefreshWithMFAUnsupportedError(),
            "The AWS profile 'work' needs an MFA code to refresh its credentials, and Amazon "
            "Bedrock can't ask you for one. Refresh them with your AWS tool, then try again, or "
            f"{SET_PROFILE} to a profile that doesn't need MFA.",
        ),
        (
            aws_errors.InvalidRegionError(region_name="us_east_1"),
            "'us_east_1' isn't an AWS region name. Set AWS Region on this Amazon Bedrock instance "
            "in Settings → Providers to one like us-east-1, then try again.",
        ),
        (
            aws_errors.EndpointConnectionError(
                endpoint_url="https://bedrock-runtime.region.example/model/m/converse-stream"
            ),
            "No connection could be made to AWS at bedrock-runtime.region.example. Check that "
            "this machine is online and that us-east-1 is a region Amazon Bedrock runs in (AWS "
            "Region on this Amazon Bedrock instance in Settings → Providers), then try again.",
        ),
    ],
)
def test_each_setup_failure_says_what_is_wrong_and_what_to_do(error, start):
    """🔴 Red on main: every one of these passed through as botocore's own line."""
    sentence = _mapped(error, profile="work", region="us-east-1")

    assert sentence == f"{start} Details: {' '.join(str(error).split())}"
    assert "API key" not in sentence


def test_an_expired_aws_login_says_to_sign_in_again():
    sentence = _mapped(aws_errors.LoginRefreshRequired(), profile="work")

    assert sentence.startswith(
        "The AWS sign-in for the AWS profile 'work' can't be used. Sign in again with `aws login "
        "--profile work`, then try again. Details: "
    ), sentence


def test_a_role_the_profile_may_not_assume_is_about_the_role_not_the_model():
    """🔴 Red on main: STS refusing the profile's role passed through, and the chat's own map
    read "not authorized to" as the account having no access to the model."""
    refused = aws_errors.ClientError(
        {
            "Error": {
                "Code": "AccessDenied",
                "Message": "User: arn:aws:iam::111122223333:user/fixture is not authorized to "
                "perform: sts:AssumeRole on resource: arn:aws:iam::111122223333:role/Fixture",
            }
        },
        "AssumeRole",
    )

    sentence = _mapped(refused, profile="work")

    assert sentence.startswith(
        "AWS refused to let the AWS profile 'work' assume its IAM role (AccessDenied). Check "
        "that the role exists and lets the identity the profile starts from assume it, then try "
        f"again, or {SET_PROFILE} to a different profile. Details: "
    ), sentence
    assert MODEL not in sentence


def test_what_a_credential_command_printed_is_redacted_before_it_is_cut():
    """The detail is whatever the command printed, so a credential in it is redacted — BEFORE
    the detail is cut to length, or a key cut in half would slip past the redactor. The key id
    here starts four characters before the cut."""
    prefix = "Error when retrieving credentials from custom-process: "
    printed = "x" * (200 - len(prefix) - 5) + " AKIAIOSFODNN7EXAMPLE"

    detail = _mapped(_command_failed(printed), profile="work").split(" Details: ", 1)[1]

    assert detail.startswith(prefix) and detail.endswith("…"), detail
    assert "AKIA" not in detail


# ── the same failures, driven through real boto3 ──


@pytest.fixture
def aws(tmp_path, monkeypatch):
    """A scratch AWS setup for real boto3, with a recorder in place of botocore's HTTP send.

    HOME and both AWS config files point into ``tmp_path``, every ``AWS_*`` variable is cleared
    and the instance-metadata lookup is off, so the credential chain finds only what a test
    writes. The recorder fails any request, so a test that got as far as AWS fails instead of
    reaching it. boto3 is dropped from ``sys.modules`` afterwards: the provider module is proven
    to load without it, and that test must not see this one's import."""
    import botocore.httpsession

    import provider

    for var in [name for name in os.environ if name.startswith("AWS_") or name == "BOTO_CONFIG"]:
        monkeypatch.delenv(var)
    home = tmp_path / "home"
    (home / ".aws").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(home / ".aws" / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(home / ".aws" / "credentials"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    sent: list[str] = []

    def _record(self, request):
        sent.append(str(request.url))
        raise AssertionError(f"a request left for {request.url}")

    monkeypatch.setattr(botocore.httpsession.URLLib3Session, "send", _record)
    monkeypatch.setattr(provider, "_BEDROCK_CACHE", {})
    before = {name for name in sys.modules if name.partition(".")[0] == "boto3"}
    yield types.SimpleNamespace(config=home / ".aws" / "config", scripts=tmp_path, sent=sent)
    for name in [n for n in sys.modules if n.partition(".")[0] == "boto3" and n not in before]:
        del sys.modules[name]
    assert sent == [], f"requests left the process: {sent}"


def _profile(aws, profile: str, **settings: str) -> None:
    lines = [f"[profile {profile}]", *(f"{key} = {value}" for key, value in settings.items())]
    aws.config.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _command(aws, body: str) -> str:
    """A credential command: a shell script written here, named by its absolute path."""
    script: Path = aws.scripts / "fixture-helper.sh"
    script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


def _tested(options: dict[str, str]):
    """What "Save and test" reports for an instance with these settings."""
    import provider

    return asyncio.run(provider.create_catalog(options).test_connection())


async def _turn(bedrock) -> list:
    return [event async for event in bedrock.complete([{"role": "user", "content": "hi"}])]


def test_save_and_test_with_no_credentials_names_the_cause_not_a_fallback_catalog(aws):
    """🔴 Red on main: "Could not reach the AWS Bedrock control plane (check credentials/region);
    showing a fallback catalog." No such catalog exists, and the cause went unnamed."""
    result = _tested({"region": "us-east-1"})

    assert (result.ok, result.detail) == (False, NO_CREDENTIALS)
    assert result.rejected_credential is False, "this instance stores no key to update"


def test_save_and_test_with_a_failing_credential_command_names_it(aws):
    """🔴 Red on main: the same false fallback-catalog line."""
    helper = _command(aws, f'echo "{PRINTED}" >&2\nexit 1\n')
    _profile(aws, "work", credential_process=helper)

    result = _tested({"region": "us-east-1", "profile": "work"})

    assert (result.ok, result.detail) == (False, CREDENTIAL_COMMAND_FAILED)


@pytest.mark.parametrize(
    "body", ['echo "Sign in at https://sso.example.com first."\nexit 0\n', None]
)
def test_a_credential_command_that_gives_no_credentials_or_cannot_start_is_named(aws, body):
    """One prints something other than the credentials JSON; the other is not there to run.
    botocore passes both through as the bare ``ValueError`` / ``OSError`` they are."""
    command = _command(aws, body) if body else "/nonexistent/pc-fixture-helper"
    _profile(aws, "work", credential_process=command)

    result = _tested({"region": "us-east-1", "profile": "work"})

    assert result.detail.startswith(
        "The command the AWS profile 'work' runs to get its AWS credentials (its "
        "credential_process) failed. "
    ), result.detail


def test_an_sso_sign_in_never_made_says_to_sign_in(aws):
    _profile(
        aws,
        "work",
        sso_start_url="https://sso.example.com/start",
        sso_region="us-east-1",
        sso_account_id="111122223333",
        sso_role_name="Fixture",
    )

    result = _tested({"region": "us-east-1", "profile": "work"})

    assert result.detail == (
        f"{SSO_SIGN_IN} Details: Error loading SSO Token: Token for "
        "https://sso.example.com/start does not exist"
    )


@pytest.mark.parametrize(
    ("options", "start"),
    [
        ({"region": "us_east_1"}, "'us_east_1' isn't an AWS region name. "),
        (
            {"region": "us-east-1", "profile": "absent"},
            "The AWS profile 'absent' isn't in your AWS config ",
        ),
    ],
)
def test_a_region_or_profile_that_is_not_one_is_named(aws, options, start):
    assert _tested(options).detail.startswith(start)


def test_a_chat_turn_with_a_failing_credential_command_says_what_to_do(aws):
    """🔴 Red on main: the command fails while boto3 builds the client — before the stream's own
    mapping ever ran — so the chat showed botocore's line and nothing else."""
    import provider

    _profile(aws, "work", credential_process=_command(aws, f'echo "{PRINTED}" >&2\nexit 1\n'))
    bedrock = provider.BedrockProvider(model=MODEL, region="us-east-1", profile_name="work")

    with pytest.raises(ProviderResolutionError) as refused:
        asyncio.run(_turn(bedrock))

    assert str(refused.value) == CREDENTIAL_COMMAND_FAILED
    assert isinstance(refused.value.__cause__, aws_errors.CredentialRetrievalError), (
        "the gateway log keeps the SDK's own error"
    )


def test_a_chat_turn_with_no_credentials_says_what_to_do(aws):
    """🔴 Red on main: "Unable to locate credentials", and nothing else."""
    import provider

    with pytest.raises(ProviderResolutionError) as refused:
        asyncio.run(_turn(provider.BedrockProvider(model=MODEL, region="us-east-1")))

    assert str(refused.value) == NO_CREDENTIALS


def test_an_image_with_no_credentials_says_what_to_do(aws):
    """🔴 Red on main: "Bedrock image generation failed: Unable to locate credentials"."""
    import provider
    from personalclaw.sdk.image import ImageGenError

    adapter = provider.BedrockImageProvider(region="us-east-1", name="my-bedrock")
    with pytest.raises(ImageGenError) as refused:
        asyncio.run(adapter.generate("a heron", model="amazon.nova-canvas-v1:0"))

    assert str(refused.value) == NO_CREDENTIALS


def test_a_listing_the_policy_denies_names_the_action(aws, monkeypatch):
    """A real botocore client with its two listings stubbed to AWS's refusal: nothing is
    listed, so the refusal is the answer, and it names the action to add."""
    from botocore.session import get_session
    from botocore.stub import Stubber

    import provider

    denied = (
        "User: arn:aws:sts::111122223333:assumed-role/Fixture/test is not authorized to perform: "
        "bedrock:ListFoundationModels because no identity-based policy allows the "
        "bedrock:ListFoundationModels action"
    )
    client = get_session().create_client(
        "bedrock", region_name="eu-west-1", aws_access_key_id="fake", aws_secret_access_key="fake"
    )
    stub = Stubber(client)
    for operation in ("list_foundation_models", "list_inference_profiles"):
        stub.add_client_error(operation, "AccessDeniedException", denied, http_status_code=403)
    session = types.SimpleNamespace(client=lambda service, region_name=None: client)
    monkeypatch.setitem(
        sys.modules, "boto3", types.SimpleNamespace(Session=lambda profile_name=None: session)
    )

    with stub, pytest.raises(ModelDiscoveryError) as refused:
        asyncio.run(provider.create_catalog({"region": "eu-west-1"}).list_models())
    stub.assert_no_pending_responses()

    assert str(refused.value).startswith(
        "Your AWS credentials aren't allowed to call bedrock:ListFoundationModels in eu-west-1, "
        "so Amazon Bedrock's models can't be listed. Add that action to the IAM policy of the "
        f"identity Bedrock signs in as, then try again, or {SET_PROFILE} to a profile that has "
        "it. Details: "
    ), str(refused.value)
    assert refused.value.rejected_credential is False


def test_the_store_installs_it_without_a_scanner_warning(tmp_path):
    """The sentences name the AWS files by their folder: a path into it, in an app's code, reads
    to the Store's install scanner as the app reading that file, and a first-party install then
    says "The security scanner raised warnings". This file's own copies of the sentences are
    scanned too."""
    from apps_testkit import design_rails
    from personalclaw.supply_chain import default_scanner

    design_rails.assert_scans_clean(Path(__file__).parent, default_scanner, tmp_path)


# ── speech-to-text and embeddings say it too ──

#: What an adapter says when it is asked why it is unavailable, before any call has failed: the
#: credential chain answered "none" rather than raising, so there is no SDK error to add.
NO_CREDENTIALS_FOUND = NO_CREDENTIALS.split(" Details: ", 1)[0]


def test_speech_to_text_with_no_credentials_says_what_to_do(aws, tmp_path, monkeypatch):
    """🔴 Red on main: unavailable was False with nothing saying why, and a transcription
    came back ``None``, which core read as audio with no speech in it."""
    import provider
    from personalclaw.sdk.stt import SttError

    monkeypatch.setattr(provider, "_cred_cache", {})
    clip = tmp_path / "recording.webm"
    clip.write_bytes(b"\x1aE\xdf\xa3")
    adapter = provider.BedrockSTTProvider(region="us-east-1", name="my-bedrock", s3_bucket="clips")

    assert asyncio.run(adapter.unavailable_reason()) == NO_CREDENTIALS_FOUND
    with pytest.raises(SttError) as failed:
        asyncio.run(adapter.transcribe(str(clip), model=provider.TRANSCRIBE_MODEL))

    assert str(failed.value) == NO_CREDENTIALS
    assert isinstance(failed.value.__cause__, aws_errors.NoCredentialsError)


def test_embeddings_with_no_credentials_say_what_to_do(aws, monkeypatch):
    """🔴 Red on main: ``None`` and a DEBUG line, and nothing for a refused re-index to say."""
    import provider

    monkeypatch.setattr(provider, "_cred_cache", {})
    adapter = provider.BedrockEmbeddingProvider(region="us-east-1", name="my-bedrock")

    assert asyncio.run(adapter.unavailable_reason()) == NO_CREDENTIALS_FOUND
    assert asyncio.run(adapter.embed("a heron", model="amazon.titan-embed-text-v2:0")) is None
    assert asyncio.run(adapter.unavailable_reason()) == NO_CREDENTIALS, "the call's own failure"


def test_image_and_video_with_no_credentials_say_why_they_are_unavailable(aws, monkeypatch):
    """🔴 Red before: the SDK's default "" — Settings → Models left both out with nothing
    saying why."""
    import provider

    monkeypatch.setattr(provider, "_cred_cache", {})
    image = provider.BedrockImageProvider(region="us-east-1", name="my-bedrock")
    video = provider.BedrockVideoProvider(region="us-east-1", name="my-bedrock", s3_bucket="clips")

    assert asyncio.run(image.unavailable_reason()) == NO_CREDENTIALS_FOUND
    assert asyncio.run(video.unavailable_reason()) == NO_CREDENTIALS_FOUND


def test_video_with_no_bucket_says_it_needs_one():
    import provider

    video = provider.BedrockVideoProvider(region="us-east-1", name="my-bedrock", s3_bucket="")
    video._s3_bucket = ""  # the BEDROCK_VIDEO_S3_BUCKET fallback is not this test's

    assert asyncio.run(video.unavailable_reason()) == (
        "Bedrock video generation needs an S3 bucket for Nova Reel to write the video to. Set S3 "
        "Bucket on this Amazon Bedrock instance in Settings → Providers (under Advanced), or the "
        "BEDROCK_VIDEO_S3_BUCKET environment variable, then try again."
    )
