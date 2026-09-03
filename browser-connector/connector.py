"""The typed loopback contract for the browser connector (BA-8), and its loopback rail.

This is the single source of truth for the connector's contract; the extension's
``extension/contract.js`` mirrors it verbatim and ``test_contract.py`` asserts the two do
not drift. The gateway drives the operator's own browser through a deliberately NARROW,
CLOSED vocabulary — ``navigate`` / ``read-outline`` / ``click`` / ``type`` / ``close`` —
carried over a CDP page-target endpoint the browser exposes on loopback. A wider surface is
a wider blast radius on a session the operator is already logged into, so the vocabulary is
closed by construction: a verb outside it is refused, never guessed.

Two loopback rules make "writing cdp_url over LOOPBACK_INTERNAL only, no new listener" true
in the bundle rather than merely promised:

* :func:`announce_payload` refuses to build the write unless the announced ``cdp_url`` is a
  **loopback ws(s)** endpoint, so a public endpoint can never leave the bundle; and
* :func:`announce_url` refuses to target anything but a **loopback** gateway.

The connector opens no listening socket of its own — it makes outbound loopback requests to
the gateway and drives the browser's own debugger transport — so these two rules plus the
extension manifest's loopback-only host permissions are the whole network surface.

Pure stdlib, and it imports nothing from ``personalclaw`` — the SDK boundary the apps repo
enforces is satisfied by not crossing it at all.
"""

from __future__ import annotations

import ipaddress
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


class ContractError(ValueError):
    """A message that is not a valid typed-contract request."""


@dataclass(frozen=True)
class ContractRequest:
    """One typed request the gateway issues to the connected browser."""

    method: str
    params: dict[str, Any] = field(default_factory=dict)

    def to_message(self) -> dict[str, Any]:
        """The wire form: ``{"method": ..., "params": {...}}``."""
        return {"method": self.method, "params": dict(self.params)}


def build_request(method: str, **params: Any) -> ContractRequest:
    """Build a validated request, or raise :class:`ContractError`.

    A method outside :data:`CONTRACT_METHODS` is refused rather than passed through — the
    same reason core's ``BROWSE_TARGETS`` is a closed vocabulary: a typo must not become an
    action on the operator's live session.
    """
    if method not in CONTRACT_METHODS:
        raise ContractError(
            f"unknown contract method {method!r}; the vocabulary is {list(CONTRACT_METHODS)}"
        )
    missing = [p for p in REQUIRED_PARAMS[method] if not str(params.get(p, "")).strip()]
    if missing:
        raise ContractError(f"{method!r} is missing required param(s): {missing}")
    return ContractRequest(method=method, params=dict(params))


def parse_request(message: Any) -> ContractRequest:
    """Parse an inbound wire message into a validated :class:`ContractRequest`."""
    if not isinstance(message, dict):
        raise ContractError("a contract message must be a JSON object")
    params = message.get("params") or {}
    if not isinstance(params, dict):
        raise ContractError("`params` must be an object")
    return build_request(str(message.get("method") or ""), **params)


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
    """The POST body the extension sends to ``/api/browse/connector``.

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
    """The gateway route the extension writes to, refusing a non-loopback gateway.

    The connector talks to the LOCAL gateway only; announcing a cdp_url to a remote gateway
    would ship an endpoint reference off-box, which this refuses outright.
    """
    base = (gateway_base_url or "").rstrip("/")
    if not is_loopback_http_url(base):
        raise ContractError(
            f"the connector announces to a loopback gateway only, got {gateway_base_url!r}"
        )
    return f"{base}/api/browse/connector"
