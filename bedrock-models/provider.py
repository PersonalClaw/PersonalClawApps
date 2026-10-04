"""Amazon Bedrock provider — Converse streaming via ``boto3``.

A ModelProvider (stateless inference) sibling of :mod:`personalclaw.llm.openai`,
backed by the ``bedrock-runtime`` Converse API. ``boto3`` is imported lazily
inside :meth:`BedrockProvider.start` (Property 11 / Provider SDK Lazy Import) so
this module is safe to import without ``boto3`` installed; only starting a
provider triggers the SDK import.

Authentication is delegated entirely to boto3's standard AWS credential chain
(environment, ``~/.aws`` profiles, SSO cache, container/instance metadata) and
SigV4 signing happens inside botocore. PersonalClaw's ``CredentialStore`` is NOT
used — Bedrock takes only a region, a model, and an optional AWS profile name;
no AWS secret is ever read into or persisted by PersonalClaw.

``converse_stream`` is synchronous and returns a blocking ``EventStream``. To
avoid stalling the aiohttp event loop, the blocking call + iteration run in a
worker thread (:func:`asyncio.to_thread`) that pushes deltas onto an
:class:`asyncio.Queue` the async generator drains — token streaming stays
non-blocking under a multi-session gateway.
"""

import asyncio
import base64
import binascii
import json
import logging
import os
import re
from collections.abc import AsyncIterator, Callable
from typing import Any, NamedTuple, NoReturn, TypeVar
from urllib.parse import urlsplit

from personalclaw.sdk.model import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    CancelOutcome,
    LLMEvent,
    ModelProvider,
    closing_stream,
    until_terminal,
)
from personalclaw.sdk.model import CACHE_HINT_KEY, Capability, PromptCache, ProviderCapability
from personalclaw.sdk.model import VOLATILE_KEY
from personalclaw.sdk.model import (
    ModelCatalog,
    ModelDiscoveryError,
    ModelInfo,
    ProviderEntry,
    ProviderResolutionError,
    get_default_registry,
    output_cap,
    own_model,
    per_call_temperature,
    require_model,
)
from personalclaw.sdk.net import sentence_with_detail

#: Named, not ``__name__``: core loads this module under a private name, and a line logged under
#: it would carry that name. Which lines reach the gateway log does not depend on it: core
#: recognises an app's lines by the code that logs them.
logger = logging.getLogger("bedrock_models")

# NO model id is chosen here, hardcoded or discovered. A call names its model: the chat binding
# in Settings → Models (e.g. ``Bedrock:global.anthropic.claude-opus-4-8``), else the instance's
# own Default Model (the SDK's ``own_model``). One that names neither is refused before it is
# sent (``require_model``). This used to pick a Claude from live discovery at ``start()``, so an
# unbound call on an instance saved without a Default Model answered on a model nobody chose.
#: The one region an instance that names none uses: for chat, every media call and the model
#: list alike. Chat and the media calls used us-west-2 while the model list came from us-east-1,
#: so a listed model could be one the call's region does not serve. us-east-1 is the region the
#: Add-instance form fills in, and Bedrock serves the most models there, Nova Reel among them.
DEFAULT_REGION = "us-east-1"

# Max conversation history entries before trimming oldest (mirrors openai.py).
_MAX_HISTORY = 50

# Fallback context window when the model is absent from ``model_tokens.json``.
# Claude-on-Bedrock is 200k; Nova is 300k. Pick the conservative Claude value
# so the percentage estimate skews high rather than hiding usage.
_DEFAULT_CONTEXT_WINDOW = 200_000

# Default output cap when none is configured. Bedrock Converse applies a LOW
# per-model default (historically 512 for Anthropic) when ``maxTokens`` is
# omitted — that silently TRUNCATES the streamed tool-call JSON of any large
# tool argument (e.g. a write_file with a big ``content``), so the accumulated
# arguments are incomplete JSON and parse to ``{}`` ("missing required
# argument"). The sibling Anthropic provider always sends a default (4096), so
# mirror that here: a generous cap large enough for substantial file writes.
_DEFAULT_MAX_TOKENS = 8192

# ── Prompt caching: Bedrock's OWN wire form ───────────────────────────────────
#
# THIS APP IS THE ONLY PLACE ``cachePoint`` MAY BE NAMED. Core emits the NEUTRAL
# ``CACHE_HINT_KEY`` marker on exactly one message (``personalclaw/llm/prompt_cache.py``)
# and deliberately never learns Converse's syntax — core's own rails sweep
# (``tests/test_prompt_cache_wire_translation.py``) FAILS the build if ``cachePoint``
# appears in any core module outside the Anthropic adapter. The translation therefore
# lives here, beside the client that speaks the wire.
#
# Shape per the Converse API reference ("Using cachePoint", Amazon Bedrock User Guide):
# a cache checkpoint is a CONTENT BLOCK appended to a message's ``content`` array (and,
# equivalently, to the ``system`` block list) — everything BEFORE the block is cached.
# ``"default"`` is the only checkpoint type Converse defines.
_CACHE_POINT_BLOCK: dict[str, dict[str, str]] = {"cachePoint": {"type": "default"}}

# Converse reports caching in ``metadata.usage`` under these two keys; they map onto
# ``LLMEvent.cache_read_tokens`` / ``.cache_creation_tokens``, the fields the cost +
# savings surfaces already read for Anthropic. Without them an EXPLICIT posture is
# unobservable: the marker would ship and nothing would ever report a hit.
_CACHE_READ_KEY = "cacheReadInputTokens"
_CACHE_WRITE_KEY = "cacheWriteInputTokens"

# Sentinel pushed onto the bridge queue when the worker thread finishes.
_STREAM_DONE = object()

#: Whose stream this provider reads, and the event its answer ends with, as a cut-off names them.
_ADAPTER = "Bedrock"
_MESSAGE_STOP = "messageStop"


async def _drained(
    queue: "asyncio.Queue[Any]", fail: Callable[[Exception], NoReturn]
) -> AsyncIterator[tuple[str, Any]]:
    """What the worker thread puts on *queue*, an item each, until it is done. An error the
    thread caught is raised here, as *fail* says it: a call that failed is that failure, never an
    answer cut off."""
    while True:
        item = await queue.get()
        if item is _STREAM_DONE:
            return
        kind, payload = item
        if kind == "error":
            fail(payload)
        yield item


def _answer_ended(item: tuple[str, Any]) -> bool:
    """Whether *item* is the stream's ``messageStop``, which ends the answer: a stream that ends
    before it was cut off (``until_terminal``), whatever text had arrived."""
    return item[0] == "stop"

# Streaming-read timeout (seconds). botocore's default is 60s applied PER socket
# read — and during a ``converse_stream`` that fires on the GAP BETWEEN streamed
# events, not just time-to-first-byte. Reasoning-class models (Opus) routinely go
# quiet far longer than 60s mid-turn (extended internal reasoning, or a slow tool
# round-trip the model is waiting on) while the turn is perfectly healthy, so the
# default silently kills long-but-live turns with a bare ``Read timed out``. Give
# the stream generous headroom; the supervisor/watchdog owns true stall recovery.
# (Repro: a heavy SPEC-writing loop turn streamed 66 chunks then died at ~82s on
# an inter-chunk gap; short turns never tripped it.) connect stays tight.
_STREAM_READ_TIMEOUT = 600
_CONNECT_TIMEOUT = 15


def _bare_model_id(model: str | None, fallback: str) -> str:
    """Return a clean AWS Bedrock model id from a possibly provider-qualified ref.

    Upstream callers may hand the model as the ``active_models.json`` ref form
    (``"Bedrock:global.anthropic.claude-opus-4-8"`` — provider name + ':' +
    bare id). Bedrock model ids themselves contain colons (``…-v1:0``), so only
    strip a SINGLE leading segment, and only when what follows still looks like
    a Bedrock id (contains a vendor '.' namespace, e.g. ``anthropic.``/
    ``amazon.``/``global.``/``us.``). Defense-in-depth at the AWS boundary so no
    upstream path can send an invalid identifier. Empty → fallback.
    """
    mid = (model or "").strip() or fallback
    if ":" in mid:
        head, rest = mid.split(":", 1)
        # Strip only an obvious "<Provider>:" prefix — the remainder must look
        # like a Bedrock model id (has a dotted vendor namespace) so we never
        # mangle a real id whose first colon is part of the version (…-v1:0).
        if "." in rest.split(":", 1)[0]:
            mid = rest
    return mid


#: The IAM action an ``AccessDeniedException`` names when the identity's policy lacks it:
#: "... is not authorized to perform: bedrock:InvokeModelWithResponseStream on resource: ...".
_NOT_AUTHORIZED_RE = re.compile(r"not authorized to perform:\s*(?P<action>[\w:*-]+)", re.IGNORECASE)
#: The codes AWS answers with when it does not accept the credentials themselves.
_REJECTED_CREDENTIAL_CODES = frozenset(
    {"UnrecognizedClientException", "ExpiredTokenException", "ExpiredToken", "InvalidClientTokenId"}
)
#: The calls boto3 makes to turn a profile's IAM role into credentials. A refusal of one is about
#: the profile's role, never about a Bedrock model, so it must not read as access to a model.
_ROLE_OPERATIONS = frozenset({"AssumeRole", "AssumeRoleWithWebIdentity", "AssumeRoleWithSAML"})

#: Where an instance's AWS Region and AWS Profile are set. AWS Profile sits under the form's
#: Advanced disclosure, so a step that names it says so.
_ON_INSTANCE = "on this Amazon Bedrock instance in Settings → Providers"
_SET_PROFILE = f"set AWS Profile {_ON_INSTANCE} (under Advanced)"
#: How a sentence ends when nothing here recognised the failure: the rest of it is in the gateway
#: log, where :func:`_warn_once` puts the traceback, at DEBUG. A sentence that names the cause and
#: the fix never says it.
_SEE_THE_LOG = "Try again; if it keeps failing, check the gateway log."


def _aws_error_code(error: Exception) -> str:
    """The AWS error code of a botocore ``ClientError`` (``AccessDeniedException``), else ""."""
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        code = str((response.get("Error") or {}).get("Code") or "")
        if code:
            return code
    found = re.search(r"\((\w+)\) when calling", str(error))
    return found.group(1) if found else ""


def _aws_operation(error: Exception) -> str:
    """The AWS operation a botocore ``ClientError`` answered (``AssumeRole``), else ""."""
    name = getattr(error, "operation_name", None)
    if isinstance(name, str) and name:
        return name
    found = re.search(r"when calling the (\w+) operation", str(error))
    return found.group(1) if found else ""


def _botocore(error: BaseException, *names: str) -> bool:
    """Whether ``error`` is one of botocore's exceptions ``names``.

    Told by class name and module along its MRO, because this module never imports botocore: it
    must load without the SDK installed."""
    return any(
        cls.__name__ in names and cls.__module__.partition(".")[0] == "botocore"
        for cls in type(error).__mro__
    )


def _from_credential_command(error: BaseException) -> bool:
    """Whether ``error`` came from running a profile's ``credential_process`` command.

    botocore wraps a command that exits non-zero in its own error, but lets one that prints
    something other than JSON, or that cannot be started, through as the ``ValueError`` or
    ``OSError`` it is. For those the frame it was raised in is what says where it came from."""
    if _botocore(error, "CredentialRetrievalError"):
        return (getattr(error, "kwargs", None) or {}).get("provider") == "custom-process"
    if not isinstance(error, (ValueError, OSError)):
        return False
    frame = error.__traceback__
    while frame is not None:
        if frame.tb_frame.f_code.co_name == "_retrieve_credentials_using":
            return True
        frame = frame.tb_next
    return False


def _profile_in_use(profile: str | None) -> str:
    """The AWS profile boto3 signs in with: the instance's AWS Profile, else the one the
    environment names (botocore reads ``AWS_DEFAULT_PROFILE``, then ``AWS_PROFILE``). ""
    when neither names one, and boto3 walks the default credential chain."""
    return (
        profile
        or os.environ.get("AWS_DEFAULT_PROFILE", "")
        or os.environ.get("AWS_PROFILE", "")
    )


def _no_credentials(profile: str | None) -> str:
    """What is said when the AWS credential chain found no credentials at all: none for the
    profile boto3 signs in with, or none anywhere in the default chain when no profile is named."""
    in_use = _profile_in_use(profile)
    flag = f" --profile {in_use}" if in_use else ""
    found = (
        f"No AWS credentials were found for the AWS profile '{in_use}'."
        if in_use
        else "No AWS credentials were found: this Amazon Bedrock instance names no AWS "
        "profile, and the default credential chain has none."
    )
    return (
        f"{found} Sign in with your AWS tool (for example `aws sso login{flag}`, or "
        f"`aws configure{flag}` to enter access keys), then try again, or {_SET_PROFILE} to a "
        "profile that has credentials."
    )


# The AWS files are named by their folder, ~/.aws, never by a path into it: the Store's install
# scanner reads a credential file's path in an app's code as the app reading that file, and
# says so on the install's consent.
def _aws_setup_problem(error: BaseException, *, region: str, profile: str | None) -> str | None:
    """What is missing or wrong in the AWS setup Bedrock signs in with, and what to do about it.

    PersonalClaw stores no AWS key: boto3 signs every call with the AWS credential chain, and
    when that chain cannot produce credentials botocore says so in its own words ("Unable to
    locate credentials", or whatever a profile's credential command printed). Those name nothing
    a user can change here, so each case says what is missing and the next step: sign in with the
    AWS tool, fix the profile or the region, or choose another profile. The same sentence serves
    the chat, the connection test and the Models page. ``None`` when ``error`` is not about the
    setup."""
    in_use = _profile_in_use(profile)
    whose = f"the AWS profile '{in_use}'" if in_use else "your default AWS profile"
    flag = f" --profile {in_use}" if in_use else ""
    code = _aws_error_code(error)
    if _from_credential_command(error):
        sentence = (
            f"The command {whose} runs to get its AWS credentials (its credential_process) "
            "failed. Run that command in a terminal to see why, fix it or sign in again, then try "
            f"again, or {_SET_PROFILE} to a different profile."
        )
    elif _botocore(error, "NoCredentialsError"):
        sentence = _no_credentials(profile)
    elif _botocore(error, "PartialCredentialsError"):
        sentence = (
            "Only part of a set of AWS credentials was found, so Amazon Bedrock can't sign in "
            "with them. Complete or remove that set with your AWS tool, then try again, or "
            f"{_SET_PROFILE} to a profile that has credentials."
        )
    elif _botocore(error, "ProfileNotFound"):
        missing = (getattr(error, "kwargs", None) or {}).get("profile") or in_use
        sentence = (
            f"The AWS profile '{missing}' isn't in your AWS config (the config and credentials "
            f"files in ~/.aws). Add it with your AWS tool (for example `aws configure "
            f"--profile {missing}`), then try again, or {_SET_PROFILE} to a profile you have."
        )
    elif _botocore(error, "SSOError", "TokenRetrievalError"):
        sentence = (
            f"The AWS SSO sign-in for {whose} has expired or hasn't been made. Sign in with "
            f"`aws sso login{flag}`, then try again."
        )
    elif _botocore(error, "LoginError"):
        sentence = (
            f"The AWS sign-in for {whose} can't be used. Sign in again with `aws login{flag}`, "
            "then try again."
        )
    elif _botocore(error, "RefreshWithMFAUnsupportedError"):
        sentence = (
            f"{whose[0].upper()}{whose[1:]} needs an MFA code to refresh its credentials, and "
            "Amazon Bedrock can't ask you for one. Refresh them with your AWS tool, then try "
            f"again, or {_SET_PROFILE} to a profile that doesn't need MFA."
        )
    elif _botocore(error, "InvalidConfigError", "ConfigParseError"):
        sentence = (
            f"The AWS configuration of {whose} can't be used. Correct it in the config file in "
            f"~/.aws, then try again, or {_SET_PROFILE} to a different profile."
        )
    elif _botocore(error, "InvalidRegionError"):
        named = (getattr(error, "kwargs", None) or {}).get("region_name") or region
        sentence = (
            f"'{named}' isn't an AWS region name. Set AWS Region {_ON_INSTANCE} to one like "
            "us-east-1, then try again."
        )
    elif _botocore(error, "EndpointConnectionError", "ConnectTimeoutError"):
        url = str((getattr(error, "kwargs", None) or {}).get("endpoint_url") or "")
        host = urlsplit(url).hostname or url or "its endpoint"
        sentence = (
            f"No connection could be made to AWS at {host}. Check that this machine is online "
            f"and that {region} is a region Amazon Bedrock runs in (AWS Region {_ON_INSTANCE}), "
            "then try again."
        )
    elif _aws_operation(error) in _ROLE_OPERATIONS and code not in _REJECTED_CREDENTIAL_CODES:
        sentence = (
            f"AWS refused to let {whose} assume its IAM role ({code}). Check that the "
            "role exists and lets the identity the profile starts from assume it, then try "
            f"again, or {_SET_PROFILE} to a different profile."
        )
    else:
        return None
    return sentence_with_detail(sentence, error)


def _friendly_bedrock_error(
    error: Exception, model_id: str, *, region: str = "", profile: str | None = None
) -> Exception:
    """Map an opaque botocore Bedrock error to the sentence that names its fix.

    Returned as the SDK's ``ProviderResolutionError``, which the chat shows as written: the fix
    for each of these is outside PersonalClaw, and only this app knows where.

    * The AWS setup Bedrock signs in with, when that is what failed: no credentials, a failing
      credential command, an expired sign-in, a missing profile, a bad region, no connection
      (:func:`_aws_setup_problem`).
    * A model whose mandatory data-retention policy isn't enabled for this account
      (``ValidationException: data retention mode 'default' is not available for this model``).
    * ``AccessDeniedException`` naming an IAM action: the identity's policy lacks it, so the fix is
      that action in the policy, not a new key.
    * ``AccessDeniedException`` without one: the account has no access to that model in the
      region (model access is granted per account and region).
    * The credentials themselves turned down (an invalid or expired security token): sign in to
      AWS again.
    * A model the region does not serve, or one it serves only through an inference profile
      (``ValidationException``: "The provided model identifier is invalid", "… with on-demand
      throughput isn't supported"): choose a model the instance's list offers, AWS's words after.

    Everything else passes through unchanged.
    """
    setup = _aws_setup_problem(error, region=region or DEFAULT_REGION, profile=profile)
    if setup is not None:
        return ProviderResolutionError(setup)
    msg = str(error)
    if "data retention" in msg and "not available for this model" in msg:
        return ProviderResolutionError(
            f"The Bedrock model '{model_id}' can't be used from this AWS account: "
            f"it requires a data-retention policy that isn't enabled here. "
            f"Choose a different Bedrock model in Settings → Models (most models "
            f"work with no extra setup)."
        )
    code = _aws_error_code(error)
    where = f" in {region}" if region else ""
    if code == "ValidationException" and "model identifier is invalid" in msg:
        return ProviderResolutionError(
            sentence_with_detail(
                f"Amazon Bedrock{where} has no model '{model_id}' this AWS account can call. "
                "Choose one of the models Settings → Models lists for this Amazon Bedrock "
                f"instance, or set AWS Region {_ON_INSTANCE} to a region that serves it.",
                error,
            )
        )
    if code == "ValidationException" and "on-demand throughput" in msg:
        return ProviderResolutionError(
            sentence_with_detail(
                f"Amazon Bedrock{where} serves '{model_id}' only through an inference profile. "
                "Choose the inference profile Settings → Models lists for it (its id starts with a "
                "geography, such as us. or global.).",
                error,
            )
        )
    if code == "AccessDeniedException":
        named = _NOT_AUTHORIZED_RE.search(msg)
        if named:
            return ProviderResolutionError(
                f"Your AWS credentials aren't allowed to call {named['action']} on the Bedrock "
                f"model '{model_id}'{where}. Add that action to the IAM policy of the identity "
                "Bedrock signs in as, or pick a different model in Settings → Models."
            )
        return ProviderResolutionError(
            f"This AWS account has no access to the Bedrock model '{model_id}'{where}. Request "
            "access to it in the Amazon Bedrock console for that region, or pick a different "
            "model in Settings → Models."
        )
    if code in _REJECTED_CREDENTIAL_CODES or "security token included in the request is" in msg:
        return ProviderResolutionError(
            "AWS turned down the credentials Bedrock used: their security token is invalid or "
            "has expired. Sign in to AWS again (`aws sso login` for an SSO profile), or pick a "
            "different model in Settings → Models."
        )
    return error


def _raise_friendly(
    error: Exception, model_id: str, *, region: str, profile: str | None
) -> NoReturn:
    """Raise :func:`_friendly_bedrock_error`'s sentence for ``error``, chained to it so the
    gateway log keeps the SDK's traceback — or ``error`` itself when nothing here maps it."""
    friendly = _friendly_bedrock_error(error, model_id, region=region, profile=profile)
    if friendly is error:
        raise error
    raise friendly from error


# Model → context window tokens (shared JSON, same file openai.py/anthropic.py read).
from personalclaw.sdk.model import model_context_window as _model_window


# ── OpenAI-shape → Bedrock Converse translation ───────────────────────────
#
# The native loop sends one uniform (OpenAI-shaped) message + tool format
# across all ModelProviders; each adapts. Converse uses its own envelope:
# the system prompt is a top-level ``system`` list, tool calls are ``toolUse``
# content blocks, tool results are ``toolResult`` blocks in a user turn, and
# tool schemas live under ``toolConfig.tools[].toolSpec``. These helpers map
# the loop's shapes so :meth:`BedrockProvider.complete` accepts them unchanged.


# Bedrock Converse constrains ``toolSpec.name`` to ``[a-zA-Z0-9_-]+`` (≤64 chars).
# MCP tools are namespaced with slashes (e.g. ``mcp/github/search_issues``),
# which Bedrock rejects. We sanitize names before sending them (in BOTH the
# toolConfig and the history ``toolUse`` blocks, which must agree) and reverse-map
# the name Bedrock returns back to the real tool id before the loop dispatches it.
_TOOL_NAME_ILLEGAL_RE = re.compile(r"[^a-zA-Z0-9_-]")
_TOOL_NAME_MAX = 64


def _sanitize_tool_name(name: object) -> str:
    """Coerce a tool name into Bedrock's ``[a-zA-Z0-9_-]+`` (≤64) constraint.

    ``name`` is coerced to ``str`` first: a malformed tool schema can deliver a
    non-string here (observed: a dict), and ``re.sub`` on a non-str raises
    "expected string or bytes-like object, got 'dict'" — crashing the whole turn
    before any tool runs. Defensive str() keeps a garbled name from breaking the
    stream; valid string names are unaffected."""
    safe = _TOOL_NAME_ILLEGAL_RE.sub("_", str(name) if name else "")[:_TOOL_NAME_MAX]
    return safe or "tool"


def _build_tool_name_maps(tools: list[dict] | None) -> tuple[dict[str, str], dict[str, str]]:
    """Return ``(forward, reverse)`` maps between real tool names and the
    Bedrock-safe names. Collisions after sanitizing are disambiguated with a
    numeric suffix so the forward map stays 1:1 (and thus reversible)."""
    forward: dict[str, str] = {}
    reverse: dict[str, str] = {}
    for tool in tools or []:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(fn, dict):
            continue
        name = fn.get("name", "") or ""
        if not name or name in forward:
            continue
        safe = _sanitize_tool_name(name)
        if safe in reverse:  # collision — keep names distinct
            i = 2
            base = safe[: _TOOL_NAME_MAX - 3]
            while f"{base}_{i}" in reverse:
                i += 1
            safe = f"{base}_{i}"
        forward[name] = safe
        reverse[safe] = name
    return forward, reverse


def _translate_tools(tools: list[dict], name_map: dict[str, str]) -> dict:
    """Map OpenAI ``tools`` to a Converse ``toolConfig`` dict.

    Each OpenAI entry ``{"type": "function", "function": {name, description,
    parameters}}`` becomes ``{"toolSpec": {name, description,
    inputSchema: {"json": parameters}}}``. ``name_map`` (real→Bedrock-safe) is
    applied so namespaced MCP names satisfy Bedrock's name constraint.
    """
    specs: list[dict] = []
    for tool in tools:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(fn, dict):
            continue
        raw_name = fn.get("name", "") or ""
        specs.append(
            {
                "toolSpec": {
                    "name": name_map.get(raw_name, _sanitize_tool_name(raw_name)),
                    "description": fn.get("description", "") or "",
                    "inputSchema": {
                        "json": fn.get("parameters")
                        or {"type": "object", "properties": {}}
                    },
                }
            }
        )
    return {"tools": specs}


def _translate_messages(
    messages: list[dict], name_map: dict[str, str] | None = None
) -> tuple[list[dict], list[dict]]:
    """Split OpenAI-shaped ``messages`` into ``(system_blocks, converse_messages)``.

    * ``role: "system"`` → entries in the returned ``system`` block list.
    * ``role: "assistant"`` with ``tool_calls`` → content blocks mixing an
      optional ``{"text": ...}`` block and one ``{"toolUse": {...}}`` per call.
    * ``role: "tool"`` → a ``{"toolResult": {...}}`` block; consecutive tool
      results merge into a single user turn (Converse groups them).
    * Plain ``user``/``assistant`` strings → ``{role, content: [{"text": ...}]}``.

    ``name_map`` (real→Bedrock-safe tool name) is applied to historical
    ``toolUse`` blocks so a replayed assistant turn names tools exactly as the
    toolConfig does (Bedrock rejects a toolUse whose name is not in the config).

    Prompt caching (PCS-8) — this is where the neutral marker becomes Converse syntax:

    * a message carrying :data:`CACHE_HINT_KEY` is the trailing boundary of the
      cacheable span, and Converse marks a boundary with a ``cachePoint`` CONTENT
      BLOCK, so the checkpoint is appended AFTER that message's blocks. A hinted
      ``system`` message puts the checkpoint at the end of the ``system`` block list
      (Converse serves ``system`` first, so that caches the whole stable head).
    * a hint on an ABSENT or EMPTY span is a NO-OP — there is no block to follow.
    * a hint on a ``role: "tool"`` message is likewise a NO-OP. Core never places one
      there (``mark_cacheable_prefix`` skips tool results when picking the boundary),
      and a checkpoint wedged into a merged toolResult turn would split the group
      Converse requires to stay contiguous. Degrading to "no checkpoint" costs a cache
      hit; splitting the group would cost the whole request.
    * the per-turn VOLATILE note (core's ``role: "system"`` message carrying the SDK's
      ``VOLATILE_KEY``, whose content changes every turn) is NOT hoisted into ``system``:
      Converse serves ``system`` ahead of ``messages[0]``, so the cacheable prefix would
      differ on every turn and no checkpoint could ever be read. Converse has no system
      turn inside the conversation either, so the note is one text block appended to the
      request's LAST user turn — after its ``toolResult`` blocks and after any
      ``cachePoint`` — as core sends it (fenced as the runtime's). Never a user turn of
      its own: after a tool result, that turn was the newest thing the user had "said",
      and the model answered the tool catalog in the chat. Multiple notes ship once each,
      in order, and a request with no user turn gets one carrying only the notes.
    """
    name_map = name_map or {}
    system_blocks: list[dict] = []
    out: list[dict] = []
    volatile_notes: list[object] = []

    # Converse rejects the whole request when ANY text block is empty OR
    # whitespace-only ("text content blocks must contain non-whitespace text") —
    # and empty texts legitimately occur upstream (a tool that printed nothing, a
    # bare assistant turn around tool calls). Substitute a visible placeholder.
    def _text(v: object) -> dict:
        s = "" if v is None else str(v)
        return {"text": s if s.strip() else "(empty)"}

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        hinted = CACHE_HINT_KEY in msg

        if role == "system":
            if msg.get(VOLATILE_KEY):
                if content:
                    volatile_notes.append(content)
                continue
            if content:
                system_blocks.append({"text": str(content)})
                if hinted:
                    system_blocks.append(dict(_CACHE_POINT_BLOCK))
            continue

        if role == "tool":
            block = {
                "toolResult": {
                    "toolUseId": str(msg.get("tool_call_id", "") or ""),
                    "content": [_text(content)],
                }
            }
            if (
                out
                and out[-1].get("role") == "user"
                and isinstance(out[-1].get("content"), list)
                and all("toolResult" in b for b in out[-1]["content"])
            ):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
            continue

        if role == "assistant" and msg.get("tool_calls"):
            blocks: list[dict] = []
            if content:
                blocks.append({"text": str(content)})
            for call in msg["tool_calls"]:
                fn = call.get("function", {}) if isinstance(call, dict) else {}
                raw_args = fn.get("arguments", "") or ""
                try:
                    parsed = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except (json.JSONDecodeError, ValueError):
                    parsed = {}
                if not isinstance(parsed, dict):
                    parsed = {}
                raw_name = fn.get("name", "") or ""
                blocks.append(
                    {
                        "toolUse": {
                            "toolUseId": str(call.get("id", "") or ""),
                            "name": name_map.get(raw_name, _sanitize_tool_name(raw_name)),
                            "input": parsed,
                        }
                    }
                )
            if hinted:
                blocks.append(dict(_CACHE_POINT_BLOCK))
            out.append({"role": "assistant", "content": blocks})
            continue

        # A list content is the multimodal content-block shape (text + images), used by
        # vision use-cases. Translate each block to a Converse block; a plain string
        # (the common case) stays a single text block.
        if isinstance(content, list):
            out.append({"role": role, "content": _content_blocks_to_converse(content)})
        else:
            out.append({"role": role, "content": [_text(content)]})
        if hinted:
            out[-1]["content"].append(dict(_CACHE_POINT_BLOCK))

    # The volatile notes end the request, on its last user turn — laid after the tool-pair
    # repair, which can add that turn (a synthetic result for an interrupted call).
    out = _repair_tool_pairs(out)
    notes = [_text(note) for note in volatile_notes]
    if notes:
        last_user = next((m for m in reversed(out) if m.get("role") == "user"), None)
        if last_user is None:
            out.append({"role": "user", "content": notes})
        else:
            last_user["content"] = [*last_user["content"], *notes]
    return system_blocks, out


def _read_cache_usage(usage: dict) -> tuple[int, int]:
    """``(cache_creation_tokens, cache_read_tokens)`` from a Converse ``metadata.usage``.

    Converse reports cache WRITES and READS in their own fields and EXCLUDES both from
    ``inputTokens`` once caching engages (Bedrock User Guide: "the ``inputTokens`` field
    represents only the non-cached input tokens"), so these two keys are the ONLY place a
    hit is observable. Producer for :class:`LLMEvent`'s ``cache_creation_tokens`` /
    ``cache_read_tokens`` — the same fields core's Anthropic adapter fills, so the cost
    and savings surfaces need no Bedrock-specific reader.

    An uncached turn simply omits the keys; missing or non-numeric values read as 0
    rather than raising, because a usage-parsing failure must never fail the turn.
    """

    def _int(key: str) -> int:
        try:
            return max(0, int(usage.get(key, 0) or 0))
        except (TypeError, ValueError):
            return 0

    return _int(_CACHE_WRITE_KEY), _int(_CACHE_READ_KEY)


def _content_blocks_to_converse(blocks: list) -> list[dict]:
    """Translate OpenAI-style multimodal content blocks into Converse content blocks.

    Handles the two shapes the knowledge vision nodes emit (see pipeline/nodes/_llm.py):
    ``{"type": "text", "text": ...}`` → ``{"text": ...}`` and
    ``{"type": "image_url", "image_url": {"url": "data:<mime>;base64,<...>"}}`` →
    ``{"image": {"format": <fmt>, "source": {"bytes": <raw>}}}``. Bytes are decoded
    from the data URL; a block we can't parse is dropped rather than corrupting the turn.
    Always returns at least one block (Converse rejects empty content)."""
    out: list[dict] = []
    for b in blocks:
        if not isinstance(b, dict):
            if b:
                out.append({"text": str(b)})
            continue
        btype = b.get("type")
        if btype == "text":
            t = str(b.get("text", ""))
            out.append({"text": t if t.strip() else "(empty)"})
        elif btype == "image_url":
            url = ((b.get("image_url") or {}) if isinstance(b.get("image_url"), dict) else {}).get("url", "")
            img = _data_url_to_converse_image(url)
            if img:
                out.append(img)
    return out or [{"text": "(empty)"}]


def _data_url_to_converse_image(url: str) -> dict | None:
    """``data:image/jpeg;base64,<b64>`` → a Converse ``{"image": {...}}`` block, or None."""
    if not isinstance(url, str) or not url.startswith("data:"):
        return None
    try:
        header, b64 = url.split(",", 1)
        mime = header[5:].split(";", 1)[0] or "image/png"  # strip "data:"
        fmt = mime.split("/", 1)[1].lower() if "/" in mime else "png"
        # Converse accepts png/jpeg/gif/webp; normalise jpg→jpeg.
        fmt = {"jpg": "jpeg"}.get(fmt, fmt)
        raw = base64.b64decode(b64)
    except (ValueError, binascii.Error):
        return None
    if not raw:
        return None
    return {"image": {"format": fmt, "source": {"bytes": raw}}}


# Synthetic result fed to Bedrock when an assistant ``toolUse`` has no matching
# ``toolResult`` in history — see :func:`_repair_tool_pairs`.
_ORPHAN_TOOL_RESULT_TEXT = (
    "[tool result unavailable — the previous turn was interrupted before this "
    "tool finished; treat it as no-op and continue]"
)


def _repair_tool_pairs(messages: list[dict]) -> list[dict]:
    """Make ``toolUse``/``toolResult`` pairing valid for Converse.

    Converse rejects a request (``Expected toolResult blocks at messages.N…``)
    when an assistant ``toolUse`` block isn't answered by a ``toolResult`` (same
    ``toolUseId``) in the immediately-following user turn, and likewise rejects a
    ``toolResult`` that answers no preceding ``toolUse``. A turn cancelled
    mid-inference (watchdog wedged-turn recovery / circuit-breaker) records the
    assistant's tool calls but no results, leaving such orphans in the loop's
    persisted history; on the next cycle (or Resume) the whole history replays
    and the request fails.

    This boundary repair, applied to every Converse request, heals such history
    in place: each unanswered ``toolUse`` gets a synthetic ``toolResult`` (marked
    as an interrupted no-op) injected into the following user turn, and any
    ``toolResult`` with no matching prior ``toolUse`` is dropped. Well-formed
    history is returned unchanged.
    """
    # Pass 1: drop toolResult blocks that answer no emitted toolUse id.
    emitted: set[str] = set()
    for msg in messages:
        if msg.get("role") == "assistant":
            for b in msg.get("content") or []:
                tu = b.get("toolUse") if isinstance(b, dict) else None
                if tu is not None:
                    emitted.add(str(tu.get("toolUseId", "") or ""))
    cleaned: list[dict] = []
    for msg in messages:
        content = msg.get("content")
        if (
            msg.get("role") == "user"
            and isinstance(content, list)
            and any("toolResult" in b for b in content)
        ):
            kept = [
                b
                for b in content
                if "toolResult" not in b
                or str(b["toolResult"].get("toolUseId", "") or "") in emitted
            ]
            if not kept:
                continue  # whole turn was orphan results — drop it
            cleaned.append({**msg, "content": kept})
        else:
            cleaned.append(msg)

    # Pass 2: every assistant toolUse must be answered in the NEXT user turn;
    # inject synthetic results for any that aren't.
    out: list[dict] = []
    for i, msg in enumerate(cleaned):
        out.append(msg)
        if msg.get("role") != "assistant":
            continue
        ids = [
            str(b["toolUse"].get("toolUseId", "") or "")
            for b in (msg.get("content") or [])
            if isinstance(b, dict) and "toolUse" in b
        ]
        if not ids:
            continue
        nxt = cleaned[i + 1] if i + 1 < len(cleaned) else None
        answered: set[str] = set()
        nxt_is_results = (
            nxt is not None
            and nxt.get("role") == "user"
            and isinstance(nxt.get("content"), list)
            and any("toolResult" in b for b in nxt["content"])
        )
        if nxt_is_results:
            answered = {
                str(b["toolResult"].get("toolUseId", "") or "")
                for b in nxt["content"]
                if "toolResult" in b
            }
        missing = [tid for tid in ids if tid not in answered]
        if not missing:
            continue
        synthetic = [
            {
                "toolResult": {
                    "toolUseId": tid,
                    "content": [{"text": _ORPHAN_TOOL_RESULT_TEXT}],
                }
            }
            for tid in missing
        ]
        if nxt_is_results:
            # Prepend so the synthetic results sit alongside the real ones.
            nxt["content"] = synthetic + list(nxt["content"])
        else:
            out.append({"role": "user", "content": synthetic})
    return out


class BedrockProvider(ModelProvider):
    """ModelProvider backed by the Bedrock Converse streaming API.

    The legacy :meth:`stream` path is text-only. :meth:`complete` (the
    native-loop contract) additionally supports multi-message history and
    tool calling: the Converse API natively accepts a ``toolConfig`` and
    emits ``toolUse`` content blocks, which :meth:`complete` translates to
    the same :data:`EVENT_TOOL_CALL` shape the other providers emit. ``boto3``
    is imported in :meth:`start` to keep module import SDK-free (Property 11).
    """

    # Bedrock Converse supports tools + multi-message; complete() drives them
    # by translating the loop's OpenAI-shaped messages/tools into Converse.
    supports_tools: bool = True

    # Converse needs a per-request cache CHECKPOINT — it does not cache a prefix on its
    # own — so this provider is EXPLICIT: core's native loop places the neutral marker
    # and :func:`_translate_messages` turns it into a ``cachePoint`` block. The attr the
    # loop reads is on the INSTANCE (``ModelProvider.prompt_cache``); BEDROCK_CAPABILITY
    # carries the same value as the declarative twin. Both must agree — a capability
    # promising cache reads while the instance places no marker is worse than NONE.
    prompt_cache: PromptCache = PromptCache.EXPLICIT

    def __init__(
        self,
        *,
        model: str,
        region: str = DEFAULT_REGION,
        profile_name: str | None = None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> None:
        # NO credential parameter — boto3's chain authenticates (G-AUTH).
        # Empty ⇒ every call that names no model of its own is refused (``require_model``).
        self._model_id = model or ""
        self._region = region or DEFAULT_REGION
        self._profile = profile_name or None
        self._system_prompt = (system_prompt or "").strip()
        # Always send an output cap (see _DEFAULT_MAX_TOKENS): omitting it lets
        # Converse truncate large tool-call JSON mid-stream.
        self._max_tokens = max_tokens if max_tokens is not None else _DEFAULT_MAX_TOKENS
        #: The sampling temperature every request carries, or ``None`` for the model default.
        self._temperature = temperature
        self._client: Any = None
        # Converse message shape: [{"role": "user"|"assistant",
        #                           "content": [{"text": "..."}]}]
        self._history: list[dict[str, Any]] = []
        self._last_context_pct: float = 0.0

    @property
    def sampling_temperature(self) -> float | None:
        """The ``inferenceConfig.temperature`` a :meth:`stream` request carries, if any.

        :meth:`complete` with a reasoning effort turns extended thinking on, which takes no custom
        temperature and drops it — a per-turn fact about the native loop, which one-shot sampling
        (the caller that reads this back) never takes."""
        return self._temperature

    def _inference_config(self, *, thinking: bool = False) -> dict[str, Any]:
        """Converse's ``inferenceConfig``: the output cap always, the sampling temperature when
        one was asked for — except with extended thinking on, which rejects a custom one."""
        config: dict[str, Any] = {"maxTokens": self._max_tokens}
        if self._temperature is not None and not thinking:
            config["temperature"] = self._temperature
        return config

    # ── Context window ────────────────────────────────────────────────
    #
    # ``served_context_window()`` is deliberately NOT overridden, so it answers ``None`` ("this
    # provider cannot say") and core's resolver falls back to the binding's declared
    # ``context_window``, then the model's catalog card, then the shared window table. Bedrock
    # publishes no served window to read: the control plane's ``GetFoundationModel`` returns
    # ``FoundationModelDetails`` (modalities, lifecycle, inference types, streaming support)
    # with no token limits, and Converse reports token USAGE, never the limit it was served
    # against. Answering from the same table the resolver already consults would claim a served
    # measurement this provider never made.

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        """Build the ``bedrock-runtime`` client via boto3's credential chain.

        boto3 session + client construction is SYNCHRONOUS and can be slow — for an
        SSO profile it resolves (and may refresh) cached credentials on the calling
        thread. Doing that inline on the event loop froze the whole gateway for
        ~0.5-1.5s on the first chat turn, which stalled other tabs' WebSocket
        handshakes and emptied the composer model list (the "warmup blocks the
        websocket" symptom). So the blocking build runs in a worker thread.

        Building the client is also where boto3 resolves credentials: a missing profile, a
        credential command that fails, or a region that isn't one fails HERE, before any
        request, so its sentence is made here too — :func:`_raise_friendly`, as a request's.
        """

        def _build_client() -> Any:
            # Lazy import per Property 11. Do NOT lift to module top.
            import boto3  # noqa: PLC0415
            from botocore.config import Config  # noqa: PLC0415

            # Long read timeout so a healthy-but-quiet reasoning stream isn't killed
            # mid-turn (see _STREAM_READ_TIMEOUT). Retries OFF: PersonalClaw owns
            # retry and stall recovery at the loop/watchdog layer, and a botocore
            # retry of a streaming call would replay a partially consumed turn.
            boto_config = Config(
                read_timeout=_STREAM_READ_TIMEOUT,
                connect_timeout=_CONNECT_TIMEOUT,
                retries={"max_attempts": 0, "mode": "standard"},
                tcp_keepalive=True,
            )
            session = (
                boto3.Session(profile_name=self._profile)
                if self._profile else boto3.Session()
            )
            return session.client(
                "bedrock-runtime", region_name=self._region, config=boto_config
            )

        try:
            self._client = await asyncio.to_thread(_build_client)
        except Exception as exc:  # noqa: BLE001 — said as its fix, or re-raised as it is
            _raise_friendly(exc, self._model_id, region=self._region, profile=self._profile)
        logger.info(
            "Bedrock provider ready: model=%s region=%s profile=%s",
            self._model_id or "<none chosen>",
            self._region,
            self._profile or "<default-chain>",
        )

    async def shutdown(self) -> None:
        """Release the boto3 client and clear conversation history."""
        # botocore clients hold a connection pool but expose no async close;
        # dropping the reference lets it be GC'd. History is cleared eagerly.
        self._client = None
        self._history.clear()

    # ── Streaming ─────────────────────────────────────────────────────

    async def stream(self, message: str) -> AsyncIterator[LLMEvent]:
        """Stream a Converse completion, bridging the sync boto stream.

        The blocking ``converse_stream`` call and its ``EventStream``
        iteration run in a worker thread; text deltas and the final usage
        record are pushed onto an :class:`asyncio.Queue` this generator
        drains, so the event loop is never blocked by boto I/O.
        """
        # Refused before anything is built or kept: a call that names no model is never sent.
        model_id = require_model(self._model_id)
        if self._client is None:
            await self.start()

        self._history.append({"role": "user", "content": [{"text": message if message.strip() else "(empty)"}]})
        if len(self._history) > _MAX_HISTORY:
            self._history = self._history[-_MAX_HISTORY:]

        request: dict[str, Any] = {
            "modelId": model_id,
            "messages": self._history,
            "inferenceConfig": self._inference_config(),
        }
        if self._system_prompt:
            request["system"] = [{"text": self._system_prompt}]

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue()

        def _pump() -> None:
            """Run in a worker thread: drive the sync boto stream onto the queue."""
            try:
                response = self._client.converse_stream(**request)
                for event in response.get("stream", []):
                    if "contentBlockDelta" in event:
                        delta = event["contentBlockDelta"].get("delta", {})
                        text = delta.get("text", "")
                        if text:
                            loop.call_soon_threadsafe(queue.put_nowait, ("text", text))
                    elif "messageStop" in event:
                        reason = str(event["messageStop"].get("stopReason") or "")
                        loop.call_soon_threadsafe(queue.put_nowait, ("stop", reason))
                    elif "metadata" in event:
                        usage = event["metadata"].get("usage", {})
                        loop.call_soon_threadsafe(queue.put_nowait, ("usage", usage))
            except Exception as exc:  # surface to the consumer, never crash the thread
                loop.call_soon_threadsafe(queue.put_nowait, ("error", exc))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, _STREAM_DONE)

        worker = asyncio.ensure_future(asyncio.to_thread(_pump))

        assistant_text = ""
        input_tokens = 0
        output_tokens = 0
        cache_creation_tokens = 0
        cache_read_tokens = 0
        # How the answer ended (`messageStop.stopReason`), carried on the terminal event:
        # `max_tokens` is the one core must know, an answer cut at its Max Output Tokens.
        stop_reason = ""

        def _fail(error: Exception) -> NoReturn:
            _raise_friendly(error, self._model_id, region=self._region, profile=self._profile)

        answer = until_terminal(
            _drained(queue, _fail),
            ends=_answer_ended,
            adapter=_ADAPTER,
            missing=_MESSAGE_STOP,
            model=model_id,
        )
        try:
            async with closing_stream(answer) as items:
                async for kind, payload in items:
                    if kind == "text":
                        assistant_text += payload
                        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=payload)
                    elif kind == "stop":
                        stop_reason = payload
                    elif kind == "usage":
                        input_tokens = int(payload.get("inputTokens", input_tokens) or input_tokens)
                        output_tokens = int(
                            payload.get("outputTokens", output_tokens) or output_tokens
                        )
                        cache_creation_tokens, cache_read_tokens = _read_cache_usage(payload)
        finally:
            await worker  # ensure the thread is joined even on cancellation

        if input_tokens > 0:
            ctx = _model_window(self._model_id, _DEFAULT_CONTEXT_WINDOW)
            self._last_context_pct = (input_tokens / ctx) * 100

        if assistant_text:
            self._history.append({"role": "assistant", "content": [{"text": assistant_text}]})

        yield LLMEvent(
            kind=EVENT_COMPLETE,
            stop_reason=stop_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_tokens=cache_creation_tokens,
            cache_read_tokens=cache_read_tokens,
            context_usage_pct=self._last_context_pct,
        )

    # ── Stateless completion (native loop) ────────────────────────────

    async def complete(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        model: str | None = None,
        reasoning_effort: str = "",
    ) -> AsyncIterator[LLMEvent]:
        """Stream a stateless Converse turn for the full ``messages`` list.

        Unlike :meth:`stream`, this NEVER touches ``self._history`` — the
        native loop owns conversation state and sends OpenAI-shaped messages
        + tools uniformly; :func:`_translate_messages` / :func:`_translate_tools`
        map them into Converse's envelope (system list, ``toolUse`` /
        ``toolResult`` blocks, ``toolConfig``).

        Converse streams ``toolUse`` calls as a ``contentBlockStart`` (carrying
        ``toolUseId`` + ``name``) followed by ``contentBlockDelta`` input JSON
        fragments and a ``contentBlockStop``; each call is emitted as one
        :data:`EVENT_TOOL_CALL` once the stream's ``messageStop`` has said how the
        answer ended, and a stream that ends before it raises ``AnswerCutOff``
        (``until_terminal``) and emits none. The blocking boto stream is bridged
        onto an :class:`asyncio.Queue` so the event loop is never stalled (mirrors
        :meth:`stream`).
        """
        model_id = require_model(_bare_model_id(model, self._model_id))
        if self._client is None:
            await self.start()

        # Bedrock-safe tool names (real↔safe). Built from the live `tools` list;
        # reused to rename historical toolUse blocks so config + history agree.
        tool_name_fwd, tool_name_rev = _build_tool_name_maps(tools)
        system_blocks, converse_messages = _translate_messages(messages, tool_name_fwd)

        request: dict[str, Any] = {
            "modelId": model_id,
            "messages": converse_messages,
        }
        if system_blocks:
            request["system"] = system_blocks
        elif self._system_prompt:
            request["system"] = [{"text": self._system_prompt}]
        if tools:
            request["toolConfig"] = _translate_tools(tools, tool_name_fwd)

        # Extended thinking (Anthropic-on-Bedrock): map reasoning effort via
        # additionalModelRequestFields. Newer Claude models (Opus 4.x on Bedrock)
        # take adaptive thinking + an effort LEVEL (output_config.effort) directly —
        # which fits the "no canonical scale" model: the effort token is forwarded
        # as-is. Older models take a fixed budget_tokens; we send the level shape
        # and fall back on the ValidationException path is avoided by using the
        # documented adaptive form. "" = no thinking (model default).
        _eff = (reasoning_effort or "").strip()
        request["inferenceConfig"] = self._inference_config(thinking=bool(_eff))
        if _eff:
            request["additionalModelRequestFields"] = {
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": _eff},
            }

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue()

        def _pump() -> None:
            """Run in a worker thread: drive the sync boto stream onto the queue."""
            try:
                response = self._client.converse_stream(**request)
                for event in response.get("stream", []):
                    if "contentBlockStart" in event:
                        start = event["contentBlockStart"]
                        block_index = start.get("contentBlockIndex", 0)
                        tool_use = start.get("start", {}).get("toolUse")
                        if tool_use is not None:
                            loop.call_soon_threadsafe(
                                queue.put_nowait,
                                ("tool_start", (block_index, tool_use)),
                            )
                    elif "contentBlockDelta" in event:
                        block = event["contentBlockDelta"]
                        block_index = block.get("contentBlockIndex", 0)
                        delta = block.get("delta", {})
                        text = delta.get("text", "")
                        if text:
                            loop.call_soon_threadsafe(queue.put_nowait, ("text", text))
                        tool_delta = delta.get("toolUse")
                        if tool_delta is not None:
                            loop.call_soon_threadsafe(
                                queue.put_nowait,
                                ("tool_delta", (block_index, tool_delta.get("input", ""))),
                            )
                    elif "messageStop" in event:
                        reason = str(event["messageStop"].get("stopReason") or "")
                        loop.call_soon_threadsafe(queue.put_nowait, ("stop", reason))
                    elif "metadata" in event:
                        usage = event["metadata"].get("usage", {})
                        loop.call_soon_threadsafe(queue.put_nowait, ("usage", usage))
            except Exception as exc:  # surface to the consumer, never crash the thread
                loop.call_soon_threadsafe(queue.put_nowait, ("error", exc))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, _STREAM_DONE)

        worker = asyncio.ensure_future(asyncio.to_thread(_pump))

        # Per content-block-index accumulators for toolUse blocks.
        tool_blocks: dict[int, dict[str, str]] = {}
        input_tokens = 0
        output_tokens = 0
        cache_creation_tokens = 0
        cache_read_tokens = 0
        # How the answer ended (`messageStop.stopReason`), carried on the terminal event:
        # `max_tokens` is the one core must know, an answer cut at its Max Output Tokens.
        stop_reason = ""

        def _fail(error: Exception) -> NoReturn:
            _raise_friendly(error, model_id, region=self._region, profile=self._profile)

        answer = until_terminal(
            _drained(queue, _fail),
            ends=_answer_ended,
            adapter=_ADAPTER,
            missing=_MESSAGE_STOP,
            model=model_id,
        )
        try:
            async with closing_stream(answer) as items:
                async for kind, payload in items:
                    if kind == "text":
                        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=payload)
                    elif kind == "tool_start":
                        block_index, tool_use = payload
                        tool_blocks[block_index] = {
                            "id": str(tool_use.get("toolUseId", "") or ""),
                            "name": str(tool_use.get("name", "") or ""),
                            "arguments": "",
                        }
                    elif kind == "tool_delta":
                        block_index, frag = payload
                        bucket = tool_blocks.get(block_index)
                        if bucket is not None and frag:
                            bucket["arguments"] += frag
                    elif kind == "stop":
                        stop_reason = payload
                    elif kind == "usage":
                        input_tokens = int(payload.get("inputTokens", input_tokens) or input_tokens)
                        output_tokens = int(
                            payload.get("outputTokens", output_tokens) or output_tokens
                        )
                        cache_creation_tokens, cache_read_tokens = _read_cache_usage(payload)
        finally:
            await worker  # ensure the thread is joined even on cancellation

        # The answer's calls, once its `messageStop` has said how it ended, in the order they
        # opened, each carrying that reason: `max_tokens` is what tells the runtime a call cut at
        # the cap from a malformed one. A stream that ended before its `messageStop` raised above
        # and emits none, since a call's arguments may never have finished.
        for _block_index, bucket in sorted(tool_blocks.items()):
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id=bucket["id"],
                # Reverse-map the Bedrock-safe name back to the real tool id so the loop
                # dispatches the actual tool.
                title=tool_name_rev.get(bucket["name"], bucket["name"]),
                tool_input=bucket["arguments"],
                stop_reason=stop_reason,
            )

        context_pct = 0.0
        if input_tokens > 0:
            ctx = _model_window(model_id, _DEFAULT_CONTEXT_WINDOW)
            context_pct = (input_tokens / ctx) * 100

        yield LLMEvent(
            kind=EVENT_COMPLETE,
            stop_reason=stop_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_tokens=cache_creation_tokens,
            cache_read_tokens=cache_read_tokens,
            context_usage_pct=context_pct,
            cost_usd=0.0,
        )

    # ── Tool approval (no-op — text-only) ─────────────────────────────

    async def approve_tool(self, request_id: str | int) -> None:
        """No-op: Bedrock is text-only at this layer (no interactive tools)."""
        return None

    async def reject_tool(self, request_id: str | int) -> None:
        """No-op: Bedrock is text-only at this layer (no interactive tools)."""
        return None

    # ── Status ────────────────────────────────────────────────────────

    def context_usage_pct(self) -> float:
        return self._last_context_pct

    async def cancel(self, *, wait_ack_timeout: float = 0.0) -> CancelOutcome:
        """No turn-level abort wired yet; the worker is joined per-stream."""
        return "no_turn"


# ── Capability descriptor ────────────────────────────────────────────────
BEDROCK_CAPABILITY = ProviderCapability(
    type="bedrock",
    capabilities=frozenset({Capability.CHAT, Capability.CODE_TOOLS, Capability.STREAMING, Capability.VISION}),
    supports_streaming=True,
    supports_tools=True,
    supports_embeddings=False,
    supports_vision=True,
    max_context_tokens=0,  # model-dependent
    # EXPLICIT: Converse needs a per-request ``cachePoint`` checkpoint — it caches no
    # prefix on its own. Declarative twin of BedrockProvider.prompt_cache; the two must
    # agree (see the class attr for why a mismatch is worse than declaring NONE).
    prompt_cache=PromptCache.EXPLICIT,
    notes=(
        "Amazon Bedrock Converse via boto3; AWS credential chain. The native-loop "
        "complete() path supports multi-message + tools + image content blocks (vision "
        "models like Nova, Claude, Gemma-VL, Qwen-VL); legacy stream() is text-only."
    ),
)


# ── Catalog (discovery + connectivity via the AWS Bedrock control plane) ──
#
# Model discovery for Bedrock is NOT an HTTP /v1/models call — it queries the AWS
# control plane via boto3 (list_foundation_models + list_inference_profiles). This
# logic used to live in core's discovery handler (coupling core to boto3 + a
# hardcoded fallback catalog); it belongs with the provider. boto3 stays lazily
# imported (Property 11) so importing this module is SDK-free.

# NO hardcoded fallback catalog (user directive 2026-07-06): Bedrock is discovered
# from the control plane, and discovery is authoritative. When it can't list anything
# (no credentials, a failing credential command, a denied permission, no connection),
# ``list_models`` RAISES ``ModelDiscoveryError`` naming why and what to do — the
# catalog contract — rather than answer ``[]``, which reads as an account that serves
# no models, or show fake ids that may not be invocable.


def _capabilities_of(record: dict[str, Any]) -> list[str]:
    """What a foundation model can be bound for here, from its ListFoundationModels record:
    ``[]`` for a model none of this app's adapters can drive through the catalog.

    * **Embedding** — it writes an embedding.
    * **Chat** — a chat turn is a Converse stream of text (``converse_stream``), so the model
      reads and writes text and Bedrock says it streams its answer. A rerank model writes text
      too, and does not stream: it scores documents and holds no conversation. A model that hears
      and speaks (Nova Sonic) takes only the two-way stream this app does not drive. A model that
      reads video and no images is a video analyser (TwelveLabs Pegasus), which answers no
      Converse call; the models that converse about a video read images too. Chat stacks what
      else the model reads: images (``image_modality``) and speech (``audio_modality``:
      understanding audio in a conversation, which is not transcription — speech-to-text is
      Amazon Transcribe).

    A model that writes images or video makes them through the image and video adapters, whose
    lists name the models they can drive."""
    reads = set(record.get("inputModalities") or [])
    writes = set(record.get("outputModalities") or [])
    if "EMBEDDING" in writes:
        return ["embedding"]
    streams = record.get("responseStreamingSupported") is not False
    hears_and_speaks = "SPEECH" in reads and "SPEECH" in writes
    analyses_video = "VIDEO" in reads and "IMAGE" not in reads
    if "TEXT" not in reads or "TEXT" not in writes or not streams or hears_and_speaks or analyses_video:
        return []
    caps = ["chat"]
    if "IMAGE" in reads:
        caps.append("image_modality")
    if "SPEECH" in reads:
        caps.append("audio_modality")
    return caps


def _label(record: dict[str, Any], model_id: str) -> str:
    """A foundation model's name as the pickers show it: "Nova Pro (Amazon)"."""
    label = record.get("modelName", model_id)
    provider = record.get("providerName", "")
    return f"{label}" + (f" ({provider})" if provider and provider not in label else "")


def _routed_model(profile: dict[str, Any], records: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """The record of the foundation model an inference profile routes to, or ``None`` when no
    listing named it. A profile's ``models`` names that one model by ARN, once per Region it
    routes to."""
    for routed in profile.get("models") or []:
        model_id = str(routed.get("modelArn") or "").rpartition("/")[2]
        if model_id in records:
            return records[model_id]
    return None


def _list_bedrock_models_sync(region: str, profile: str) -> list[dict[str, Any]]:
    """Query the Bedrock control plane for every id a call in this region can name, each with
    what the catalog can bind it for (:func:`_capabilities_of`, ``[]`` for a model only the image
    or video adapter can use) and the foundation model that answers it (``model``). Blocking boto3
    calls — run via ``asyncio.to_thread``.

    Combines two sources so every list shows what's actually invocable:
      * ``list_foundation_models()`` — every foundation model's record: what it reads and writes,
        whether it streams, how it is invoked. A model whose record says ON_DEMAND is called by
        its own id.
      * ``list_inference_profiles()`` — cross-region / system profiles (the ``us.*`` / ``global.*``
        ids), the only way to call models that don't offer ON_DEMAND (e.g. newer Claude). Each
        profile id is directly invocable, and does what the model it routes to does, so it is
        bound for that model's capabilities; one whose model no record describes is left out,
        since nothing says what it can do.

    A listing that fails leaves its models out of what the others found. When nothing was
    listed and a listing failed, that failure is raised: it is the answer, not ``[]``.
    """
    import boto3  # noqa: PLC0415 — lazy per Property 11

    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    client = session.client("bedrock", region_name=region or DEFAULT_REGION)

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    failed: list[Exception] = []
    records: dict[str, dict[str, Any]] = {}

    # ── Foundation models ──
    try:
        resp = client.list_foundation_models()
        for m in resp.get("modelSummaries", []):
            records.setdefault(str(m.get("modelId") or ""), m)
    except Exception as exc:
        failed.append(exc)
        logger.debug("Bedrock list_foundation_models failed", exc_info=True)
    records.pop("", None)
    for model_id, m in records.items():
        # Only models invocable directly (ON_DEMAND); the rest need a profile
        # and surface via list_inference_profiles below.
        if "ON_DEMAND" not in (m.get("inferenceTypesSupported") or []):
            continue
        if (m.get("modelLifecycle") or {}).get("status", "ACTIVE") != "ACTIVE":
            continue
        seen.add(model_id)
        out.append({
            "id": model_id,
            "name": _label(m, model_id),
            "capabilities": _capabilities_of(m),
            "model": model_id,
        })

    # ── Inference profiles (cross-region / system — the us.* invocable ids) ──
    try:
        paginator_kwargs: dict[str, Any] = {}
        while True:
            resp = client.list_inference_profiles(**paginator_kwargs)
            for p in resp.get("inferenceProfileSummaries", []):
                pid = p.get("inferenceProfileId", "")
                if not pid or pid in seen:
                    continue
                if p.get("status", "ACTIVE") != "ACTIVE":
                    continue
                routed = _routed_model(p, records)
                if routed is None:
                    continue
                seen.add(pid)
                out.append({
                    "id": pid,
                    "name": p.get("inferenceProfileName", pid),
                    "capabilities": _capabilities_of(routed),
                    "model": str(routed.get("modelId") or ""),
                })
            token = resp.get("nextToken")
            if not token:
                break
            paginator_kwargs = {"nextToken": token}
    except Exception as exc:
        failed.append(exc)
        logger.debug("Bedrock list_inference_profiles failed", exc_info=True)

    if failed and not out:
        raise failed[0]
    return out


# Short TTL cache keyed by (region, profile): the control-plane catalog is stable
# and Settings discovery is hit on every dropdown open, so we avoid two AWS
# round-trips per request. Only successful non-empty results are cached.
_BEDROCK_CACHE: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
_BEDROCK_CACHE_TTL = 300.0  # seconds


def _discovery_failure(error: Exception, *, region: str, profile: str | None) -> ModelDiscoveryError:
    """Why Bedrock's models could not be listed, as the connection test and the Models page say
    it: the cause and what to do, then the SDK's own words.

    The AWS setup's sentence when that is what failed (:func:`_aws_setup_problem`); a listing
    call the identity's IAM policy does not allow; credentials AWS turned down. Anything else is
    said as a listing that did not answer. No ``status`` is given: that would read as a stored
    key the endpoint refused ("update its key"), and this instance stores no key."""
    setup = _aws_setup_problem(error, region=region, profile=profile)
    if setup is not None:
        return ModelDiscoveryError(setup)
    msg = str(error)
    code = _aws_error_code(error)
    named = _NOT_AUTHORIZED_RE.search(msg) if code == "AccessDeniedException" else None
    if named:
        sentence = (
            f"Your AWS credentials aren't allowed to call {named['action']} in {region}, so "
            "Amazon Bedrock's models can't be listed. Add that action to the IAM policy of the "
            f"identity Bedrock signs in as, then try again, or {_SET_PROFILE} to a profile that "
            "has it."
        )
    elif code in _REJECTED_CREDENTIAL_CODES or "security token included in the request is" in msg:
        in_use = _profile_in_use(profile)
        flag = f" --profile {in_use}" if in_use else ""
        sentence = (
            "AWS turned down the credentials Bedrock used: their security token is invalid or "
            f"has expired. Sign in to AWS again (`aws sso login{flag}` for an SSO profile), then "
            "try again."
        )
    else:
        sentence = f"No model list came back from Amazon Bedrock in {region}. {_SEE_THE_LOG}"
    return ModelDiscoveryError(sentence_with_detail(sentence, error))


async def _listed_in_region(region: str, profile: str | None) -> list[dict[str, Any]]:
    """Every id a call in ``region`` can name (:func:`_list_bedrock_models_sync`), sorted by name
    and cached for ``_BEDROCK_CACHE_TTL`` seconds: the chat catalog and the image and video adapters
    read the one listing. A listing that could not run raises its :class:`ModelDiscoveryError`."""
    region = region or DEFAULT_REGION
    key = (region, profile or "")
    cached = _BEDROCK_CACHE.get(key)
    if cached and (_time.monotonic() - cached[0]) < _BEDROCK_CACHE_TTL:
        return cached[1]
    try:
        rows = await asyncio.to_thread(_list_bedrock_models_sync, region, profile or "")
    except Exception as exc:  # noqa: BLE001 — every failure is said as its cause
        failure = _discovery_failure(exc, region=region, profile=profile or None)
        _warn_once("Listing Amazon Bedrock's models", str(failure), exc)
        raise failure from exc
    rows.sort(key=lambda m: m.get("name", "").lower())
    if rows:
        _BEDROCK_CACHE[key] = (_time.monotonic(), rows)
    return rows


def _distinct_names(rows: list[dict[str, Any]]) -> dict[str, str]:
    """Each listed model's name by id, told apart from every other's: AWS gives two models names
    that differ in case alone ("Titan Text Embeddings v2" and "Titan Text Embeddings V2"), so a
    name another model shares, case aside, is shown with its id after it."""
    named = {r["id"]: str(r.get("name") or r["id"]) for r in rows}
    counts: dict[str, int] = {}
    for name in named.values():
        counts[name.casefold()] = counts.get(name.casefold(), 0) + 1
    return {
        model_id: f"{name} · {model_id}" if counts[name.casefold()] > 1 else name
        for model_id, name in named.items()
    }


class BedrockCatalog(ModelCatalog):
    """Discovers Bedrock models from the AWS control plane (boto3 chain auth), cached for
    ``_BEDROCK_CACHE_TTL`` seconds. Config-only: reads region/profile from the entry options.

    Its connection test is the catalog contract's own: listing is the connectivity signal, and a
    listing that could not run raises :class:`ModelDiscoveryError` with its cause, which the test
    reports as written. There is no fallback catalog to show instead."""

    def __init__(self, region: str = "", profile: str = "") -> None:
        self._region = region or DEFAULT_REGION
        self._profile = profile or ""

    async def list_models(self) -> list[ModelInfo]:
        rows = await _listed_in_region(self._region, self._profile)
        offered = []
        for r in rows:
            capabilities = list(r.get("capabilities", ["chat"]))
            # Embedding is offered only on a model this app knows the request of.
            if "embedding" in capabilities and _embedding_model(r["id"]) is None:
                capabilities.remove("embedding")
            if capabilities:
                offered.append({**r, "capabilities": capabilities})
        names = _distinct_names(offered)
        models = [
            ModelInfo(id=r["id"], name=names[r["id"]], capabilities=r["capabilities"])
            for r in offered
        ]
        if models:
            # Amazon Transcribe, which the speech-to-text adapter runs, is no foundation model,
            # so no listing names it, and Settings → Models had nothing to bind speech-to-text
            # to. Listed for an account the listings reached, so a binding can name it.
            models.append(ModelInfo(id=TRANSCRIBE_MODEL, name="Amazon Transcribe", capabilities=["stt"]))
        return models


def create_catalog(options: dict[str, Any] | None = None, *, model: str = "") -> BedrockCatalog:
    """Catalog factory (registry contract) — build discovery from entry options."""
    del model
    opts = options or {}
    return BedrockCatalog(region=str(opts.get("region") or ""), profile=str(opts.get("profile") or ""))


# ── Extension factory (named by the bundled manifest's `implementation`) ──


def create_provider(config: dict[str, Any]) -> BedrockProvider:
    """Build a :class:`BedrockProvider` from a model-Extension instance config.

    Reads ``region`` / ``default_model`` (or ``model``) / ``profile`` /
    ``system_prompt`` / ``max_tokens`` from the instance settings. No
    credential is resolved — boto3's chain authenticates (G-AUTH).
    """
    # `max_tokens` is DECLARED in the manifest schema, which is what makes it settable at all —
    # the config form renders only declared properties. Its declared default is 0, and 0 means
    # "leave it to the model", which `output_cap` reads as unset rather than a zero-token ceiling.
    max_tokens = output_cap(config.get("max_tokens"), None)
    return BedrockProvider(
        # The instance's own model: its Default Model. With none, each call is refused.
        model=own_model(config.get("model"), config),
        region=config.get("region") or DEFAULT_REGION,
        profile_name=config.get("profile") or None,
        system_prompt=config.get("system_prompt") or None,
        max_tokens=max_tokens,
    )


# ── Registry factory ──────────────────────────────────────────────────────


def _factory(
    *,
    entry: ProviderEntry,
    session_key: str | None = None,
    **kwargs: object,
) -> ModelProvider:
    """Construct a :class:`BedrockProvider` from a :class:`ProviderEntry`.

    ``session_key`` is accepted for registry-contract parity but ignored —
    Bedrock is stateless. No credential is resolved (G-AUTH): the entry's
    options carry only region/model/profile.

    A ``model`` kwarg (threaded by ``registry.build(name, model=…)``) is the model the call
    is built for: core resolves the active model per use-case (e.g.
    ``Bedrock:global.anthropic.claude-opus-4-8``) and passes it, and the provider must
    serve exactly that. Without one, the entry's own model (the SDK's
    ``ProviderEntry.own_model``: its model, else the Default Model the Add-instance form
    saves). With neither, the provider is built for no model and refuses each call.
    """
    del session_key  # unused — Bedrock provider is stateless.

    options = dict(entry.options or {})
    region = str(options.get("region") or DEFAULT_REGION)
    profile_value = options.get("profile")
    profile = str(profile_value) if profile_value else None
    system_value = options.get("system_prompt")
    system_prompt = str(system_value) if system_value else None
    # The operator's configured cap, else the budget core derives for the model it is building
    # for (the ``max_tokens`` build kwarg), else the provider default. The schema's declared
    # default is 0, and 0 means "leave it to the model" — the same rule create_provider applies —
    # so it must not pass through as a zero-token ceiling.
    max_tokens = output_cap(options.get("max_tokens"), kwargs.get("max_tokens"))
    # A per-call sampling temperature: best-of-N builds each candidate with the ``temperature``
    # build kwarg. Without it every request samples at the model's default.
    temperature = per_call_temperature(kwargs)

    model = str(kwargs.get("model") or "") or entry.own_model

    return BedrockProvider(
        model=model,
        region=region,
        profile_name=profile,
        system_prompt=system_prompt,
        max_tokens=max_tokens,
        temperature=temperature,
    )


# ── Registration ─────────────────────────────────────────────────────────
# Register on import — the app loader imports this module when the app is
# enabled, wiring the type into the default registry without pulling boto3
# into ``sys.modules`` (lazy SDK import). Idempotent against module reload
# in tests.
try:
    get_default_registry().register_type(BEDROCK_CAPABILITY, _factory)
except ProviderResolutionError:
    logger.debug("bedrock provider type already registered with default registry")

# The discovery/connectivity axis (register_catalog is idempotent — last wins).
get_default_registry().register_catalog("bedrock", create_catalog)


# ═══════════════════════════════════════════════════════════════════════════════
# ADDITIONAL PROVIDERS: Embedding, Image Generation, Video Generation, STT
# ═══════════════════════════════════════════════════════════════════════════════
#
# Each provider is independent of the chat ``BedrockProvider`` above and targets a
# different capability axis. They share the same boto3 credential chain and region
# resolution, and wrap all synchronous boto3 calls in ``asyncio.to_thread`` /
# ``run_in_executor`` so the event loop is never blocked.

import tempfile
import time as _time

from personalclaw.sdk.embedding import EmbeddingProvider
from personalclaw.sdk.image import (
    ImageGenError,
    ImageGenModel,
    ImageGenProvider,
    ImageResult,
)
from personalclaw.sdk.video import (
    VideoGenError,
    VideoGenModel,
    VideoGenProvider,
    VideoResult,
)
from personalclaw.sdk.stt import SttError, SttProvider, TranscriptResult


# ── Shared credential check (never blocks the event loop) ────────────────────
#
# boto3's ``Session.get_credentials()`` does synchronous disk / SSO-cache I/O
# (and can trigger a network refresh for an SSO profile). Calling it directly in
# an async ``is_available()`` blocks the aiohttp event loop — and the media
# registries probe ``is_available()`` on every ``/api/models/available`` call,
# so the loop stalls (symptoms: the composer's model list empties on send, new
# tabs spin). Run it on a worker thread AND cache the boolean per (profile,
# region) so repeated probes are instant.
#
# Cached for a while, not for the process: the answer was kept forever, so credentials that
# came back (an SSO login, a fixed profile) left embeddings, images, video and speech
# unavailable until a restart. A missing credential is asked about again soon, so recovery is
# seen on the next use after it; a working one is re-checked less often, so an expired one is
# noticed too.
#
# The answer is the REASON, not only the bit: a credential command that failed made every
# media feature read as unavailable with nothing saying why, and the probe had the error in
# its hand when it answered False.

#: Seconds one answer stands: a credential that resolved, and one that did not.
_CRED_OK_TTL = 300.0
_CRED_MISSING_TTL = 30.0
#: (profile, region) → (when it was measured, why the chain cannot sign in — "" when it can).
_cred_cache: dict[tuple[str, str], tuple[float, str]] = {}


async def _creds_problem(region: str, profile: str | None) -> str:
    """Why the AWS credential chain cannot sign in for this profile — the setup sentence the
    chat says for the same failure (:func:`_aws_setup_problem`) — or ``""`` when it resolves.

    Cached for a while (``_CRED_OK_TTL`` / ``_CRED_MISSING_TTL``), and run off the event loop so
    it never blocks."""
    key = (profile or "", region or "")
    hit = _cred_cache.get(key)
    if hit is not None:
        at, problem = hit
        if _time.monotonic() - at < (_CRED_MISSING_TTL if problem else _CRED_OK_TTL):
            return problem

    def _probe() -> str:
        try:
            import boto3  # noqa: PLC0415

            session = boto3.Session(profile_name=profile) if profile else boto3.Session()
            found = session.get_credentials()
        except Exception as exc:  # noqa: BLE001 — every failure is said as its cause
            problem = _aws_setup_problem(exc, region=region, profile=profile)
            if problem is None:
                problem = sentence_with_detail(
                    f"Amazon Bedrock couldn't check its AWS credentials. {_SEE_THE_LOG}", exc
                )
                _warn_once("Checking the AWS credentials", problem, exc)
            return problem
        return "" if found is not None else _no_credentials(profile)

    problem = await asyncio.to_thread(_probe)
    _cred_cache[key] = (_time.monotonic(), problem)
    return problem


async def _creds_ok(region: str, profile: str | None) -> bool:
    """Whether the AWS credential chain resolves for this profile (:func:`_creds_problem`)."""
    return not await _creds_problem(region, profile)


# ── Bedrock media providers ──────────────────────────────────────────────────
#
# Like chat, a media call names its model: the binding in Settings → Models for that use case
# (``Bedrock:amazon.nova-canvas-v1:0``). One that names none is refused before anything is sent
# (the SDK's ``require_model``). Each adapter used to put Bedrock's first model of its kind in
# its place (Titan Embed, Nova Canvas, Nova Reel), and the speech adapter ran Transcribe for a
# binding that named no model at all.


def _media_failure(
    what: str, error: Exception, model_id: str, *, region: str, profile: str | None
) -> str:
    """The sentence a failed image, video or embedding call reports: the chat's own sentence for
    the same failure (:func:`_friendly_bedrock_error` — the AWS setup, model access, credentials
    AWS turned down), else that ``what`` failed, what to do, and the SDK's words after it."""
    friendly = _friendly_bedrock_error(error, model_id, region=region, profile=profile)
    if friendly is not error:
        return str(friendly)
    return sentence_with_detail(f"Bedrock {what} failed. {_SEE_THE_LOG}", error)


def _needs_bucket(purpose: str) -> str:
    """What a media feature that stages its files in S3 says when the instance names no bucket.
    ``purpose`` is the sentence saying what the feature needs one for."""
    return (
        f"{purpose} Set S3 Bucket {_ON_INSTANCE} (under Advanced), or the "
        "BEDROCK_VIDEO_S3_BUCKET environment variable, then try again."
    )


_VIDEO_NEEDS_BUCKET = (
    "Bedrock video generation needs an S3 bucket for Nova Reel to write the video to."
)
_STT_NEEDS_BUCKET = (
    "Speech-to-text with Amazon Transcribe needs an S3 bucket to upload each recording to."
)

#: When each failure was last logged at WARNING, by what failed and the sentence saying why. A
#: failure repeats with every call that meets it (the Models page lists models on every read), so
#: the gateway log says it once every ``_WARN_EVERY`` seconds, and at DEBUG in between.
_WARNED_AT: dict[tuple[str, str], float] = {}
_WARN_EVERY = 300.0


def _warn_once(what: str, sentence: str, error: BaseException) -> None:
    """Log that ``what`` failed: ``sentence``, one line, at WARNING the first time in
    ``_WARN_EVERY`` seconds, else at DEBUG. The sentence's own words, not its SDK detail, are what
    makes it the same failure.

    A WARNING is the sentence alone, its SDK detail included. The traceback of a condition this
    app recognises says nothing the sentence does not, and sixty lines of botocore under a
    warning buried the sentence, on every failure a user meets at use time (an image the region
    has no model for). A sentence that could name no cause sends the reader to the gateway log
    (:data:`_SEE_THE_LOG`), so ``error``'s traceback follows it there at DEBUG, redacted as the
    sentence's detail is, since what a credential command printed is in it."""
    key = (what, sentence.split(" Details: ", 1)[0])
    now = _time.monotonic()
    at = _WARNED_AT.get(key)
    repeat = at is not None and now - at < _WARN_EVERY
    if not repeat:
        if len(_WARNED_AT) > 64:
            _WARNED_AT.clear()
        _WARNED_AT[key] = now
    logger.log(logging.DEBUG if repeat else logging.WARNING, "%s failed: %s", what, sentence)
    if _SEE_THE_LOG not in sentence or not logger.isEnabledFor(logging.DEBUG):
        return
    import traceback  # noqa: PLC0415 — failure path only

    from personalclaw.sdk.channel import redact_credentials  # noqa: PLC0415

    trace, _ = redact_credentials("".join(traceback.format_exception(error)).rstrip())
    logger.debug("%s failed with this traceback:\n%s", what, trace)


# ── Bedrock embedding models: each one's own request and answer ─────────────────────────
#
# Every embedding model on Bedrock takes an ``InvokeModel`` body of its own and answers in a shape
# of its own, as the Amazon Bedrock User Guide documents them ("Inference request parameters and
# response fields for foundation models": Amazon Titan Embeddings G1 - Text, which also covers
# Titan Text Embeddings V2; Amazon Titan Multimodal Embeddings G1; Cohere Embed v3 and v4) and the
# Amazon Nova User Guide ("Complete embeddings request and response schema"). One body for every id
# that was not Cohere's (V2's ``dimensions`` and ``normalize``, which only V2 takes) and one answer
# for every Cohere id (v4's) failed four of the six models a region listed on their first text.
#
# A model is called only the way its documentation says, so only those models are offered for
# Embedding (``BedrockCatalog.list_models``), and a binding to any other is refused before anything
# is sent. Titan Text Embeddings ``amazon.titan-embed-g1-text-02`` is listed by Bedrock but has no
# documented request body.

#: The width every model that lets the caller choose one is asked for: Titan Text Embeddings V2's
#: own default, and one each of the others takes.
_EMBEDDING_WIDTH = 1024


def _titan_text_g1_body(text: str) -> dict[str, Any]:
    """Titan Embeddings G1 - Text: ``inputText`` is its only field."""
    return {"inputText": text}


def _titan_text_v2_body(text: str) -> dict[str, Any]:
    return {"inputText": text, "dimensions": _EMBEDDING_WIDTH, "normalize": True}


def _titan_multimodal_body(text: str) -> dict[str, Any]:
    return {"inputText": text, "embeddingConfig": {"outputEmbeddingLength": _EMBEDDING_WIDTH}}


def _cohere_body(text: str) -> dict[str, Any]:
    """Cohere Embed v3 and v4 take the same text request. Every text is embedded as a document:
    the embedding contract has no query/document distinction to pass on."""
    return {"texts": [text], "input_type": "search_document"}


def _nova_body(text: str) -> dict[str, Any]:
    return {
        "taskType": "SINGLE_EMBEDDING",
        "singleEmbeddingParams": {
            "embeddingPurpose": "GENERIC_INDEX",
            "embeddingDimension": _EMBEDDING_WIDTH,
            "text": {"truncationMode": "END", "value": text},
        },
    }


class _NoEmbedding(Exception):
    """A model answered, and its answer holds no embedding."""


def _titan_vector(answer: dict[str, Any]) -> Any:
    """Every Titan embedding model answers ``embedding``; the multimodal one says in ``message``
    why it could not."""
    if answer.get("message") and not answer.get("embedding"):
        raise _NoEmbedding(str(answer["message"]))
    return answer.get("embedding")


def _cohere_vector(answer: dict[str, Any]) -> Any:
    """Cohere answers ``embeddings`` as a list of vectors (``embeddings_floats``) or, by type, as
    ``{"float": [...]}`` (``embeddings_by_type``)."""
    embeddings = answer.get("embeddings")
    if isinstance(embeddings, dict):
        embeddings = embeddings.get("float")
    return embeddings[0] if isinstance(embeddings, list) and embeddings else None


def _nova_vector(answer: dict[str, Any]) -> Any:
    embeddings = answer.get("embeddings")
    first = embeddings[0] if isinstance(embeddings, list) and embeddings else {}
    return first.get("embedding") if isinstance(first, dict) else None


class _EmbeddingModel(NamedTuple):
    """How one embedding model is called: its request body for a text, where its answer keeps the
    vector, and, for a model that refuses a longer text outright, the most characters it takes."""

    body: Callable[[str], dict[str, Any]]
    vector: Callable[[dict[str, Any]], Any]
    max_chars: int | None = None


_COHERE_V3 = _EmbeddingModel(_cohere_body, _cohere_vector, max_chars=2048)

#: Keyed by the model's id without its version (``amazon.titan-embed-text-v2`` for ``…-v2:0``).
#: Cohere Embed v3 refuses a text over 2,048 characters, and Nova one over 8,192; each cuts a text
#: past its token limit at the end, so each is sent the first that many characters.
_EMBEDDING_MODELS: dict[str, _EmbeddingModel] = {
    "amazon.titan-embed-text-v1": _EmbeddingModel(_titan_text_g1_body, _titan_vector),
    "amazon.titan-embed-text-v2": _EmbeddingModel(_titan_text_v2_body, _titan_vector),
    "amazon.titan-embed-image-v1": _EmbeddingModel(_titan_multimodal_body, _titan_vector),
    "cohere.embed-english-v3": _COHERE_V3,
    "cohere.embed-multilingual-v3": _COHERE_V3,
    "cohere.embed-v4": _EmbeddingModel(_cohere_body, _cohere_vector),
    "amazon.nova-2-multimodal-embeddings-v1": _EmbeddingModel(
        _nova_body, _nova_vector, max_chars=8192
    ),
}


_Known = TypeVar("_Known")


def _by_family(known: dict[str, _Known], model_id: str) -> _Known | None:
    """What ``known`` says of ``model_id``, keyed by the model's id without its version
    (``amazon.titan-embed-text-v2`` for ``…-v2:0``), or None. An inference profile
    (``us.cohere.embed-v4:0``, ``global.…``) is the model after its geography."""
    bare = model_id.split(":", 1)[0]
    found = known.get(bare)
    if found is None and "." in bare:
        found = known.get(bare.split(".", 1)[1])
    return found


def _embedding_model(model_id: str) -> _EmbeddingModel | None:
    """How ``model_id`` is called, or None for a model this app cannot call."""
    return _by_family(_EMBEDDING_MODELS, model_id)


def _not_callable(model_id: str) -> str:
    """What an embedding on a model this app has no request for says."""
    return (
        f"Bedrock's {model_id} is not an embedding model this app can call, so nothing was sent. "
        "Choose one of the embedding models Settings → Models lists for Amazon Bedrock."
    )


# ── Bedrock Embedding Provider ───────────────────────────────────────────────


class BedrockEmbeddingProvider(EmbeddingProvider):
    """Embedding via Bedrock ``invoke_model`` (Titan Embeddings, Cohere Embed, Nova Multimodal
    Embeddings), each model called as ``_EMBEDDING_MODELS`` says.

    boto3 is lazily imported inside methods (Property 11). All blocking calls
    run via ``asyncio.to_thread`` so the event loop stays unblocked.
    """

    def __init__(self, *, region: str = DEFAULT_REGION, profile: str | None = None,
                 name: str = "bedrock") -> None:
        self._region = region or DEFAULT_REGION
        self._profile = profile
        self._name = name
        #: The last embedding's failure — when, and the sentence saying why — until one succeeds.
        self._last_failure: tuple[float, str] | None = None

    @property
    def name(self) -> str:
        return self._name

    @name.setter
    def name(self, value: str) -> None:
        self._name = value

    @property
    def display_name(self) -> str:
        return "Amazon Bedrock (embedding)"

    async def unavailable_reason(self) -> str:
        """Why an embedding did not come back: the last one's failure while it is recent (what
        AWS answered for the model: no access to it, an action the policy lacks, credentials it
        turned down), else why the AWS credential chain cannot sign in, else ``""``.

        A recent failure stands as long as a missing credential's answer does
        (``_CRED_MISSING_TTL``), so a re-index refused on it names the fix, and a fix is seen soon.
        """
        failed = self._last_failure
        if failed is not None and _time.monotonic() - failed[0] < _CRED_MISSING_TTL:
            return failed[1]
        return await _creds_problem(self._region, self._profile)

    def _get_client(self):
        """Build a fresh bedrock-runtime client (lazy boto3 import)."""
        import boto3  # noqa: PLC0415

        session = boto3.Session(profile_name=self._profile) if self._profile else boto3.Session()
        return session.client("bedrock-runtime", region_name=self._region)

    async def is_available(self) -> bool:
        """True if the AWS credential chain resolves (cached, off-loop)."""
        return await _creds_ok(self._region, self._profile)

    def _invoke_embed_sync(self, text: str, model_id: str, how: _EmbeddingModel) -> list[float]:
        """Blocking ``invoke_model`` for one embedding, sent and read as ``how`` says — run via
        to_thread. Raises :class:`_NoEmbedding` for an answer that holds no vector."""
        if how.max_chars is not None:
            text = text[: how.max_chars]
        response = self._get_client().invoke_model(
            modelId=model_id,
            body=json.dumps(how.body(text)),
            contentType="application/json",
            accept="application/json",
        )
        vector = how.vector(json.loads(response["body"].read()))
        if not isinstance(vector, list) or not vector:
            raise _NoEmbedding(f"{model_id} answered with no embedding.")
        return vector

    async def embed(self, text: str, model: str = "") -> list[float] | None:
        """Embed a single text string with ``model``. ``None`` when it names none or a model this
        app cannot call, or fails.

        A failure is logged with the sentence that names its fix, and :meth:`unavailable_reason`
        says it: the contract answers a failure with ``None`` and nothing else."""
        try:
            model_id = require_model(model)
        except ProviderResolutionError as exc:
            logger.warning("Bedrock embedding on %r refused: %s", self._name, exc)
            return None
        how = _embedding_model(model_id)
        if how is None:
            sentence = _not_callable(model_id)
            self._last_failure = (_time.monotonic(), sentence)
            _warn_once(f"Bedrock embedding on {self._name!r}", sentence, ValueError(sentence))
            return None
        try:
            vector = await asyncio.to_thread(self._invoke_embed_sync, text, model_id, how)
        except Exception as exc:  # noqa: BLE001 — said, then answered with the contract's None
            sentence = _media_failure(
                "embedding", exc, model_id, region=self._region, profile=self._profile
            )
            self._last_failure = (_time.monotonic(), sentence)
            _warn_once(f"Bedrock embedding on {self._name!r}", sentence, exc)
            return None
        self._last_failure = None
        return vector

    async def embed_batch(self, texts: list[str], model: str = "") -> list[list[float] | None]:
        """Embed multiple texts (sequential calls — Bedrock has no native batch): one entry per
        text, in order, ``None`` for a text that was not embedded.

        A failed text was answered with an empty vector, which core's batch path stores as the
        text's vector: ``None`` is how it knows to keep the text without one, keyword-searchable,
        and :meth:`unavailable_reason` says why. A call that names no model embeds nothing."""
        try:
            require_model(model)
        except ProviderResolutionError as exc:
            logger.warning("Bedrock embedding on %r refused: %s", self._name, exc)
            return [None for _ in texts]
        return [await self.embed(text, model) for text in texts]


# ── Bedrock image generation: each model's own request and answer ─────────────────────────────
#
# A model that makes an image from a prompt takes an ``InvokeModel`` body of its own and answers
# in a shape of its own, as AWS documents them: Amazon Nova Canvas a ``TEXT_IMAGE`` task (the
# Amazon Nova User Guide, "Request and response structure for image generation"), and Stability
# AI's Stable Diffusion 3.5 Large, Stable Image Core and Stable Image Ultra a prompt and an aspect
# ratio (the Amazon Bedrock User Guide, "Stability AI models"). Stability AI's Image Services
# (inpaint, outpaint, erase, search and replace or recolor, remove background, the upscalers,
# control sketch and structure, style guide and style transfer) each change an image they are
# given, so none makes one from a prompt; nothing here edits an image, so none of them is offered.
#
# Image · Generation offers only these models, and only those the instance's region lists (the
# one listing the chat catalog reads), each by the id a call there names: the model's own when
# Bedrock serves it on demand, else that of the inference profile that routes to it. The list used
# to name Nova Canvas whatever the region, and a binding to it in a region that does not serve it
# was refused at its first image with "The provided model identifier is invalid".

#: The size Nova Canvas is asked for when a call names none it can read.
_NOVA_CANVAS_SIZE = (1024, 1024)

#: The aspect ratios Stability AI's text-to-image models make, as each takes them.
_STABILITY_RATIOS = ("16:9", "1:1", "21:9", "2:3", "3:2", "4:5", "5:4", "9:16", "9:21")


def _width_height(size: str) -> tuple[int, int] | None:
    """A size as a width and a height ("1280x720" → (1280, 720)), or None for one that names
    neither."""
    width, _, height = size.strip().lower().partition("x")
    if width.isdigit() and height.isdigit() and int(width) and int(height):
        return int(width), int(height)
    return None


def _aspect_ratio(size: str) -> str:
    """The aspect ratio a Stability AI model is asked for: ``size`` when it is one the model makes
    ("16:9"), the nearest one to a width and height ("1280x720" → "16:9"), else its default, 1:1."""
    size = size.strip()
    if size in _STABILITY_RATIOS:
        return size
    named = _width_height(size)
    if named is None:
        return "1:1"
    import math  # noqa: PLC0415 — a size named as a width and height only

    wanted = math.log(named[0] / named[1])

    def _off(ratio: str) -> float:
        across, _, up = ratio.partition(":")
        return abs(math.log(int(across) / int(up)) - wanted)

    return min(_STABILITY_RATIOS, key=_off)


def _nova_canvas_body(prompt: str, size: str, count: int) -> dict[str, Any]:
    width, height = _width_height(size) or _NOVA_CANVAS_SIZE
    return {
        "taskType": "TEXT_IMAGE",
        "textToImageParams": {"text": prompt},
        "imageGenerationConfig": {"numberOfImages": count, "width": width, "height": height},
    }


def _stability_body(prompt: str, size: str, count: int) -> dict[str, Any]:
    """Stable Diffusion 3.5 Large, Stable Image Core and Ultra take the same text-to-image request,
    and each makes one image for it, so ``count`` is always one."""
    del count
    return {"prompt": prompt, "aspect_ratio": _aspect_ratio(size), "output_format": "png"}


class _NoImage(Exception):
    """A model answered, and its answer holds no image. Its text is the reason the answer gave."""


def _nova_canvas_images(answer: dict[str, Any]) -> list[str]:
    """Nova Canvas answers ``images``, or says in ``error`` why it made none."""
    images = [str(image) for image in answer.get("images") or [] if image]
    if not images and answer.get("error"):
        raise _NoImage(str(answer["error"]))
    return images


def _stability_images(answer: dict[str, Any]) -> list[str]:
    """Stability AI's models answer ``images``, and a ``finish_reasons`` entry that is not null for
    one they filtered or could not make ("Filter reason: prompt", "Inference error")."""
    images = [str(image) for image in answer.get("images") or [] if image]
    reasons = [str(reason) for reason in answer.get("finish_reasons") or [] if reason]
    if not images and reasons:
        raise _NoImage("; ".join(reasons))
    return images


class _ImageModel(NamedTuple):
    """How one model makes an image from a prompt: its request body for a prompt, a size and a count
    of images, where its answer keeps them, the most one request makes, and the sizes it lists."""

    body: Callable[[str, str, int], dict[str, Any]]
    images: Callable[[dict[str, Any]], list[str]]
    per_request: int
    sizes: tuple[str, ...] = ()


_STABILITY_TEXT_TO_IMAGE = _ImageModel(_stability_body, _stability_images, per_request=1)

#: Keyed by the model's id without its version (``stability.stable-image-core-v1`` for ``…-v1:1``).
#: Stability AI's models are asked for an aspect ratio, not a size, so they list none.
_IMAGE_MODELS: dict[str, _ImageModel] = {
    "amazon.nova-canvas-v1": _ImageModel(
        _nova_canvas_body,
        _nova_canvas_images,
        per_request=5,
        sizes=("1024x1024", "1280x720", "720x1280"),
    ),
    "stability.sd3-5-large-v1": _STABILITY_TEXT_TO_IMAGE,
    "stability.stable-image-core-v1": _STABILITY_TEXT_TO_IMAGE,
    "stability.stable-image-ultra-v1": _STABILITY_TEXT_TO_IMAGE,
}


def _image_model(model_id: str) -> _ImageModel | None:
    """How ``model_id`` makes an image from a prompt, or None for a model this app cannot call
    so."""
    return _by_family(_IMAGE_MODELS, model_id)


def _not_an_image_model(model_id: str) -> str:
    """What an image on a model this app has no text-to-image request for says."""
    return (
        f"Bedrock's {model_id} is not a model this app can make an image from a prompt with, so "
        "nothing was sent. Choose one of the models Settings → Models lists under Image · "
        "Generation for this Amazon Bedrock instance."
    )


def _no_image_model(region: str) -> str:
    """What Image · Generation says for an instance whose region lists none of
    :data:`_IMAGE_MODELS`."""
    return (
        f"No image generation model is available in {region}: Amazon Bedrock lists none there that "
        "this app can make an image from a prompt with (Amazon Nova Canvas, Stable Diffusion 3.5 "
        "Large, Stable Image Core, Stable Image Ultra). Models that only edit or upscale an image "
        f"are not offered. Set AWS Region {_ON_INSTANCE} to a region that lists one of them, or "
        "add an instance for that region."
    )


def _made_no_image(model_id: str, reason: str) -> str:
    """What an answer that holds no image says: its model's filter stopped it, or it made none."""
    if reason.startswith("Filter reason"):
        sentence = (
            f"{model_id}'s content filter stopped this image. Reword the prompt, then try again."
        )
    else:
        sentence = f"{model_id} made no image for this prompt. Try again."
    return sentence_with_detail(sentence, reason)


class BedrockImageProvider(ImageGenProvider):
    """Image generation via Bedrock ``invoke_model``: each model :data:`_IMAGE_MODELS` names that
    the instance's region lists, called the way AWS documents it.

    boto3 is lazily imported inside methods (Property 11), and the blocking ``invoke_model`` call
    runs in a thread pool.
    """

    def __init__(self, *, region: str = DEFAULT_REGION, profile: str | None = None,
                 name: str = "bedrock") -> None:
        self._region = region or DEFAULT_REGION
        self._profile = profile
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @name.setter
    def name(self, value: str) -> None:
        self._name = value

    @property
    def display_name(self) -> str:
        return "Amazon Bedrock (image)"

    def _get_client(self):
        import boto3  # noqa: PLC0415

        session = boto3.Session(profile_name=self._profile) if self._profile else boto3.Session()
        return session.client("bedrock-runtime", region_name=self._region)

    async def is_available(self) -> bool:
        """True when it can make an image: :meth:`unavailable_reason` has nothing to say."""
        return not await self.unavailable_reason()

    async def unavailable_reason(self) -> str:
        """Why images can't be made, as Settings → Models shows it under Image · Generation: why the
        AWS credential chain cannot sign in (the chat's sentence for the same failure), or that the
        region lists no model this app can make an image from a prompt with, else ``""``.

        A listing that fails says nothing here: :meth:`list_models` raises its failure, which is
        what the row says."""
        problem = await _creds_problem(self._region, self._profile)
        if problem:
            return problem
        try:
            offered = await self.list_models()
        except ModelDiscoveryError:
            return ""
        return "" if offered else _no_image_model(self._region)

    async def list_models(self) -> list[ImageGenModel]:
        """The region's models this app can make an image from a prompt with, each by the id a call
        names. Raises the listing's :class:`ModelDiscoveryError` when there is no listing."""
        rows = await _listed_in_region(self._region, self._profile)
        offered = []
        for row in rows:
            how = _image_model(row["id"])
            if how is None:
                continue
            offered.append(
                ImageGenModel(
                    name=row["id"],
                    description=f"{row.get('name') or row['id']} — makes an image from a prompt",
                    sizes=list(how.sizes),
                    supports_edit=False,
                )
            )
        return offered

    def _generate_sync(
        self, prompt: str, model_id: str, how: _ImageModel, size: str, n: int
    ) -> list[str]:
        """Blocking image generation, sent and read as ``how`` says — run via to_thread. Asks for
        ``n`` images, at most ``how.per_request`` in one request. Raises :class:`_NoImage` for an
        answer that holds none."""
        client = self._get_client()
        images: list[str] = []
        wanted = max(1, n)
        while len(images) < wanted:
            count = min(wanted - len(images), how.per_request)
            response = client.invoke_model(
                modelId=model_id,
                body=json.dumps(how.body(prompt, size, count)),
                contentType="application/json",
                accept="application/json",
            )
            made = how.images(json.loads(response["body"].read()))
            if not made:
                raise _NoImage("")
            images.extend(made)
        return images[:wanted]

    async def generate(
        self,
        prompt: str,
        *,
        model: str = "",
        size: str = "",
        n: int = 1,
        **opts: Any,
    ) -> list[ImageResult]:
        """Generate images from a text prompt with ``model``. A model this app has no text-to-image
        request for is refused before anything is sent."""
        try:
            model_id = require_model(model)
        except ProviderResolutionError as exc:
            raise ImageGenError(str(exc)) from exc
        what = f"Bedrock image generation on {self._name!r}"
        how = _image_model(model_id)
        if how is None:
            sentence = _not_an_image_model(model_id)
            _warn_once(what, sentence, ValueError(sentence))
            raise ImageGenError(sentence)
        try:
            images = await asyncio.to_thread(self._generate_sync, prompt, model_id, how, size, n)
        except _NoImage as exc:
            sentence = _made_no_image(model_id, str(exc))
            _warn_once(what, sentence, exc)
            raise ImageGenError(sentence) from exc
        except Exception as exc:
            sentence = _media_failure(
                "image generation", exc, model_id, region=self._region, profile=self._profile
            )
            _warn_once(what, sentence, exc)
            raise ImageGenError(sentence) from exc
        return [ImageResult(b64=image, mime="image/png") for image in images]

    async def edit(
        self,
        prompt: str,
        *,
        source_image: str,
        mask: str = "",
        model: str = "",
        size: str = "",
        n: int = 1,
        **opts: Any,
    ) -> list[ImageResult]:
        """No model this app calls edits an image: each makes a new one from a prompt."""
        raise ImageGenError(
            "This Amazon Bedrock instance makes new images from a prompt and edits none, so "
            "nothing was sent. Ask for a new image instead of an edit."
        )


# ── Bedrock Video Generation Provider ────────────────────────────────────────


#: The video models this app calls (a ``TEXT_VIDEO`` task through ``StartAsyncInvoke``), keyed by
#: id without its version, with the aspect ratios each makes: Amazon Nova Reel. Video · Generation
#: offers those the instance's region lists, as Image · Generation does its models, rather than
#: naming Nova Reel whatever the region.
_VIDEO_MODELS: dict[str, tuple[str, ...]] = {"amazon.nova-reel-v1": ("16:9",)}


def _not_a_video_model(model_id: str) -> str:
    """What a video on a model this app has no request for says."""
    return (
        f"Bedrock's {model_id} is not a model this app can make a video with, so nothing was sent. "
        "Choose one of the models Settings → Models lists under Video · Generation for this Amazon "
        "Bedrock instance."
    )


def _no_video_model(region: str) -> str:
    """What Video · Generation says for an instance whose region lists no Nova Reel model."""
    return (
        f"No video generation model is available in {region}: Amazon Bedrock lists no Amazon Nova "
        f"Reel model there, the one this app makes videos with. Set AWS Region {_ON_INSTANCE} to a "
        "region that lists it, or add an instance for that region."
    )


_VIDEO_POLL_INTERVAL = 10  # seconds
# 600s, not 300s: an async-invoke job still InProgress after 5 minutes has not
# failed — abandoning the poll reports FAILURE for a generation that completes (and
# is billed) moments later. The ceiling tracks the slowest legitimate job.
_VIDEO_POLL_TIMEOUT = 600  # seconds

#: The file a finished Nova Reel job writes its video to, in the job's own folder.
_VIDEO_FILE = "output.mp4"

#: What to do about each way AWS documents a Nova Reel job failing, told by the words of its
#: ``failureMessage``. Any other failure says :data:`_VIDEO_JOB_FAILED`, and its own words after.
_VIDEO_FAILURES: tuple[tuple[str, str], ...] = (
    (
        "blocked by our content filters",
        "Nova Reel's content filters blocked the video it made for this prompt. Reword the "
        "prompt, then try again.",
    ),
    (
        "capacity limit",
        "Nova Reel has reached its capacity limit for now. Wait a few minutes, then try again.",
    ),
    (
        "went wrong on the server side",
        "Something went wrong on AWS's side while Nova Reel made this video. Try again in a few "
        "minutes.",
    ),
    (
        "has been aborted",
        "The Nova Reel job for this video was stopped before it finished. Try again.",
    ),
)
_VIDEO_JOB_FAILED = (
    "Nova Reel couldn't generate this video. Fix what its reason names, then try again."
)


def _spoken_seconds(seconds: float) -> str:
    """A wait as a sentence says it: "10 minutes", "1 minute", "45 seconds"."""
    minutes, rest = divmod(int(seconds), 60)
    if minutes and not rest:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{int(seconds)} seconds"


def _video_job_failed(reason: str) -> str:
    """What a Nova Reel job that ended ``Failed`` says: what happened, what to do, and the reason
    Bedrock gave after it. A job AWS stops fails too, with "Request has been aborted."."""
    low = reason.lower()
    for words, sentence in _VIDEO_FAILURES:
        if words in low:
            return sentence_with_detail(sentence, reason)
    if not reason.strip():
        return "Nova Reel couldn't generate this video, and gave no reason. Try again."
    return sentence_with_detail(_VIDEO_JOB_FAILED, reason)


def _job_folder(status: dict[str, Any]) -> str:
    """The S3 folder a Nova Reel job writes into, as its own status names it
    (``outputDataConfig.s3OutputDataConfig.s3Uri``). Bedrock makes one for each job under the
    prefix it was asked for, named for the job, so no path built here would find the video.
    "" when the status names none."""
    config = (status.get("outputDataConfig") or {}).get("s3OutputDataConfig") or {}
    return str(config.get("s3Uri") or "").rstrip("/")


def _video_download_failed(error: Exception, *, uri: str, folder: str) -> str:
    """What a finished job's video that could not be downloaded says, and where to look for it."""
    code = _aws_error_code(error)
    if code in ("404", "NoSuchKey", "NotFound"):
        sentence = (
            f"Nova Reel said this video was finished, but there is no video at {uri}. Look in "
            f"the job's folder, {folder}/, in the S3 console; if it isn't there, generate the "
            "video again."
        )
    elif code in ("403", "AccessDenied", "Forbidden"):
        sentence = (
            f"Nova Reel finished this video, but the identity Bedrock signs in as may not read "
            f"{uri} (s3:GetObject). Add that action to its IAM policy, or get the video from "
            "there in the S3 console."
        )
    else:
        sentence = (
            f"Nova Reel finished this video, but it couldn't be downloaded from {uri}. Get it "
            "from there, in the S3 console for example."
        )
    return sentence_with_detail(sentence, error)


class BedrockVideoProvider(VideoGenProvider):
    """Video generation via Bedrock async invoke (Nova Reel).

    ``generate()`` performs the full submit → poll → download cycle:
    1. ``start_async_invoke`` submits the generation job
    2. ``get_async_invoke`` polls until ``status == 'Completed'``
    3. Download the MP4 from the job's own folder, which its status names

    Requires the instance's S3 Bucket (``video_s3_bucket``, which speech-to-text uses too) or the
    ``BEDROCK_VIDEO_S3_BUCKET`` env var.
    """

    def __init__(
        self,
        *,
        region: str = DEFAULT_REGION,
        profile: str | None = None,
        s3_bucket: str = "",
        name: str = "bedrock",
    ) -> None:
        self._region = region or DEFAULT_REGION
        self._profile = profile
        self._s3_bucket = s3_bucket or os.environ.get("BEDROCK_VIDEO_S3_BUCKET", "")
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @name.setter
    def name(self, value: str) -> None:
        self._name = value

    @property
    def display_name(self) -> str:
        return "Amazon Bedrock (video)"

    def _get_runtime_client(self):
        import boto3  # noqa: PLC0415

        session = boto3.Session(profile_name=self._profile) if self._profile else boto3.Session()
        return session.client("bedrock-runtime", region_name=self._region)

    def _get_s3_client(self):
        import boto3  # noqa: PLC0415

        session = boto3.Session(profile_name=self._profile) if self._profile else boto3.Session()
        return session.client("s3", region_name=self._region)

    async def is_available(self) -> bool:
        """True when it can make a video: :meth:`unavailable_reason` has nothing to say."""
        return not await self.unavailable_reason()

    async def unavailable_reason(self) -> str:
        """Why video can't be made, as Settings → Models shows it: no S3 bucket for Nova Reel to
        write to, why the AWS credential chain cannot sign in, or that the region lists no Nova Reel
        model, else ``""``. A listing that fails is :meth:`list_models`'s to say."""
        if not self._s3_bucket:
            return _needs_bucket(_VIDEO_NEEDS_BUCKET)
        problem = await _creds_problem(self._region, self._profile)
        if problem:
            return problem
        try:
            offered = await self.list_models()
        except ModelDiscoveryError:
            return ""
        return "" if offered else _no_video_model(self._region)

    async def list_models(self) -> list[VideoGenModel]:
        """The region's Nova Reel models, each by the id a call names. Raises the listing's
        :class:`ModelDiscoveryError` when there is no listing."""
        rows = await _listed_in_region(self._region, self._profile)
        offered = []
        for row in rows:
            ratios = _by_family(_VIDEO_MODELS, row["id"])
            if ratios is None:
                continue
            offered.append(
                VideoGenModel(
                    name=row["id"],
                    description=f"{row.get('name') or row['id']} — makes a video from a prompt",
                    aspect_ratios=list(ratios),
                    max_duration_s=6,
                )
            )
        return offered

    def _generate_sync(self, prompt: str, model_id: str, duration_seconds: float) -> str:
        """Blocking submit → poll → download. Returns local file path to the MP4."""
        if not self._s3_bucket:
            raise VideoGenError(_needs_bucket(_VIDEO_NEEDS_BUCKET))

        client = self._get_runtime_client()
        duration = max(6, min(int(duration_seconds), 6))  # Nova Reel supports 6s clips

        # The prefix the job's own folder goes under: Bedrock names that folder for the job and
        # says which in the job's status (:func:`_job_folder`), so the video's path is read from
        # there, never built here.
        s3_uri = f"s3://{self._s3_bucket}/bedrock-video/{int(_time.time())}"

        model_input = {
            "taskType": "TEXT_VIDEO",
            "textToVideoParams": {"text": prompt},
            "videoGenerationConfig": {
                "durationSeconds": duration,
                "fps": 24,
                "dimension": "1280x720",
            },
        }

        # Submit async invoke
        response = client.start_async_invoke(
            modelId=model_id,
            modelInput=model_input,
            outputDataConfig={"s3OutputDataConfig": {"s3Uri": s3_uri}},
        )
        invocation_arn = response["invocationArn"]

        # Poll until completed or timeout. A job is InProgress, Completed or Failed (the service
        # model's whole set); one AWS stops is Failed.
        status_resp: dict[str, Any] = {}
        start = _time.monotonic()
        while (_time.monotonic() - start) < _VIDEO_POLL_TIMEOUT:
            _time.sleep(_VIDEO_POLL_INTERVAL)
            status_resp = client.get_async_invoke(invocationArn=invocation_arn)
            status = status_resp.get("status", "")
            if status == "Completed":
                break
            if status == "Failed":
                raise VideoGenError(_video_job_failed(str(status_resp.get("failureMessage") or "")))
        else:
            # The job runs on in AWS, and one that finishes writes its video and is billed, so
            # the step is to look for it before asking for another.
            folder = _job_folder(status_resp)
            where = f"to {folder}/{_VIDEO_FILE}" if folder else f"into a folder under {s3_uri}/"
            raise VideoGenError(
                f"Nova Reel hadn't finished this video after {_spoken_seconds(_VIDEO_POLL_TIMEOUT)}"
                ", so video generation stopped waiting for it. The job may still finish and write "
                f"the video {where}: look there before you try again."
            )
        return self._download(_job_folder(status_resp), requested=s3_uri)

    def _download(self, folder: str, *, requested: str) -> str:
        """Download the video a finished job wrote into ``folder`` — its own, as its status names
        it — to a local file, and return that file's path."""
        parts = urlsplit(folder)
        if parts.scheme != "s3" or not parts.netloc:
            raise VideoGenError(
                "Nova Reel finished this video, but its status didn't say where it wrote it. Look "
                f"for it in a folder under {requested}/ in the S3 console."
            )
        key = "/".join(part for part in (parts.path.strip("/"), _VIDEO_FILE) if part)
        uri = f"s3://{parts.netloc}/{key}"
        local_path = os.path.join(tempfile.gettempdir(), f"bedrock_video_{int(_time.time())}.mp4")
        try:
            self._get_s3_client().download_file(parts.netloc, key, local_path)
        except Exception as exc:  # noqa: BLE001 — the video was made; say where it is
            raise VideoGenError(_video_download_failed(exc, uri=uri, folder=folder)) from exc
        return local_path

    async def generate(
        self,
        prompt: str,
        *,
        model: str = "",
        duration_seconds: float = 5.0,
        aspect_ratio: str = "",
        **opts: Any,
    ) -> list[VideoResult]:
        """Generate a video from a text prompt with ``model`` (Nova Reel, async invoke). Any other
        model is refused before anything is sent."""
        try:
            model_id = require_model(model)
        except ProviderResolutionError as exc:
            raise VideoGenError(str(exc)) from exc
        if _by_family(_VIDEO_MODELS, model_id) is None:
            raise VideoGenError(_not_a_video_model(model_id))
        try:
            local_path = await asyncio.to_thread(
                self._generate_sync, prompt, model_id, duration_seconds
            )
        except VideoGenError:
            raise
        except Exception as exc:
            sentence = _media_failure(
                "video generation", exc, model_id, region=self._region, profile=self._profile
            )
            _warn_once(f"Bedrock video generation on {self._name!r}", sentence, exc)
            raise VideoGenError(sentence) from exc

        return [VideoResult(local_path=local_path, mime="video/mp4", duration_s=6.0)]


# ── Bedrock STT Provider (Amazon Transcribe) ─────────────────────────────────

# The STT capability uses Amazon Transcribe — the purpose-built AWS speech-to-text
# service — NOT Voxtral (which is an audio-modality LLM that hallucinates).
# Voxtral is correctly classified as an audio_modality model (understands audio
# in chat context) — a different use-case from deterministic transcription.
#
# Amazon Transcribe works via a batch job: upload audio to S3 → start_transcription_job
# → poll → download transcript JSON. For short clips (< 30s, the composer mic path),
# this completes in 3-8 seconds. No hallucination, handles all formats natively.
#: The id a speech-to-text binding names for it (``Bedrock:amazon-transcribe``), listed by the
#: catalog so Settings → Models can offer it. Transcribe has one model, so the adapter needs no
#: more than that the binding names it.
TRANSCRIBE_MODEL = "amazon-transcribe"

#: How often a Transcribe job is asked whether it has finished, and how many times before
#: speech-to-text stops waiting (short clips, the composer's microphone, finish in 3-8 seconds).
_STT_POLL_INTERVAL = 1.5
_STT_POLL_TRIES = 40

#: The IAM action each call speech-to-text makes needs, by the AWS operation that answered. A
#: large recording is uploaded in parts, and every part needs PutObject's action.
_STT_ACTIONS = {
    "PutObject": "s3:PutObject",
    "CreateMultipartUpload": "s3:PutObject",
    "UploadPart": "s3:PutObject",
    "CompleteMultipartUpload": "s3:PutObject",
    "StartTranscriptionJob": "transcribe:StartTranscriptionJob",
    "GetTranscriptionJob": "transcribe:GetTranscriptionJob",
}
#: The codes S3 answers, beside the ones Bedrock does, when it does not accept the credentials.
_S3_REJECTED_CREDENTIAL_CODES = frozenset({"InvalidAccessKeyId", "InvalidToken"})


def _stt_failure(error: Exception, *, bucket: str, region: str, profile: str | None) -> str:
    """What a transcription that failed says: the AWS setup, when that is what failed (the
    chat's sentence for it); a bucket that isn't there; the IAM action the identity lacks;
    credentials AWS turned down — else that it failed, what to do, and the SDK's words after it.

    Not :func:`_media_failure`: speech-to-text calls S3 and Amazon Transcribe, where a refusal is
    about the bucket or Transcribe, never about access to a Bedrock model."""
    setup = _aws_setup_problem(error, region=region, profile=profile)
    if setup is not None:
        return setup
    code = _aws_error_code(error)
    if code == "NoSuchBucket":
        sentence = (
            f"The S3 bucket '{bucket}' that speech-to-text uploads each recording to doesn't "
            f"exist. Create it in {region}, or set S3 Bucket {_ON_INSTANCE} (under Advanced) to "
            "one you have, then try again."
        )
    elif code in ("AccessDenied", "AccessDeniedException"):
        named = _NOT_AUTHORIZED_RE.search(str(error))
        action = named["action"] if named else _STT_ACTIONS.get(_aws_operation(error), "")
        on = f" on the S3 bucket '{bucket}'" if action.startswith("s3:") else ""
        sentence = (
            f"Your AWS credentials aren't allowed to call {action}{on}, which speech-to-text "
            "needs. Add that action to the IAM policy of the identity Bedrock signs in as, then "
            "try again."
            if action
            else f"AWS refused a call speech-to-text makes ({code}). It needs s3:PutObject and "
            f"s3:GetObject on the S3 bucket '{bucket}', and transcribe:StartTranscriptionJob and "
            "transcribe:GetTranscriptionJob: add them to the IAM policy of the identity Bedrock "
            "signs in as, then try again."
        )
    elif (
        code in _REJECTED_CREDENTIAL_CODES
        or code in _S3_REJECTED_CREDENTIAL_CODES
        or "security token included in the request is" in str(error)
    ):
        sentence = (
            "AWS turned down the credentials speech-to-text signed in with: they are invalid or "
            "have expired. Sign in to AWS again (`aws sso login` for an SSO profile), then try "
            "again."
        )
    else:
        sentence = f"Amazon Transcribe couldn't transcribe this audio. {_SEE_THE_LOG}"
    return sentence_with_detail(sentence, error)


def _transcribe_job_failed(reason: str) -> str:
    """What a Transcribe job that ended FAILED says: that it failed, what to do, and the reason
    Transcribe gave after it."""
    if not reason.strip():
        return "Amazon Transcribe couldn't transcribe this audio, and gave no reason. Try again."
    return sentence_with_detail(
        "Amazon Transcribe couldn't transcribe this audio. Fix what its reason names, then try "
        "again.",
        reason,
    )


class BedrockSTTProvider(SttProvider):
    """Speech-to-text via Amazon Transcribe (the real AWS transcription service).

    Uploads the audio to the instance's S3 Bucket — the one video generation writes to
    (``video_s3_bucket``, or the ``BEDROCK_VIDEO_S3_BUCKET`` env var), so speech-to-text needs
    it set too — runs a Transcribe job, returns the verbatim transcript, and deletes the upload.
    Deterministic — no hallucination, no prompt engineering needed.
    Handles wav, mp3, mp4, flac, ogg, webm natively.
    """

    def __init__(self, *, region: str = DEFAULT_REGION, profile: str | None = None,
                 name: str = "bedrock", s3_bucket: str = "") -> None:
        self._region = region or DEFAULT_REGION
        self._profile = profile
        self._name = name
        self._s3_bucket = s3_bucket or os.environ.get("BEDROCK_VIDEO_S3_BUCKET", "")

    @property
    def name(self) -> str:
        return self._name

    @name.setter
    def name(self, value: str) -> None:
        self._name = value

    @property
    def display_name(self) -> str:
        return "Amazon Transcribe"

    def _get_session(self):
        import boto3  # noqa: PLC0415
        return boto3.Session(profile_name=self._profile) if self._profile else boto3.Session()

    async def is_available(self) -> bool:
        """True if the AWS credential chain resolves AND an S3 bucket is set."""
        return bool(self._s3_bucket) and await _creds_ok(self._region, self._profile)

    async def unavailable_reason(self) -> str:
        """Why speech-to-text is unavailable: no S3 bucket to upload each recording to, or why
        the AWS credential chain cannot sign in (the chat's sentence for the same failure)."""
        if not self._s3_bucket:
            return _needs_bucket(_STT_NEEDS_BUCKET)
        return await _creds_problem(self._region, self._profile)

    def _transcribe_sync(self, audio_path: str, model: str, language: str) -> str:
        """Blocking: upload → start job → poll → fetch transcript. Via to_thread.

        A job Transcribe failed, or did not finish in time, raises ``SttError`` saying so."""
        import urllib.request
        import uuid

        session = self._get_session()
        s3 = session.client("s3", region_name=self._region)
        transcribe = session.client("transcribe", region_name=self._region)

        # Determine media format from extension
        ext = os.path.splitext(audio_path)[1].lower().lstrip(".") or "wav"
        format_map = {"webm": "webm", "ogg": "ogg", "mp3": "mp3",
                      "mp4": "mp4", "m4a": "mp4", "flac": "flac", "wav": "wav"}
        media_format = format_map.get(ext, "wav")

        # Upload to S3
        s3_key = f"stt-transcribe/{uuid.uuid4().hex}.{ext}"
        s3.upload_file(audio_path, self._s3_bucket, s3_key)
        s3_uri = f"s3://{self._s3_bucket}/{s3_key}"

        job_name = f"pclaw-stt-{uuid.uuid4().hex[:12]}"
        try:
            # Start transcription job
            job_kwargs: dict[str, Any] = {
                "TranscriptionJobName": job_name,
                "Media": {"MediaFileUri": s3_uri},
                "MediaFormat": media_format,
            }
            if language:
                job_kwargs["LanguageCode"] = language
            else:
                job_kwargs["IdentifyLanguage"] = True

            transcribe.start_transcription_job(**job_kwargs)

            # Poll for completion (short clips finish in 3-8s)
            for _ in range(_STT_POLL_TRIES):
                _time.sleep(_STT_POLL_INTERVAL)
                status = transcribe.get_transcription_job(
                    TranscriptionJobName=job_name
                )
                st = status["TranscriptionJob"]["TranscriptionJobStatus"]
                if st == "COMPLETED":
                    uri = status["TranscriptionJob"]["Transcript"]["TranscriptFileUri"]
                    result = json.loads(urllib.request.urlopen(uri, timeout=10).read())
                    return result["results"]["transcripts"][0]["transcript"]
                elif st == "FAILED":
                    reason = str(status["TranscriptionJob"].get("FailureReason") or "")
                    raise SttError(_transcribe_job_failed(reason))
            waited = _spoken_seconds(_STT_POLL_INTERVAL * _STT_POLL_TRIES)
            raise SttError(
                f"Amazon Transcribe hadn't finished this audio after {waited}, so speech-to-text "
                "stopped waiting for it. Try again; if it keeps happening, try a shorter recording."
            )
        finally:
            # Cleanup: delete S3 object + transcription job
            try:
                s3.delete_object(Bucket=self._s3_bucket, Key=s3_key)
            except Exception:
                pass
            try:
                transcribe.delete_transcription_job(TranscriptionJobName=job_name)
            except Exception:
                pass

    async def transcribe(self, audio_path: str, model: str = "", language: str = "") -> str | None:
        """Transcribe an audio file via Amazon Transcribe, for a call that names its model.

        Its text, empty when there was no speech. A transcription that could not run raises the
        SDK's ``SttError`` with what failed and what to do — no bucket, the AWS sign-in, the
        bucket or an IAM action, a job Transcribe failed or did not finish — so a failure is never
        read as audio with no speech in it. A call that names no model is refused before anything
        is sent, as every media call is (``None``, and the SDK's sentence logged)."""
        try:
            require_model(model)
        except ProviderResolutionError as exc:
            logger.error("Amazon Transcribe on %r refused: %s", self._name, exc)
            return None
        if not self._s3_bucket:
            raise SttError(_needs_bucket(_STT_NEEDS_BUCKET))
        try:
            return await asyncio.to_thread(self._transcribe_sync, audio_path, model, language)
        except SttError:
            raise
        except Exception as exc:
            sentence = _stt_failure(
                exc, bucket=self._s3_bucket, region=self._region, profile=self._profile
            )
            _warn_once(f"Amazon Transcribe on {self._name!r}", sentence, exc)
            raise SttError(sentence) from exc

    async def transcribe_detailed(
        self,
        audio_path: str,
        *,
        model: str = "",
        language: str = "",
        bias_terms: list[str] | None = None,
    ) -> TranscriptResult | None:
        """Detailed transcription (wraps flat transcribe — no segment support). Fails as
        :meth:`transcribe` does."""
        text = await self.transcribe(audio_path, model=model, language=language)
        return TranscriptResult(text=text) if text is not None else None


# ── Media-capability config scanners ─────────────────────────────────────────
#
# One Bedrock config.json entry (a single AWS profile + region) serves EVERY
# use-case Bedrock offers. Chat resolves through the LLM registry (register_type
# above). The media capabilities (embedding / image / video / STT) resolve
# through their OWN registries, which build a per-config adapter. Core knows the
# OpenAI-family built-in; Bedrock contributes its adapters via the app-owned
# ``media_scanners`` extension point — one scanner per capability, registered on
# import. Each scanner receives the config provider entries and returns a Bedrock
# adapter for each entry whose ``type`` is ``bedrock`` (or a branded alias
# collapsing to it), keyed by that entry's name so ``<name>:model`` refs resolve
# to the same AWS account that backs the entry's chat.


def _bedrock_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The config provider entries this app owns (type ``bedrock``)."""
    out = []
    for e in entries:
        ptype = str(e.get("type", ""))
        # canonical + branded-alias tolerance (the config sync may stamp _original_type)
        if ptype == "bedrock" or str((e.get("options") or {}).get("_original_type", "")) == "bedrock":
            out.append(e)
    return out


def _entry_region(e: dict[str, Any]) -> str:
    return str((e.get("options") or {}).get("region", "") or DEFAULT_REGION)


def _entry_profile(e: dict[str, Any]) -> str | None:
    p = (e.get("options") or {}).get("profile")
    return str(p) if p else None


def _scan_embedding(entries: list[dict[str, Any]]) -> list:
    # Key each adapter by the config entry name (not the generic "bedrock") so a
    # ``<name>:model`` binding resolves to the same AWS account backing the chat.
    return [
        BedrockEmbeddingProvider(
            region=_entry_region(e), profile=_entry_profile(e), name=str(e["name"]),
        )
        for e in _bedrock_entries(entries)
    ]


def _scan_image(entries: list[dict[str, Any]]) -> list:
    return [
        BedrockImageProvider(
            region=_entry_region(e), profile=_entry_profile(e), name=str(e["name"]),
        )
        for e in _bedrock_entries(entries)
    ]


def _scan_video(entries: list[dict[str, Any]]) -> list:
    out = []
    for e in _bedrock_entries(entries):
        bucket = str((e.get("options") or {}).get("video_s3_bucket", "")
                     or os.environ.get("BEDROCK_VIDEO_S3_BUCKET", ""))
        out.append(BedrockVideoProvider(
            region=_entry_region(e), profile=_entry_profile(e),
            s3_bucket=bucket, name=str(e["name"]),
        ))
    return out


def _scan_stt(entries: list[dict[str, Any]]) -> list:
    out = []
    for e in _bedrock_entries(entries):
        bucket = str((e.get("options") or {}).get("video_s3_bucket", "")
                     or os.environ.get("BEDROCK_VIDEO_S3_BUCKET", ""))
        out.append(BedrockSTTProvider(
            region=_entry_region(e), profile=_entry_profile(e),
            name=str(e["name"]), s3_bucket=bucket,
        ))
    return out


try:
    from personalclaw.sdk.model import register_scanner as _reg_scanner

    _reg_scanner("embedding", _scan_embedding)
    _reg_scanner("image_gen", _scan_image)
    _reg_scanner("video_gen", _scan_video)
    _reg_scanner("stt", _scan_stt)
except Exception:  # noqa: BLE001 — older core without the extension point
    logger.debug("media_scanners extension point unavailable", exc_info=True)
