"""The typed loopback contract for the browser connector, and its loopback rail.

This is the single source of truth for the connector's contract; the extension's
``extension/contract.js`` mirrors it verbatim and ``test_contract.py`` asserts the two do
not drift. The gateway drives the operator's own browser through a deliberately NARROW,
CLOSED vocabulary — ``navigate`` / ``read-outline`` / ``click`` / ``type`` / ``close`` —
carried over the page-target endpoint of a tab the run opened for itself. A wider surface is
a wider blast radius on a session the operator is already logged into, so the vocabulary is
closed by construction: a verb outside it is refused, never guessed.

Every request is ADDRESSED to one run, by the id core gave that run's tab, and the extension
acts on that run's own tab and nothing else, never on whichever tab happens to have focus. A
run asks for its tab on :func:`run_tabs_url`; the extension announces the tab it opened, or
reports the tab's end, on :func:`run_tab_url`, with one of :data:`RUN_TAB_REPORTS`.

Two loopback rules make "writing cdp_url over LOOPBACK_INTERNAL only, no new listener" true
in the bundle rather than merely promised:

* :func:`announce_payload` refuses to build the write unless the announced ``cdp_url`` is a
  **loopback ws(s)** endpoint, so a public endpoint can never leave the bundle; and
* :func:`announce_url` (and the run-tab routes built on it) refuses to target anything but a
  **loopback** gateway.

The connector opens no listening socket of its own — it makes outbound loopback requests to
the gateway and drives the browser's own debugger transport — so these two rules plus the
extension manifest's loopback-only host permissions are the whole network surface.

Pure stdlib, and it imports nothing from ``personalclaw`` — the SDK boundary the apps repo
enforces is satisfied by not crossing it at all.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

#: The CLOSED typed-contract vocabulary. Exactly these five verbs are the contract; anything
#: else is refused. Kept in declaration order so the extension's mirror can be compared 1:1.
CONTRACT_METHODS: tuple[str, ...] = ("navigate", "read-outline", "click", "type", "close")

#: The required parameters for each verb — a closed shape per verb, the same idea as core's
#: sentinel action vocabulary. ``read-outline`` and ``close`` take none; ``click`` names an
#: element ref; ``type`` names a ref and the value to enter; ``navigate`` names a url.
REQUIRED_PARAMS: dict[str, tuple[str, ...]] = {
    "navigate": ("url",),
    "read-outline": (),
    "click": ("ref",),
    "type": ("ref", "value"),
    "close": (),
}


#: What the extension may report about a run's tab once core has asked for one: it could not
#: open a tab of the run's own, the person closed it (the tab, its group or its window), or the
#: person brought it to the front to take over. Core stops the run on the first two and pauses it
#: on the third.
RUN_TAB_REPORTS: tuple[str, ...] = ("unavailable", "closed", "taken_over")

_RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


class ContractError(ValueError):
    """A message that is not a valid typed-contract request."""


def is_run_id(value: Any) -> bool:
    """Whether *value* is a run id: core's token for one run's tab, which rides in a route path
    and so is held to a closed alphabet."""
    return isinstance(value, str) and _RUN_ID.fullmatch(value) is not None


@dataclass(frozen=True)
class ContractRequest:
    """One typed request for the tab one run opened for itself."""

    run: str
    method: str
    params: dict[str, Any] = field(default_factory=dict)

    def to_message(self) -> dict[str, Any]:
        """The wire form: ``{"run": ..., "method": ..., "params": {...}}``."""
        return {"run": self.run, "method": self.method, "params": dict(self.params)}


def build_request(method: str, *, run: str, **params: Any) -> ContractRequest:
    """Build a validated request, or raise :class:`ContractError`.

    A request that names no run is refused: the extension acts only in a tab a run opened for
    itself, so a verb with no run has no tab it may touch. A method outside
    :data:`CONTRACT_METHODS` is refused rather than passed through — the same reason core's
    ``BROWSE_TARGETS`` is a closed vocabulary: a typo must not become an action on the
    operator's live session.
    """
    if not is_run_id(run):
        raise ContractError("a contract request must name the run whose tab it acts on")
    if method not in CONTRACT_METHODS:
        raise ContractError(
            f"unknown contract method {method!r}; the vocabulary is {list(CONTRACT_METHODS)}"
        )
    missing = [p for p in REQUIRED_PARAMS[method] if not str(params.get(p, "")).strip()]
    if missing:
        raise ContractError(f"{method!r} is missing required param(s): {missing}")
    return ContractRequest(run=run, method=method, params=dict(params))


def parse_request(message: Any) -> ContractRequest:
    """Parse an inbound wire message into a validated :class:`ContractRequest`."""
    if not isinstance(message, dict):
        raise ContractError("a contract message must be a JSON object")
    params = message.get("params") or {}
    if not isinstance(params, dict):
        raise ContractError("`params` must be an object")
    return build_request(str(message.get("method") or ""), run=message.get("run"), **params)


# ── the loopback rail ───────────────────────────────────────────────────────────


def is_loopback_host(host: str) -> bool:
    """Whether *host* is a loopback address (127.0.0.0/8, ::1) or a localhost name."""
    if not host:
        return False
    stripped = host.strip("[]")
    if stripped == "localhost" or stripped.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(stripped).is_loopback
    except ValueError:
        return False


def is_loopback_ws_url(url: str) -> bool:
    """A CDP page-target endpoint must be a ``ws``/``wss`` URL on a loopback host."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in ("ws", "wss") and is_loopback_host(parts.hostname or "")


def is_loopback_http_url(url: str) -> bool:
    """The gateway base URL announced TO must itself be a loopback ``http``/``https`` URL."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and is_loopback_host(parts.hostname or "")


def announce_payload(cdp_url: str) -> dict[str, str]:
    """The POST body that announces a run's own tab on :func:`run_tab_url`.

    Refuses a non-loopback endpoint, so a public ``cdp_url`` can never leave the bundle even
    if a page or a misconfiguration supplied one — the loopback guarantee is enforced where
    the write is built, not merely documented.
    """
    value = (cdp_url or "").strip()
    if not is_loopback_ws_url(value):
        raise ContractError(
            f"cdp_url must be a loopback ws(s) page-target endpoint, got {cdp_url!r}"
        )
    return {"cdp_url": value}


def announce_url(gateway_base_url: str) -> str:
    """The route that attaches the browser as the connector, refusing a non-loopback gateway.

    The connector talks to the LOCAL gateway only; announcing a cdp_url to a remote gateway
    would ship an endpoint reference off-box, which this refuses outright. Attaching names no
    page: a page target is announced only for a tab a granted run asked for.
    """
    base = (gateway_base_url or "").rstrip("/")
    if not is_loopback_http_url(base):
        raise ContractError(
            f"the connector announces to a loopback gateway only, got {gateway_base_url!r}"
        )
    return f"{base}/api/browse/connector"


def run_tabs_url(gateway_base_url: str) -> str:
    """The route that lists the runs which asked the attached browser for a tab of their own."""
    return f"{announce_url(gateway_base_url)}/tabs"


def run_tab_url(gateway_base_url: str, run_id: str) -> str:
    """The route one run's tab is announced and reported on. Refuses anything but a run id."""
    if not is_run_id(run_id):
        raise ContractError(f"not a run id: {run_id!r}")
    return f"{run_tabs_url(gateway_base_url)}/{run_id}"
