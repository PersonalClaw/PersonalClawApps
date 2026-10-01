"""SlackTransport — the ChannelTransportProvider that owns the Slack channel.

Outbound + health/test are always available (token-gated). Inbound is driven by
:meth:`start_inbound`, which the gateway calls with a
:class:`~personalclaw.gateway_services.GatewayServices` handle whenever it starts this
channel's receiver — at boot, and when the channel is turned on, updated or its
settings are saved (it stops the previous instance's receiver first): the transport
builds a :class:`SlackRuntime`, wires the Socket-Mode receiver + interactive
handlers (which live in this bundle), and connects. A Slack it cannot reach as it
starts (the network not up yet, Slack busy) is tried again with backoff for as long
as the receiver runs; only Slack refusing a token turns inbound off, and the card
says which token.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import math
import re
import sys as _sys
from pathlib import Path as _Path
from typing import Any

# The app loader only keeps this app's dir on sys.path while it execs the entry
# module. The Slack integration is a multi-module package whose modules import
# each other (some lazily, to break cycles) throughout the process lifetime — the
# socket receiver, delivery, and interaction handlers all resolve ``slack_runtime.*``
# long after boot. Pin the app dir on sys.path for the life of the process so those
# imports keep resolving (a real installed package would be permanently importable).
_APP_DIR = str(_Path(__file__).resolve().parents[1])
if _APP_DIR not in _sys.path:
    _sys.path.insert(0, _APP_DIR)

from personalclaw.sdk.channel import (
    ChannelCapabilities,
    ChannelTransportProvider,
    OutboundMessage,
    fence_channel_content,
)

# Import ALL runtime deps at MODULE level (not lazily in start_inbound): the app
# loader only keeps this app's dir on sys.path while it execs this module, so a
# ``from slack_runtime.X import`` inside a method runs LATER — when the dir is off
# the path — and fails with "No module named 'slack_runtime'". Binding them here,
# during exec, captures them for the life of the transport instance.
from slack_runtime.client import RealSlackClient
from slack_runtime.delivery import SlackDelivery
from slack_runtime.enterprise import REJECTED, UNREACHABLE
from slack_runtime.events import SeenCache, bind_workspace, check_workspace, init_socket_mode
from slack_runtime.handler import get_owner_id
from slack_runtime.interactions import init as init_interactions
from slack_runtime.runtime import SlackRuntime
from slack_runtime.settings import LiveConfig, adopt_owner_id, load_tokens
from slack_runtime.writes import SendRefused, live_writes_disabled

#: A Slack conversation id: C (channel), D (DM), G (private group) or W (enterprise channel), then
#: upper-case letters and digits.
_CONVERSATION_RE = re.compile(r"[CDGW][A-Z0-9]+")

#: Seconds inbound waits before trying again after Slack could not be reached as it started:
#: doubling from five seconds to five minutes, then every five minutes while the gateway runs.
_RETRY_DELAYS = (5, 10, 20, 40, 80, 160, 300)

#: What one Socket Mode connect concluded, beside ``enterprise``'s REJECTED / UNREACHABLE.
_CONNECTED = "connected"

# NOT ``__name__``: the app loader execs this ENTRY module under a synthetic name
# (``_pclaw_app_slack_channel__slack_runtime_transport``), so ``__name__`` produced a
# logger outside the ``slack_runtime`` root this app declares in ``app.json``
# ``loggerRoots`` — the level the operator sets never reached the transport, so its
# diagnostics were unreachable at ANY verbosity. #952 called the offline notice "an INFO
# line invisible at the default WARNING log level"; measured, it was invisible at DEBUG
# too. Every other module in this bundle is imported as ``slack_runtime.X`` and is fine.
logger = logging.getLogger("slack_runtime.transport")


def fence_untrusted_inbound(text: str, sender_id: str, *, trusted: bool) -> str:
    """Fence untrusted NON-OWNER inbound content as DATA before it reaches the agent.

    CHANNEL-EXPANSION T1.4 / CE-6 — the channel conformance kit's ``[fencing]`` clause.
    A non-owner's chat text is untrusted input, never instructions: on Slack's direct
    inbound path (``handler.handle_message``) it MUST reach the model wrapped in the
    platform's untrusted-content fence — the SAME fence core's guarded door hands the
    sibling channels as ``verdict.fenced_text``
    (``fence_channel_content(text, provider, sender)``). It is applied HERE, at the
    transport that owns Slack's inbound path, so the handler cannot forget it — and so
    the fence a channel produces has a consumer this bundle can point to.

    A *trusted* sender passes through unfenced — the owner, an explicitly-allowlisted
    user, or a trusted bot — exactly as core exempts ``is_allowed_sender``: fencing the
    owner's own request would make the agent read it as inert data it must not act on.
    Mirrors core's ``verdict.fenced_text or msg.text``: the fenced form for an untrusted
    sender, the raw text otherwise (an empty message is returned unchanged — nothing to
    fence). The caller decides trust; keeping that decision out of here leaves this a
    pure function of ``(text, sender_id, trusted)``.
    """
    if trusted or not text:
        return text
    fenced_text = fence_channel_content(text, "slack", sender_id)
    return fenced_text


class SlackTransport(ChannelTransportProvider):
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        # The config, kept live: every token read goes through it, so a Configure → Save is seen
        # by the next Test/Connect/health/send rather than at the next restart. ``start_inbound``
        # hands the same config to the runtime, so the INBOUND half resolves its tokens from the
        # same place the outbound half does (#952).
        self._config = LiveConfig(config if config is not None else {})
        self._runtime: SlackRuntime | None = None
        #: The ``(bot, app)`` tokens the receiver was started with — ``None`` until the gateway
        #: started it. Socket Mode keeps the tokens it started with, so this is what tells
        #: ``health`` that tokens saved since have not reached inbound.
        self._inbound_tokens: tuple[str, str] | None = None
        #: True once the Socket-Mode receiver is actually connected. ``health()`` needs the
        #: three-way distinction — connected / tried-and-failed / never driven — because a
        #: config save re-cycles this provider and builds a FRESH transport, which is "never
        #: driven" until the gateway starts its receiver (and for good in a process that runs
        #: no receivers, a CLI), so it is a real, reportable state.
        self._inbound_started: bool = False
        #: Why inbound is not running, when it was driven and failed (``""`` otherwise).
        #: ``health()``/``test()`` report it, so the provider row can no longer show a
        #: green "Tokens configured" over a receiver that never started (#952).
        self._inbound_offline_reason: str = ""
        #: The task still trying to bring inbound online after Slack could not be reached as it
        #: started, and the loop time of its next try (both ``None`` while nothing is waiting).
        self._retry: asyncio.Task | None = None
        self._retry_at: float | None = None

    def capabilities(self) -> ChannelCapabilities:
        # groups: a channel the bot is in crosses the door with is_dm=False (only a D… id is a DM).
        return ChannelCapabilities(
            inbound=True, threads=True, attachments=True, reactions=True,
            edits=True, rich_text=True, typing_indicator=True, max_text_len=40000, groups=True,
        )

    @property
    def name(self) -> str:
        return "slack"

    @property
    def display_name(self) -> str:
        return "Slack"

    def validate_target(self, target: str) -> str:
        """Whether a schedule can send its results to ``target`` on Slack.

        A conversation id: a public channel (``C…``), a DM (``D…``), a private group (``G…``) or
        an enterprise-wide channel (``W…``). This rule used to be core's, for every channel.
        """
        if _CONVERSATION_RE.fullmatch(str(target or "").strip()):
            return ""
        return (
            "A Slack channel id starts with C, D, G or W, like C0123456789. It's at the bottom of "
            "the channel's About tab."
        )

    def _tokens(self) -> tuple[str, str]:
        """``(bot_token, app_token)`` as configured now."""
        return load_tokens(self._config.current())

    async def connect(self) -> bool:
        return bool(self._tokens()[0])

    async def disconnect(self) -> None:
        return None

    # ── Inbound: the gateway starts and stops this with the channel ──
    async def start_inbound(self, services: Any) -> None:
        """Build the Slack runtime, wire the socket receiver, connect (or keep trying)."""
        # Before the runtime reads its owner, and before the token check, so a Slack given its
        # tokens later keeps its owner too instead of starting in first-contact claim mode.
        adopt_owner_id()
        # Pass this transport's own config through: without it the runtime re-derived the
        # tokens from core's credential store alone and a dashboard-configured install
        # never started inbound (#952).
        runtime = SlackRuntime(services, config=self._config.current())
        self._inbound_tokens = (runtime._bot_token, runtime._app_token)
        if not runtime._slack_enabled:
            missing = " and ".join(
                n for n, tok in (("Bot Token", runtime._bot_token), ("App Token", runtime._app_token))
                if not tok
            )
            self._inbound_offline_reason = (
                f"no {missing} — Socket Mode needs both. Set them in "
                "Settings → Providers → Slack Channel."
            )
            # WARNING, not INFO: this is the whole reason a correctly-credentialled-looking
            # Slack install answers nothing, and it was previously below the default level.
            logger.warning("SlackTransport: inbound stays offline — %s", self._inbound_offline_reason)
            return
        runtime.slack = RealSlackClient(runtime._bot_token)
        self._runtime = runtime

        init_interactions(runtime)
        seen = SeenCache()
        check = init_socket_mode(runtime, seen)

        if runtime._socket_client is None:
            # The workspace check did not validate (init_socket_mode logs why). A refused token
            # stays refused. A Slack that could not be reached, because the network is not up yet
            # or Slack is busy, says nothing about the token, so it is asked again, with backoff,
            # for as long as this receiver runs; no message is accepted meanwhile.
            self._inbound_offline_reason = check.reason if check is not None else ""
            if check is not None and check.outcome == UNREACHABLE:
                self._retry_later(runtime, seen, services)
            return

        self._attach_delivery(runtime, services)
        if await self._connect(runtime) == UNREACHABLE:
            self._retry_later(runtime, seen, services)

    def _retry_later(self, runtime: SlackRuntime, seen: SeenCache, services: Any) -> None:
        """Start :meth:`_keep_trying`, with the time of its first try already known to health."""
        self._retry_at = asyncio.get_running_loop().time() + _RETRY_DELAYS[0]
        self._retry = asyncio.create_task(self._keep_trying(runtime, seen, services))

    def _attach_delivery(self, runtime: SlackRuntime, services: Any) -> None:
        """Hand core this channel's outbound delivery, once the workspace is validated."""
        # Core delivers through this ONE provider-agnostic ChannelDelivery handle (text,
        # attachments, streaming, identity lookups, approvals) — it never sees the Slack client.
        # Filed under "slack", the name core reads this channel's owner by
        # (``owner_id_for("slack")``). The delivery reads the owner each time it asks
        # (``get_owner_id``), so one claimed by the first person to message a fresh install is
        # the one it prompts, before any restart.
        delivery = SlackDelivery(runtime.slack, get_owner_id)
        if hasattr(services, "register_channel_delivery"):
            services.register_channel_delivery(delivery, provider="slack")
        if getattr(services, "dashboard_state", None) is not None:
            services.dashboard_state.channel_delivery = delivery

        # Register this channel's observe-mode channels on the core history buffer
        # (per-channel activation is APP config; channel_history stays generic).
        try:
            from slack_runtime.settings import ACTIVATION_OBSERVE, get_settings

            _ch_hist = getattr(services, "channel_history", None)
            if _ch_hist is not None:
                for _cid, _ccfg in get_settings().channels.items():
                    if _ccfg.activation == ACTIVATION_OBSERVE:
                        _ch_hist.set_observe(_cid)
        except Exception:
            logger.debug("observe-channel registration failed", exc_info=True)

    async def _connect(self, runtime: SlackRuntime) -> str:
        """Connect Socket Mode once: ``_CONNECTED``, ``REJECTED`` (Slack refuses the App Token)
        or ``UNREACHABLE`` (anything else, which is tried again)."""
        socket = runtime._socket_client
        try:
            await socket.connect()
        except Exception as e:  # noqa: BLE001 — resilience: never crash the gateway
            # Slack's own error code when it refused, else the failure's type — never the
            # exception's text, which can carry the socket URL and its one-time ticket.
            error = str(getattr(socket, "connect_error", "") or "") or type(e).__name__
            if error in _APP_TOKEN_REFUSALS:
                logger.error("Slack Socket-Mode connect refused (%s) — Slack offline", error)
                runtime._slack_enabled = False
                self._inbound_offline_reason = (
                    f"Slack refuses the App Token ({error}). Save a working App Token in "
                    "Configure; that connects it again."
                )
                return REJECTED
            logger.warning("Slack Socket-Mode connect failed (%s) — trying again", error)
            self._inbound_offline_reason = f"Socket Mode could not connect ({error})."
            return UNREACHABLE
        logger.info("SlackTransport: Socket-Mode connected")
        self._inbound_offline_reason = ""
        self._inbound_started = True
        return _CONNECTED

    async def _keep_trying(self, runtime: SlackRuntime, seen: SeenCache, services: Any) -> None:
        """Bring inbound online after Slack could not be reached as it started.

        Asks again after each of :data:`_RETRY_DELAYS`: the workspace check first, while the
        workspace is not validated, then the Socket Mode connect. Stops at the first success,
        and at a refusal, which asking again would not change. Cancelled by ``stop_inbound``.
        """
        loop = asyncio.get_running_loop()
        try:
            for delay in itertools.chain(_RETRY_DELAYS, itertools.repeat(_RETRY_DELAYS[-1])):
                self._retry_at = loop.time() + delay
                await asyncio.sleep(delay)
                self._retry_at = None
                if runtime._socket_client is None:
                    # ``auth.test`` blocks for as long as the network takes to fail, so it runs
                    # off the event loop the rest of the gateway shares.
                    check = bind_workspace(
                        runtime, seen, await asyncio.to_thread(check_workspace, runtime)
                    )
                    if check.outcome == REJECTED:
                        self._inbound_offline_reason = check.reason
                        return
                    if runtime._socket_client is None:
                        self._inbound_offline_reason = check.reason
                        continue
                    self._attach_delivery(runtime, services)
                if await self._connect(runtime) != UNREACHABLE:
                    return
        finally:
            self._retry_at = None

    def _next_try(self) -> str:
        """When inbound tries again, for the channel card: "Trying again in 20 seconds." """
        if self._retry_at is None:
            return "Trying again now."
        wait = math.ceil(self._retry_at - asyncio.get_running_loop().time())
        if wait <= 0:
            return "Trying again now."
        return f"Trying again in {wait} second{'s' if wait != 1 else ''}."

    async def stop_inbound(self) -> None:
        retry, self._retry = self._retry, None
        if retry is not None and not retry.done():
            retry.cancel()
            await asyncio.gather(retry, return_exceptions=True)
        rt = self._runtime
        if rt is not None and rt._socket_client is not None:
            try:
                await asyncio.wait_for(rt._socket_client.close(), timeout=1.0)
            except Exception:
                logger.debug("SlackTransport: socket close timed out", exc_info=True)

    async def send(self, message: OutboundMessage) -> bool | SendRefused:
        """Transmit one outbound message. ``True`` delivered, ``False`` failed, or a
        :class:`SendRefused` when the platform's live-writes kill switch is on.

        The refusal is checked AFTER the token gate on purpose: an unconfigured
        transport could not have written anything, so reporting "refused" there would
        claim the guard suppressed a write that was never possible. Only a transport
        that WOULD have transmitted reports a refusal.
        """
        bot_token, _ = self._tokens()
        if not bot_token:
            return False
        # DISABLE_LIVE_WRITES. A chat.postMessage is a live, outward,
        # instantly-human-visible write — the same class core refuses for non-GET egress
        # and model deletion. Typed refusal, never a silent no-op: a test (or an
        # operator) asserting a send must be able to see that the guard, not the
        # network, stopped it. Checked BEFORE the client is resolved so a suppressed
        # send opens no connection at all.
        if live_writes_disabled():
            # ``self.name`` rather than a new module constant: this bundle already has
            # exactly one source of truth for the channel's name, and a second spelling
            # of "slack" is a drift seam for no gain.
            refusal = SendRefused(channel=self.name, target=message.channel_id)
            logger.warning("SlackTransport.send refused: %s", refusal)
            return refusal
        try:
            # The receiver's client carries the bot token inbound started with; reuse it unless
            # the configured token has moved on since, so a rotated token is what sends.
            reuse = (
                self._runtime is not None
                and self._runtime.slack is not None
                and (self._inbound_tokens is None or self._inbound_tokens[0] == bot_token)
            )
            client = self._runtime.slack if reuse else RealSlackClient(bot_token)  # type: ignore[union-attr]
            await client.post_message(
                channel=message.channel_id,
                text=message.text,
                thread_ts=message.thread_id or None,
            )
            return True
        except Exception as e:
            logger.warning("SlackTransport.send failed: %s", e)
            return False

    @property
    def connected(self) -> bool:
        return bool(self._tokens()[0])

    async def health(self) -> dict[str, Any]:
        """Readiness for the Channels page / provider row.

        Reports the INBOUND half too (#952). It used to answer "ready — Tokens configured"
        off the bot token alone, which is exactly true and exactly useless: the two signals
        an operator trusts (a green provider row and "connected to Slack") both described
        outbound while the receiver was dead. ``error`` rather than ``offline`` because
        outbound genuinely works — the channel is half-up, not down.

        Tokens are read as configured NOW, and the receiver is compared against the tokens it
        was started with: saved tokens reach outbound at once but inbound only at the next
        start, and a boot-time reason ("no Bot Token") must not outlive the token it was about.
        """
        tokens = self._tokens()
        if not tokens[0]:
            return {"state": "offline", "detail": "No bot token configured"}
        # Whether Socket Mode is connected NOW (``None``: this instance holds no socket client).
        # Connecting once is not staying connected: the SDK reconnects on its own and only logs
        # a reconnect that fails, so the flag set at the first connect cannot answer this.
        socket = self._runtime._socket_client if self._runtime is not None else None
        connected = None if socket is None else await socket.is_connected()
        if self._inbound_tokens is not None and self._inbound_tokens != tokens:
            # Tokens saved in the running gateway replace this instance and its receiver; ones
            # changed outside it are what this reports.
            if self._inbound_started and connected is not False:
                detail = (
                    "Outbound uses the saved tokens; Socket Mode is still connected with the "
                    "ones it started with. Configure → Save, or turning the channel off and on, "
                    "moves inbound onto the saved tokens."
                )
            else:
                detail = (
                    "Outbound ready, inbound OFFLINE — the Socket-Mode receiver was started "
                    "before these tokens were saved. Configure → Save, or turning the channel "
                    "off and on, starts it on them."
                )
            return {"state": "error", "detail": detail}
        if self._inbound_started:
            if connected is False:
                return {"state": "error", "detail": _socket_down(socket)}
            return {"state": "ready", "detail": "Tokens configured, Socket-Mode connected"}
        if self._inbound_offline_reason:
            detail = f"Outbound ready, inbound OFFLINE — {self._inbound_offline_reason}"
            if self._retry is not None and not self._retry.done():
                detail = f"{detail} {self._next_try()}"
            return {"state": "error", "detail": detail}
        return {
            "state": "error",
            "detail": (
                "Outbound ready, inbound NOT STARTED — the gateway starts the Socket-Mode "
                "receiver when it turns the channel on, and Configure → Save starts it now."
            ),
        }

    async def test(self) -> dict[str, Any]:
        bot_token, _ = self._tokens()
        if not bot_token:
            return {"ok": False, "detail": "No bot token configured"}
        try:
            client = RealSlackClient(bot_token)
            res = await client.auth_test()
            team = (res or {}).get("team") or (res or {}).get("team_id") or "workspace"
        except Exception as e:
            return {"ok": False, "detail": f"auth.test failed: {e}"}
        # Agree with health(): the channel contract requires test() to be not-ok whenever
        # health() is not "ready", or the owner gets a green Test on a channel that cannot
        # hear them. Derived from health() rather than re-deciding, so the two cannot drift.
        h = await self.health()
        if h["state"] != "ready":
            return {"ok": False, "detail": f"Authenticated to {team}, but {h['detail']}"}
        return {"ok": True, "detail": f"Authenticated to {team}"}


#: What ``apps.connections.open`` answers when Slack will not take the App Token.
_APP_TOKEN_REFUSALS = frozenset(
    {
        "invalid_auth",
        "not_authed",
        "token_revoked",
        "token_expired",
        "account_inactive",
        "not_allowed_token_type",
    }
)


def _socket_down(socket: Any) -> str:
    """Why inbound is down while Socket Mode is not connected, from what the socket client kept.

    ``socket`` is the runtime's :class:`~slack_runtime.events.SocketModeReceiver`: its
    ``connect_error`` is Slack's answer to the last reconnect (or the failure's type), and its
    ``ping_interval`` is how often the SDK tries again."""
    error = socket.connect_error
    every = f"every {socket.ping_interval:g} seconds"
    if error in _APP_TOKEN_REFUSALS:
        return (
            f"Inbound OFFLINE — Socket Mode is not connected, and Slack refuses the App Token "
            f"({error}) each time it reconnects. Save a working App Token in Configure; that "
            "connects it again."
        )
    if error:
        return (
            f"Inbound OFFLINE — Socket Mode is not connected, and reconnecting fails ({error}). "
            f"The Slack SDK keeps trying {every}."
        )
    return (
        "Inbound OFFLINE — Socket Mode is not connected. The Slack SDK reconnects on its own, "
        f"trying {every}."
    )


def create_provider(config: dict[str, Any] | None = None) -> "SlackTransport":
    return SlackTransport(config)
