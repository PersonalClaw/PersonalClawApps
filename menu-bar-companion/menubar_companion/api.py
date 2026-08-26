"""The gateway HTTP surface this companion reads and writes. stdlib only.

Three calls, and they are the whole contract:

* ``GET  /api/loops``                     → ``{"loops": [...]}``
* ``GET  /api/approvals``                 → ``[...]`` (a bare JSON array)
* ``POST /api/approvals/{id}/{action}``   → ``{"ok": true}``

Auth rides the query string as ``?token=`` — that is the only owner-auth path the
gateway's token middleware honours (an ``Authorization: Bearer`` header is the
app-token NARROWING path and does not authenticate on its own).

Every request also carries an explicit ``Origin`` equal to the configured base URL's
own origin. The gateway CSRF-checks state-changing requests against an allowlist that
contains that origin by construction, so this is what a browser pointed at the same
URL would send — not a widening.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

#: The label the user reads → the action the gateway accepts. The gateway's pair is
#: ``approve``/``reject`` (``handlers/sessions.api_approval_resolve`` 400s on anything
#: else), while the human word for the second one is "Deny". Keeping the mapping in one
#: dict is what stops a "deny" from being POSTed at a route that only knows "reject".
WIRE_ACTION = {"approve": "approve", "deny": "reject"}


class GatewayError(RuntimeError):
    """A call to the gateway did not succeed. Carries a sentence fit for a menu."""


@dataclass(frozen=True)
class ResolveOutcome:
    """The result of an Approve/Deny write.

    A write is the one place this app changes the world, so its failure is a first
    class value rather than an exception swallowed at the call site: ``ok=False``
    always carries a non-empty ``error``, and the caller is expected to SHOW it.
    """

    ok: bool
    approval_id: str
    action: str
    error: str = ""

    def __post_init__(self) -> None:
        if not self.ok and not self.error:  # pragma: no cover - construction guard
            raise ValueError("a failed ResolveOutcome must carry an error to show")


def _origin_of(base_url: str) -> str:
    parts = urllib.parse.urlsplit(base_url)
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


class GatewayClient:
    """A thin authenticated client for one gateway."""

    def __init__(self, base_url: str, token: str, timeout: float = 10.0, opener=None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        # Injectable so tests drive the real request-building code against a fake
        # transport instead of asserting on a mock of this class.
        self._opener = opener or urllib.request.urlopen

    # ── URLs ──

    def url(self, path: str, **query: str) -> str:
        q = {"token": self.token, **{k: v for k, v in query.items() if v}}
        sep = "&" if "?" in path else "?"
        return f"{self.base_url}{path}{sep}{urllib.parse.urlencode(q)}"

    def socket_url(self) -> str:
        """``/api/ws`` as a ``ws://``/``wss://`` URL with the token attached."""
        http_url = self.url("/api/ws")
        if http_url.startswith("https://"):
            return "wss://" + http_url[len("https://") :]
        return "ws://" + http_url[len("http://") :]

    def origin(self) -> str:
        return _origin_of(self.base_url)

    def deep_link(self, loop_id: str) -> str:
        """The dashboard deep link for a loop that needs input.

        ``#/loops/<id>`` is a real routable deep link (core ``web/src/app/App.tsx``
        keeps ``loops`` in ``ROUTABLE`` precisely so these survive). The token goes in
        the query string, BEFORE the fragment, so a browser that has never talked to
        this gateway still lands authenticated instead of on the token prompt.
        """
        return f"{self.base_url}/?{urllib.parse.urlencode({'token': self.token})}#/loops/{loop_id}"

    # ── requests ──

    def _request(self, path: str, method: str = "GET"):
        req = urllib.request.Request(  # noqa: S310 - scheme comes from user config
            self.url(path),
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": self.origin(),
            },
        )
        try:
            with self._opener(req, timeout=self.timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                payload = json.loads(exc.read().decode("utf-8", "replace"))
                detail = str(payload.get("error", "")).strip()
            except Exception:  # noqa: BLE001 - the body is best-effort context only
                detail = ""
            raise GatewayError(
                f"{method} {path} failed: HTTP {exc.code}{f' — {detail}' if detail else ''}"
            ) from exc
        except urllib.error.URLError as exc:
            raise GatewayError(f"{method} {path} failed: {exc.reason}") from exc
        except OSError as exc:
            raise GatewayError(f"{method} {path} failed: {exc}") from exc
        if not body:
            return None
        try:
            return json.loads(body.decode("utf-8", "replace"))
        except ValueError as exc:
            raise GatewayError(f"{method} {path} returned a non-JSON body") from exc

    # ── the three calls ──

    def get_loops(self) -> list[dict]:
        payload = self._request("/api/loops")
        loops = (payload or {}).get("loops") if isinstance(payload, dict) else None
        return [row for row in (loops or []) if isinstance(row, dict)]

    def get_approvals(self) -> list[dict]:
        payload = self._request("/api/approvals")
        # This endpoint returns a BARE ARRAY, not an envelope. Tolerating both would
        # hide the day it changes; assert the shape we were built against.
        if not isinstance(payload, list):
            raise GatewayError("GET /api/approvals did not return a JSON array")
        return [row for row in payload if isinstance(row, dict)]

    def resolve_approval(self, approval_id: str, action: str) -> ResolveOutcome:
        """Approve or deny one pending approval.

        Returns an outcome instead of raising: the caller must render the failure on
        the very surface the click happened on. It deliberately does NOT mutate any
        local state — the truth comes from the next ``GET``, so a POST that failed can
        never leave a row reading as decided.
        """
        wire = WIRE_ACTION.get(action)
        if wire is None:
            return ResolveOutcome(
                ok=False,
                approval_id=approval_id,
                action=action,
                error=f"unknown approval action {action!r}",
            )
        try:
            self._request(f"/api/approvals/{urllib.parse.quote(approval_id)}/{wire}", method="POST")
        except GatewayError as exc:
            return ResolveOutcome(
                ok=False,
                approval_id=approval_id,
                action=action,
                error=f"Could not {action} {approval_id}: {exc}",
            )
        return ResolveOutcome(ok=True, approval_id=approval_id, action=action)
