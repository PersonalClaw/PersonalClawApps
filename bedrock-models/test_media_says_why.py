"""Bedrock's speech-to-text, embeddings and video say why they failed, and what to do.

Measured before this was written:

* Speech-to-text answered every failure with ``None`` and logged it at DEBUG: no S3 bucket, the
  AWS sign-in, a bucket that isn't there, a job Amazon Transcribe failed or never finished. Core
  read ``None`` as nothing said, so the composer's microphone went quiet and a knowledge item
  landed with an empty transcript. And unavailable was only False: nothing said it was the
  bucket, or the credentials.
* An embedding that failed was ``None`` and a DEBUG line, so a re-index refused on it could say
  only that the model "is not available", for a model whose account had no access to it.
* A Nova Reel job that failed came back as "Bedrock video generation failed: <AWS's words>",
  one that ran long as "timed out after 600s" with nothing saying the job runs on and writes its
  video anyway, and a finished video that could not be downloaded as the S3 error alone.

Every AWS client here is a fake, and nothing is sent.
"""

from __future__ import annotations

import asyncio
import datetime
import io
import json
import logging
import os
import sys
import types
from typing import Any

import pytest
from botocore import exceptions as aws_errors
from botocore.response import StreamingBody
from botocore.stub import ANY

import provider as prov
from personalclaw.sdk.stt import SttError
from personalclaw.sdk.video import VideoGenError

#: What each speech-to-text call names, as its binding does (``Bedrock:amazon-transcribe``).
TRANSCRIBE = prov.TRANSCRIBE_MODEL
EMBEDDER = "amazon.titan-embed-text-v2:0"
REEL = "amazon.nova-reel-v1:1"
NEEDS_BUCKET = (
    "Speech-to-text with Amazon Transcribe needs an S3 bucket to upload each recording to. Set S3 "
    "Bucket on this Amazon Bedrock instance in Settings → Providers (under Advanced), or the "
    "BEDROCK_VIDEO_S3_BUCKET environment variable, then try again."
)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _nothing_from_the_environment(monkeypatch):
    """No bucket, profile or earlier answer reaches a test from whoever runs the suite."""
    for var in ("BEDROCK_VIDEO_S3_BUCKET", "AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(prov, "_cred_cache", {})
    monkeypatch.setattr(prov, "_WARNED_AT", {})


class _Clock:
    """Time that moves only when something sleeps, so a wait of minutes takes none."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return 1_767_225_600.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _client_error(code: str, message: str, operation: str) -> Exception:
    return aws_errors.ClientError({"Error": {"Code": code, "Message": message}}, operation)


class _UploadFailed(Exception):
    """The shape of boto3's ``S3UploadFailedError``: an S3 error folded into its message, with no
    parsed response left on it."""

    def __init__(self, path: str, bucket: str, key: str, error: Exception) -> None:
        super().__init__(f"Failed to upload {path} to {bucket}/{key}: {error}")


# ── speech-to-text ──────────────────────────────────────────────────────────────────────────


class _Transcribe:
    """Amazon Transcribe's client: a job reads each status in ``statuses`` in turn."""

    def __init__(self, calls: list, statuses: list[dict[str, Any]], refusal: Exception | None):
        self._calls = calls
        self._statuses = statuses
        self._refusal = refusal

    def start_transcription_job(self, **kwargs: Any) -> None:
        self._calls.append(("start_transcription_job", kwargs["TranscriptionJobName"]))
        if self._refusal is not None:
            raise self._refusal

    def get_transcription_job(self, **kwargs: Any) -> dict[str, Any]:
        self._calls.append(("get_transcription_job", kwargs["TranscriptionJobName"]))
        job = self._statuses.pop(0) if len(self._statuses) > 1 else self._statuses[0]
        return {"TranscriptionJob": job}

    def delete_transcription_job(self, **kwargs: Any) -> None:
        self._calls.append(("delete_transcription_job", kwargs["TranscriptionJobName"]))


class _S3:
    def __init__(self, calls: list, refusal: Exception | None) -> None:
        self._calls = calls
        self._refusal = refusal

    def upload_file(self, path: str, bucket: str, key: str) -> None:
        self._calls.append(("upload_file", bucket))
        if self._refusal is not None:
            raise _UploadFailed(path, bucket, key, self._refusal)

    def delete_object(self, **kwargs: Any) -> None:
        self._calls.append(("delete_object", kwargs["Bucket"]))


@pytest.fixture
def aws_stt(monkeypatch, tmp_path):
    """A speech-to-text adapter on the bucket ``clips``, whose AWS a test sets: the statuses its
    job reads, and a refusal the upload or the job's start answers with. Returns the adapter, a
    recording, the calls made, and the setter."""
    calls: list[tuple[str, str]] = []
    world: dict[str, Any] = {"statuses": [{"TranscriptionJobStatus": "IN_PROGRESS"}]}

    def _session(_self):
        s3 = _S3(calls, world.get("upload_refusal"))
        transcribe = _Transcribe(calls, world["statuses"], world.get("start_refusal"))
        return types.SimpleNamespace(
            client=lambda service, region_name=None: {"s3": s3, "transcribe": transcribe}[service]
        )

    monkeypatch.setattr(prov.BedrockSTTProvider, "_get_session", _session)
    monkeypatch.setattr(prov, "_time", _Clock())
    clip = tmp_path / "recording.webm"
    clip.write_bytes(b"\x1aE\xdf\xa3")
    adapter = prov.BedrockSTTProvider(name="my-bedrock", region="us-east-1", s3_bucket="clips")
    return types.SimpleNamespace(adapter=adapter, clip=str(clip), calls=calls, world=world)


def _transcribed(aws_stt) -> str | None:
    return _run(aws_stt.adapter.transcribe(aws_stt.clip, model=TRANSCRIBE))


def test_speech_to_text_without_a_bucket_says_to_set_one(tmp_path):
    """🔴 Red before: unavailable said nothing, and a transcription came back ``None``."""
    clip = tmp_path / "recording.webm"
    clip.write_bytes(b"\x1aE\xdf\xa3")
    adapter = prov.BedrockSTTProvider(name="my-bedrock")

    assert _run(adapter.unavailable_reason()) == NEEDS_BUCKET
    with pytest.raises(SttError) as failed:
        _run(adapter.transcribe(str(clip), model=TRANSCRIBE))
    assert str(failed.value) == NEEDS_BUCKET


def test_speech_to_text_that_cannot_sign_in_says_why(monkeypatch):
    """🔴 Red before: False, and nothing else."""

    class _Session:
        def __init__(self, profile_name=None) -> None:
            pass

        def get_credentials(self):
            raise aws_errors.UnauthorizedSSOTokenError()

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(Session=_Session))
    adapter = prov.BedrockSTTProvider(name="my-bedrock", profile="work", s3_bucket="clips")

    assert _run(adapter.is_available()) is False
    assert _run(adapter.unavailable_reason()).startswith(
        "The AWS SSO sign-in for the AWS profile 'work' has expired or hasn't been made. Sign in "
        "with `aws sso login --profile work`, then try again. Details: "
    )


def test_speech_to_text_that_can_sign_in_has_nothing_to_say(monkeypatch):
    class _Session:
        def __init__(self, profile_name=None) -> None:
            pass

        def get_credentials(self):
            return object()

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(Session=_Session))
    adapter = prov.BedrockSTTProvider(name="my-bedrock", s3_bucket="clips")

    assert _run(adapter.is_available()) is True
    assert _run(adapter.unavailable_reason()) == ""


def test_a_job_transcribe_failed_says_what_to_do_and_cleans_up(aws_stt):
    """🔴 Red before: ``None``, and the reason only in an ERROR log line."""
    reason = (
        "The media format provided does not match the detected media format. Check the media "
        "format and try your request again."
    )
    aws_stt.world["statuses"] = [{"TranscriptionJobStatus": "FAILED", "FailureReason": reason}]

    with pytest.raises(SttError) as failed:
        _transcribed(aws_stt)

    assert str(failed.value) == (
        "Amazon Transcribe couldn't transcribe this audio. Fix what its reason names, then try "
        f"again. Details: {reason}"
    )
    done = [name for name, _ in aws_stt.calls]
    assert done[-2:] == ["delete_object", "delete_transcription_job"], "the upload is deleted"


def test_a_job_that_gives_no_reason_says_so():
    assert prov._transcribe_job_failed("") == (
        "Amazon Transcribe couldn't transcribe this audio, and gave no reason. Try again."
    )


def test_a_job_that_does_not_finish_says_so(aws_stt):
    """🔴 Red before: ``None`` after a minute of polling, which read as audio with no speech."""
    with pytest.raises(SttError) as failed:
        _transcribed(aws_stt)

    assert str(failed.value) == (
        "Amazon Transcribe hadn't finished this audio after 1 minute, so speech-to-text stopped "
        "waiting for it. Try again; if it keeps happening, try a shorter recording."
    )
    polls = [name for name, _ in aws_stt.calls if name == "get_transcription_job"]
    assert len(polls) == prov._STT_POLL_TRIES


def test_audio_with_no_speech_is_an_empty_transcript_not_a_failure(aws_stt, monkeypatch):
    """The one outcome that is "nothing": a job that finished and heard no speech."""
    import urllib.request

    aws_stt.world["statuses"] = [
        {
            "TranscriptionJobStatus": "COMPLETED",
            "Transcript": {"TranscriptFileUri": "https://transcripts.example.com/job.json"},
        }
    ]
    answer = {"results": {"transcripts": [{"transcript": ""}]}}
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda uri, timeout=None: io.BytesIO(json.dumps(answer).encode())
    )

    assert _transcribed(aws_stt) == ""


@pytest.mark.parametrize(
    ("where", "refusal", "said"),
    [
        (
            "upload_refusal",
            _client_error("NoSuchBucket", "The specified bucket does not exist", "PutObject"),
            "The S3 bucket 'clips' that speech-to-text uploads each recording to doesn't exist. "
            "Create it in us-east-1, or set S3 Bucket on this Amazon Bedrock instance in Settings "
            "→ Providers (under Advanced) to one you have, then try again. Details: Failed to "
            "upload ",
        ),
        (
            "upload_refusal",
            _client_error("AccessDenied", "Access Denied", "PutObject"),
            "Your AWS credentials aren't allowed to call s3:PutObject on the S3 bucket 'clips', "
            "which speech-to-text needs. Add that action to the IAM policy of the identity "
            "Bedrock signs in as, then try again. Details: Failed to upload ",
        ),
        (
            "start_refusal",
            _client_error(
                "AccessDeniedException",
                "User: arn:aws:iam::111122223333:user/fixture is not authorized to perform: "
                "transcribe:StartTranscriptionJob because no identity-based policy allows the "
                "transcribe:StartTranscriptionJob action",
                "StartTranscriptionJob",
            ),
            "Your AWS credentials aren't allowed to call transcribe:StartTranscriptionJob, which "
            "speech-to-text needs. Add that action to the IAM policy of the identity Bedrock "
            "signs in as, then try again. Details: An error occurred (AccessDeniedException) ",
        ),
        (
            "upload_refusal",
            _client_error(
                "InvalidAccessKeyId",
                "The AWS Access Key Id you provided does not exist in our records.",
                "PutObject",
            ),
            "AWS turned down the credentials speech-to-text signed in with: they are invalid or "
            "have expired. Sign in to AWS again (`aws sso login` for an SSO profile), then try "
            "again. Details: Failed to upload ",
        ),
        (
            "start_refusal",
            _client_error("ThrottlingException", "Rate exceeded", "StartTranscriptionJob"),
            "Amazon Transcribe couldn't transcribe this audio. Try again; if it keeps failing, "
            "check the gateway log. Details: An error occurred (ThrottlingException) ",
        ),
    ],
)
def test_a_refused_call_names_its_fix(aws_stt, caplog, where, refusal, said):
    """🔴 Red before: every one of these was ``None`` and a DEBUG line."""
    aws_stt.world[where] = refusal
    caplog.set_level(logging.WARNING, logger="bedrock_models")

    with pytest.raises(SttError) as failed:
        _transcribed(aws_stt)

    assert str(failed.value).startswith(said), str(failed.value)
    assert isinstance(failed.value.__cause__, Exception), "the gateway log keeps the SDK's error"
    warned = [
        r for r in caplog.records if r.name == "bedrock_models" and r.levelno == logging.WARNING
    ]
    assert [r.getMessage().split("\n", 1)[0] for r in warned] == [
        f"Amazon Transcribe on 'my-bedrock' failed: {failed.value}"
    ], "said once, with its traceback after it"


def test_a_refusal_that_names_no_action_names_every_one_speech_to_text_needs():
    said = prov._stt_failure(
        _client_error("AccessDenied", "Access Denied", "HeadBucket"),
        bucket="clips",
        region="us-east-1",
        profile=None,
    )

    assert said.startswith(
        "AWS refused a call speech-to-text makes (AccessDenied). It needs s3:PutObject and "
        "s3:GetObject on the S3 bucket 'clips', and transcribe:StartTranscriptionJob and "
        "transcribe:GetTranscriptionJob: add them to the IAM policy of the identity Bedrock signs "
        "in as, then try again. Details: "
    ), said


def test_every_speech_to_text_sentence_keeps_clear_of_the_composers_setup_hint():
    """The chat composer turns an error that says "not available" into "Voice input needs a
    speech-to-text model — configure one", which is false for a model that is bound."""
    said = [
        NEEDS_BUCKET,
        prov._transcribe_job_failed("reason"),
        prov._transcribe_job_failed(""),
        *(
            prov._stt_failure(
                _client_error(code, "m", operation), bucket="b", region="r", profile=None
            )
            for code, operation in (
                ("NoSuchBucket", "PutObject"),
                ("AccessDenied", "PutObject"),
                ("AccessDenied", "HeadBucket"),
                ("InvalidToken", "PutObject"),
                ("InternalFailure", "GetTranscriptionJob"),
            )
        ),
    ]

    assert [s for s in said if "not available" in s.lower()] == []


# ── embeddings ──────────────────────────────────────────────────────────────────────────────


def _embedding_boto3(monkeypatch, answers: list[Exception | list[float]]):
    """A boto3 whose ``invoke_model`` answers each of ``answers`` in turn, and whose credential
    chain resolves."""

    class _Runtime:
        def invoke_model(self, **kwargs: Any) -> dict[str, Any]:
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return {"body": io.BytesIO(json.dumps({"embedding": answer}).encode())}

    class _Session:
        def __init__(self, profile_name=None) -> None:
            pass

        def client(self, service, region_name=None):
            return _Runtime()

        def get_credentials(self):
            return object()

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(Session=_Session))


NO_ACCESS = (
    f"This AWS account has no access to the Bedrock model '{EMBEDDER}' in us-east-1. Request "
    "access to it in the Amazon Bedrock console for that region, or pick a different model in "
    "Settings → Models."
)


def _denied() -> Exception:
    return _client_error(
        "AccessDeniedException",
        "You don't have access to the model with the specified model ID.",
        "InvokeModel",
    )


def test_an_embedding_the_account_cannot_use_says_why(monkeypatch, caplog):
    """🔴 Red before: ``None``, a DEBUG line, and no reason for the re-index to give."""
    _embedding_boto3(monkeypatch, [_denied(), _denied(), _denied()])
    caplog.set_level(logging.DEBUG, logger="bedrock_models")
    adapter = prov.BedrockEmbeddingProvider(region="us-east-1", name="my-bedrock")

    vectors = _run(adapter.embed_batch(["a heron", "a kestrel", "a wren"], model=EMBEDDER))

    assert vectors == [None, None, None]
    assert _run(adapter.unavailable_reason()) == NO_ACCESS
    warned = [
        r.getMessage().split("\n", 1)[0]
        for r in caplog.records
        if r.name == "bedrock_models" and r.levelno == logging.WARNING
    ]
    assert warned == [f"Bedrock embedding on 'my-bedrock' failed: {NO_ACCESS}"], (
        "said once, not once per item"
    )


def test_a_batch_answers_none_for_a_text_it_could_not_embed(monkeypatch):
    """A failed text came back as an empty vector, which core's batch path stores as that text's
    vector. ``None`` is how it keeps the text without one; the rest of the batch is kept."""
    _embedding_boto3(monkeypatch, [[0.1, 0.2], _denied(), [0.3, 0.4], []])
    adapter = prov.BedrockEmbeddingProvider(region="us-east-1", name="my-bedrock")

    vectors = _run(adapter.embed_batch(["a heron", "a kestrel", "a wren", "a rook"], model=EMBEDDER))

    assert vectors == [[0.1, 0.2], None, [0.3, 0.4], None], "an empty embedding is no embedding"


def test_an_embedding_that_works_again_has_nothing_to_say(monkeypatch):
    _embedding_boto3(monkeypatch, [_denied(), [0.1, 0.2, 0.3]])
    adapter = prov.BedrockEmbeddingProvider(region="us-east-1", name="my-bedrock")

    assert _run(adapter.embed("a heron", model=EMBEDDER)) is None
    assert _run(adapter.embed("a heron", model=EMBEDDER)) == [0.1, 0.2, 0.3]
    assert _run(adapter.unavailable_reason()) == ""


def test_an_old_embedding_failure_gives_way_to_what_is_true_now(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(prov, "_time", clock)
    _embedding_boto3(monkeypatch, [_denied()])
    adapter = prov.BedrockEmbeddingProvider(region="us-east-1", name="my-bedrock")
    _run(adapter.embed("a heron", model=EMBEDDER))

    clock.now += prov._CRED_MISSING_TTL + 1

    assert _run(adapter.unavailable_reason()) == "", "the credential chain resolves"


# ── video ───────────────────────────────────────────────────────────────────────────────────
#
# Real boto3 clients with botocore's Stubber in front: every response a test hands a client is
# checked against botocore's own model of the service, so a field the adapter reads that the
# service does not answer fails here. Nothing is sent: botocore's HTTP send fails the test.

#: The folder the adapter asks Nova Reel to write under: the fixture clock names it.
REQUESTED = "s3://clips/bedrock-video/1767225600"
#: The job, and the folder Bedrock made for it under the one asked for.
JOB = "abcdefgh1234"
ARN = f"arn:aws:bedrock:us-east-1:111122223333:async-invoke/{JOB}"
JOB_FOLDER = f"{REQUESTED}/{JOB}"
VIDEO_KEY = f"bedrock-video/1767225600/{JOB}/output.mp4"


@pytest.fixture
def reel(monkeypatch, tmp_path):
    """A video adapter on the bucket ``clips`` whose Bedrock runtime and S3 clients are stubbed,
    with the job's statuses and the download's answers queued by each test. boto3 is dropped from
    ``sys.modules`` afterwards: the provider module is proven to load without it."""
    import botocore.httpsession
    from botocore.stub import Stubber

    for var in [name for name in os.environ if name.startswith("AWS_")]:
        monkeypatch.delenv(var)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "aws-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "aws-credentials"))
    sent: list[str] = []

    def _refuse(self, request):
        sent.append(str(request.url))
        raise AssertionError(f"a request left for {request.url}")

    monkeypatch.setattr(botocore.httpsession.URLLib3Session, "send", _refuse)
    before = {name for name in sys.modules if name.partition(".")[0] == "boto3"}
    import boto3

    session = boto3.session.Session(
        aws_access_key_id="fake", aws_secret_access_key="fake", region_name="us-east-1"
    )
    runtime, s3 = session.client("bedrock-runtime"), session.client("s3")
    stubs = types.SimpleNamespace(runtime=Stubber(runtime), s3=Stubber(s3))
    monkeypatch.setattr(prov.BedrockVideoProvider, "_get_runtime_client", lambda self: runtime)
    monkeypatch.setattr(prov.BedrockVideoProvider, "_get_s3_client", lambda self: s3)
    monkeypatch.setattr(prov, "_time", _Clock())
    stubs.runtime.activate()
    stubs.s3.activate()
    stubs.runtime.add_response(
        "start_async_invoke",
        {"invocationArn": ARN},
        {
            "modelId": REEL,
            "modelInput": ANY,
            "outputDataConfig": {"s3OutputDataConfig": {"s3Uri": REQUESTED}},
        },
    )
    stubs.adapter = prov.BedrockVideoProvider(
        name="my-bedrock", region="us-east-1", s3_bucket="clips"
    )
    yield stubs
    stubs.runtime.deactivate()
    stubs.s3.deactivate()
    for name in [n for n in sys.modules if n.partition(".")[0] == "boto3" and n not in before]:
        del sys.modules[name]
    assert sent == [], f"requests left the process: {sent}"


def _job(status: str, **fields: Any) -> dict[str, Any]:
    """A ``GetAsyncInvoke`` answer, in the shape the service model gives it."""
    return {
        "invocationArn": ARN,
        "modelArn": f"arn:aws:bedrock:us-east-1::foundation-model/{REEL}",
        "status": status,
        "submitTime": datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
        "outputDataConfig": {"s3OutputDataConfig": {"s3Uri": JOB_FOLDER}},
        **fields,
    }


def _polled(reel, *statuses: dict[str, Any]) -> None:
    for status in statuses:
        reel.runtime.add_response("get_async_invoke", status, {"invocationArn": ARN})


def _generated(reel):
    return _run(reel.adapter.generate("a heron at dawn", model=REEL))


def test_what_is_read_of_a_job_is_what_the_service_model_says_it_answers(tmp_path, monkeypatch):
    """The fields read of a job's status are the ones botocore's own model of the Bedrock
    runtime names — its status, its failure and the folder it writes into — read from the
    installed botocore, so a model that moved one fails here and not in someone's video."""
    from botocore.session import get_session

    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "aws-config"))
    job = get_session().get_service_model("bedrock-runtime").operation_model("GetAsyncInvoke")
    out = job.output_shape

    assert set(out.members["status"].enum) == {"InProgress", "Completed", "Failed"}
    assert "failureMessage" in out.members
    folder = out.members["outputDataConfig"].members["s3OutputDataConfig"]
    assert "s3Uri" in folder.required_members and "outputDataConfig" in out.required_members


def test_a_finished_video_is_downloaded_from_the_folder_its_job_names(reel):
    """🔴 Red before: the video was looked for at ``<prefix>/output.mp4``, a path built here, and
    Bedrock writes each job's video into a folder of its own, named for the job."""
    _polled(reel, _job("InProgress"), _job("Completed"))
    reel.s3.add_response(
        "head_object", {"ContentLength": 3, "ETag": '"v1"'}, {"Bucket": "clips", "Key": VIDEO_KEY}
    )
    reel.s3.add_response(
        "get_object", {"Body": StreamingBody(io.BytesIO(b"mp4"), 3), "ContentLength": 3}
    )

    [video] = _generated(reel)

    try:
        with open(video.local_path, "rb") as fh:
            assert fh.read() == b"mp4"
    finally:
        os.unlink(video.local_path)
    reel.runtime.assert_no_pending_responses()
    reel.s3.assert_no_pending_responses()


def test_a_finished_job_whose_video_is_missing_says_where_to_look(reel):
    _polled(reel, _job("Completed"))
    reel.s3.add_client_error(
        "head_object",
        service_error_code="404",
        service_message="Not Found",
        http_status_code=404,
        expected_params={"Bucket": "clips", "Key": VIDEO_KEY},
    )

    with pytest.raises(VideoGenError) as failed:
        _generated(reel)

    assert str(failed.value) == (
        f"Nova Reel said this video was finished, but there is no video at s3://clips/{VIDEO_KEY}. "
        f"Look in the job's folder, {JOB_FOLDER}/, in the S3 console; if it isn't there, generate "
        "the video again. Details: An error occurred (404) when calling the HeadObject operation: "
        "Not Found"
    )


def test_a_finished_video_that_may_not_be_read_names_the_action(reel):
    _polled(reel, _job("Completed"))
    reel.s3.add_client_error(
        "head_object",
        service_error_code="403",
        service_message="Forbidden",
        http_status_code=403,
        expected_params={"Bucket": "clips", "Key": VIDEO_KEY},
    )

    with pytest.raises(VideoGenError) as failed:
        _generated(reel)

    assert str(failed.value).startswith(
        "Nova Reel finished this video, but the identity Bedrock signs in as may not read "
        f"s3://clips/{VIDEO_KEY} (s3:GetObject). Add that action to its IAM policy, or get the "
        "video from there in the S3 console. Details: "
    ), str(failed.value)


@pytest.mark.parametrize(
    ("reason", "said"),
    [
        (
            "The generated content has been blocked by our content filters.",
            "Nova Reel's content filters blocked the video it made for this prompt. Reword the "
            "prompt, then try again.",
        ),
        (
            "Service capacity limit has been reached. Please try again later.",
            "Nova Reel has reached its capacity limit for now. Wait a few minutes, then try again.",
        ),
        (
            "Something went wrong on the server side.",
            "Something went wrong on AWS's side while Nova Reel made this video. Try again in a "
            "few minutes.",
        ),
        (
            "Request has been aborted.",
            "The Nova Reel job for this video was stopped before it finished. Try again.",
        ),
        (
            "Unrecognized failure from the fixture.",
            "Nova Reel couldn't generate this video. Fix what its reason names, then try again.",
        ),
    ],
)
def test_a_nova_reel_job_that_failed_says_what_to_do(reel, reason, said):
    """🔴 Red before: "Bedrock video generation failed: <AWS's words>", and no next step. The
    reasons are the ones AWS documents for a failed job."""
    _polled(reel, _job("Failed", failureMessage=reason))

    with pytest.raises(VideoGenError) as failed:
        _generated(reel)

    assert str(failed.value) == f"{said} Details: {reason}"
    reel.s3.assert_no_pending_responses()


def test_a_nova_reel_job_that_gives_no_reason_says_so(reel):
    _polled(reel, _job("Failed"))

    with pytest.raises(VideoGenError) as failed:
        _generated(reel)

    assert str(failed.value) == (
        "Nova Reel couldn't generate this video, and gave no reason. Try again."
    )


def test_a_nova_reel_job_that_runs_long_says_where_its_video_will_be(reel):
    """🔴 Red before: "timed out after 600s", for a job that runs on in AWS, writes its video and
    is billed for it."""
    polls = prov._VIDEO_POLL_TIMEOUT // prov._VIDEO_POLL_INTERVAL
    _polled(reel, *[_job("InProgress") for _ in range(polls)])

    with pytest.raises(VideoGenError) as failed:
        _generated(reel)

    assert str(failed.value) == (
        "Nova Reel hadn't finished this video after 10 minutes, so video generation stopped "
        f"waiting for it. The job may still finish and write the video to {JOB_FOLDER}/output.mp4: "
        "look there before you try again."
    )
    reel.runtime.assert_no_pending_responses()


def test_a_job_whose_status_names_no_folder_says_where_to_look():
    """Outside the service model's shape, which always names the folder; a status that did not
    would otherwise send the download nowhere."""
    adapter = prov.BedrockVideoProvider(name="my-bedrock", region="us-east-1", s3_bucket="clips")

    with pytest.raises(VideoGenError) as failed:
        adapter._download("", requested=REQUESTED)

    assert str(failed.value) == (
        "Nova Reel finished this video, but its status didn't say where it wrote it. Look for it "
        f"in a folder under {REQUESTED}/ in the S3 console."
    )
