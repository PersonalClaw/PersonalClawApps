"""Slack workspace origin binding.

Pins the gateway to the single workspace its bot token belongs to, so a
hot-swapped ``.env`` token can't redirect the bot to another workspace and
exfiltrate data. Two layers:

1. ``validate_enterprise()`` when inbound starts — calls ``auth.test`` and caches
   the workspace's ``team_id``. Succeeds for ANY workspace (personal or
   Enterprise Grid). Its answer tells a refused token (Slack answered, and said
   no) from a Slack it could not reach (no network yet, a timeout, Slack busy),
   because only the first is about the token: the transport tries the second
   again, and until a check succeeds no message is accepted.
2. ``check_message_origin()`` on every incoming message — compares the event's
   ``team`` field against the cached value (zero-cost in-memory check).

NOTE: an earlier Enterprise-Grid *requirement* (reject any workspace without an
``enterprise_id``) has been removed — a personal Slack workspace is a first-class,
supported deployment. The origin binding below is the deployment-neutral protection
that actually matters, and it is retained.
"""

import logging
import re
from dataclasses import dataclass

from slack_sdk.errors import SlackApiError

from personalclaw.sdk.channel import sel

logger = logging.getLogger(__name__)

# Cached at startup by validate_enterprise().  Checked per-message by
# check_message_origin().  Module-level — safe because the gateway runs
# in a single asyncio event loop.
_validated_team_id: str = ""

#: What a workspace check can conclude.
VALIDATED = "validated"
#: Slack answered and would not bind this workspace: the token is refused, or the answer names no
#: workspace. Asking again gets the same answer, so nothing retries it.
REJECTED = "rejected"
#: Slack could not be asked, or answered that it could not answer now (rate limited, an outage).
#: Nothing was learned about the token, so the transport asks again later.
UNREACHABLE = "unreachable"

#: Slack's own error codes that are about Slack right now, not about the token.
_TRANSIENT_ERRORS = frozenset(
    {"ratelimited", "internal_error", "fatal_error", "service_unavailable", "request_timeout"}
)
#: The shape of one of Slack's error codes; slack_sdk's own sentence about an answer it could not
#: read has spaces in it.
_SLACK_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]*")


@dataclass(frozen=True)
class WorkspaceCheck:
    """What one ``auth.test`` concluded, and the sentence the channel card shows for it.

    ``reason`` is ``""`` once validated. It names the failure (Slack's error code, or how the
    connection failed) and never a token.
    """

    outcome: str
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome == VALIDATED


def _connection_failure(exc: BaseException) -> str:
    """How a request that never got an answer failed, in a few words: "Connection refused"."""
    cause = getattr(exc, "reason", None)  # urllib's URLError wraps the socket's error
    err = cause if isinstance(cause, BaseException) else exc
    if isinstance(err, TimeoutError):
        return "timed out"
    words = getattr(err, "strerror", None) or (cause if isinstance(cause, str) else "")
    return str(words or type(err).__name__)


def _refused(exc: SlackApiError) -> WorkspaceCheck:
    """A check that got an answer with an error: a refusal only when it is Slack's own answer.

    Slack answers a token it refuses with HTTP 200 and its error code (``invalid_auth``). Anything
    else says nothing about the token: Slack busy (429, a 5xx, one of its transient codes), or an
    answer that is not Slack's API at all, which slack_sdk reports in a sentence of its own ("a
    non-JSON format: 404"), from a proxy's page or an address that is not Slack's.
    """
    response = getattr(exc, "response", None)
    status = int(getattr(response, "status_code", 0) or 0)
    try:
        code = str(response["error"] or "")  # type: ignore[index]
    except (KeyError, TypeError, ValueError):  # no JSON body at all
        code = ""
    slacks_own = bool(_SLACK_ERROR_CODE.fullmatch(code))
    if status in (0, 200) and slacks_own and code not in _TRANSIENT_ERRORS:
        return WorkspaceCheck(
            REJECTED,
            f"Slack refused the Bot Token ({code}). Re-check it in Settings → Providers → Slack "
            "Channel.",
        )
    said = code if slacks_own else f"HTTP {status}" if status else "an answer that is not Slack's"
    return WorkspaceCheck(
        UNREACHABLE,
        f"Slack could not check the Bot Token right now ({said}), so the workspace is not "
        "confirmed yet.",
    )


def _failed(check: WorkspaceCheck) -> WorkspaceCheck:
    """Log and audit a check that did not validate, then return it."""
    # The sentence, not a traceback: it repeats on every retry while the network is down.
    log = logger.error if check.outcome == REJECTED else logger.warning
    log("Slack workspace validation failed: %s", check.reason)
    sel().log_api_access(
        caller="gateway",
        operation="slack.workspace_validation",
        outcome="denied" if check.outcome == REJECTED else "error",
        source="startup",
        error="auth_test_refused" if check.outcome == REJECTED else "slack_unreachable",
    )
    return check


def validate_enterprise(bot_token: str) -> WorkspaceCheck:
    """Call ``auth.test`` and bind the gateway to the token's workspace.

    Caches the workspace ``team_id`` so ``check_message_origin()`` can verify each incoming
    message without an API call. Succeeds for any workspace whose token authenticates: the
    boundary is the one workspace the bot token belongs to, checked on every message, and no
    list of organisations narrows it. A failure says which kind it was
    (:class:`WorkspaceCheck`): ``REJECTED`` when Slack answered and said no, ``UNREACHABLE``
    when it could not be asked or could not answer now.
    """
    global _validated_team_id
    from slack_sdk.web import WebClient

    # Clear stale state so a failed re-validation is fail-closed.
    _validated_team_id = ""

    try:
        client = WebClient(token=bot_token)
        resp = client.auth_test()
    except SlackApiError as exc:
        return _failed(_refused(exc))
    except Exception as exc:  # noqa: BLE001 — no answer at all: the network, DNS, a timeout
        return _failed(
            WorkspaceCheck(
                UNREACHABLE,
                f"Slack could not be reached to check the Bot Token ({_connection_failure(exc)}).",
            )
        )

    enterprise_id = resp.get("enterprise_id", "")
    team_id = resp.get("team_id", "")
    team = resp.get("team", "")
    team_id_str = team_id or ""

    if not team_id_str:
        # No team_id from auth.test — can't bind origin, so fail closed.
        logger.error("Slack workspace validation: auth.test returned no team_id")
        sel().log_api_access(
            caller="gateway",
            operation="slack.workspace_validation",
            outcome="denied",
            source="startup",
            resources=f"team={team}",
            error="no_team_id",
        )
        return WorkspaceCheck(
            REJECTED,
            "Slack's answer to the workspace check named no workspace, so this gateway cannot "
            "bind to one. Re-check the Bot Token in Settings → Providers → Slack Channel.",
        )

    # Bind to this workspace for per-message origin checks.
    _validated_team_id = team_id_str

    logger.info(
        "Slack workspace validated: team=%s team_id=%s%s",
        team,
        team_id_str,
        f" enterprise_id={enterprise_id}" if enterprise_id else " (personal workspace)",
    )
    sel().log_api_access(
        caller="gateway",
        operation="slack.workspace_validation",
        outcome="allowed",
        source="startup",
        resources=f"team={team} team_id={team_id_str} enterprise_id={enterprise_id or 'none'}",
    )
    return WorkspaceCheck(VALIDATED)


def check_message_origin(event_team_id: str) -> bool:
    """Verify an incoming message's team_id matches the validated workspace.

    Zero-cost in-memory comparison — no API call.  Returns True if the
    message is from the validated workspace, False otherwise.

    If no team_id was cached (validation didn't run or failed), rejects
    all messages (fail-closed).
    """
    if not _validated_team_id:
        return False
    if not event_team_id:
        return False
    return event_team_id == _validated_team_id
