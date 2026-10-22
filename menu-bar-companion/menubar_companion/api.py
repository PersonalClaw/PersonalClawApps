"""The gateway HTTP surface this companion reads and writes. stdlib only.

Three calls, and they are the whole contract:

* ``GET  /api/waiting``                   → ``{"waiting": [...], "unreadable": [...]}``
* ``GET  /api/running``                   → ``{"running": [...], "finished": [...]}``
* ``POST /api/approvals/{id}/{action}``   → ``{"ok": true}``

The two lists are core's (``personalclaw.waiting``, ``personalclaw.running_now``): everything
waiting on the owner, of every kind, and everything running, each with what it is doing. They are
the core features ``waiting-list`` and ``running-list`` the manifest requires.

The owner token rides the ``Authorization: Bearer`` header, on these three calls and on the
``/api/ws`` upgrade (``doorbell.handshake``). That is the gateway's carrier for a client that is
not a browser: it sets no cookie and binds no address, it is judged against the whole lifetime of
the session (a ``?token=`` is a browser's sign-in link, refused once its link window has passed),
and it keeps the token out of the URL, where every log that records a request line would keep it.

No URL built here carries the token. Every page the menu opens in a browser, where an item is
answered and an approval to review, opens the dashboard with no credential in it: the browser signs
in the way it always does there, with the session it already holds, else on the gateway's own
sign-in page, which keeps the page it was opened at.

Every request also carries an explicit ``Origin`` equal to the configured base URL's
own origin. The gateway CSRF-checks state-changing requests against an allowlist that
contains that origin by construction, so this is what a browser pointed at the same
URL would send — not a widening.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

#: The label the user reads → the action the gateway accepts. The gateway's pair is
#: ``approve``/``reject`` (``handlers/sessions.api_approval_resolve`` 400s on anything
#: else), while the human word for the second one is "Deny". Keeping the mapping in one
#: dict is what stops a "deny" from being POSTed at a route that only knows "reject".
WIRE_ACTION = {"approve": "approve", "deny": "reject"}

#: The dashboard route a list names for an item (``#/loops/<id>``, ``#/chat/<key>``): a hash route
#: of URL-safe characters only, so a link built from one stays a page of this gateway's dashboard.
DASHBOARD_ROUTE = re.compile(r"#/[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*")

#: Where a link goes when the list names no route it can open: Home, whose Needs You lists
#: everything waiting on you.
HOME = "#/"


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


def bearer(token: str) -> str:
    """The ``Authorization`` header value that carries *token*.

    A PersonalClaw token is one word of printable ASCII. Anything else (a space, a comma, a line
    break, a character outside ASCII) raises :class:`GatewayError` before a request is built: the
    gateway refuses such a Bearer anyway, and a line break would end the header it rides in.
    """
    word = token.strip()
    if not word or any(not "!" <= ch <= "~" or ch == "," for ch in word):
        raise GatewayError(
            "the configured token is not a PersonalClaw token; configure the token from the "
            "link `personalclaw token` prints"
        )
    return f"Bearer {word}"


class GatewayClient:
    """A thin authenticated client for one gateway."""

    def __init__(self, base_url: str, token: str, timeout: float = 10.0, opener=None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        # Injectable so tests drive the real request-building code against a fake
        # transport instead of asserting on a mock of this class.
        self._opener = opener or urllib.request.urlopen

    # ── URLs: none of them carries the token ──

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def socket_url(self) -> str:
        """``/api/ws`` as a ``ws://``/``wss://`` URL. The upgrade carries the token's header."""
        http_url = self.url("/api/ws")
        if http_url.startswith("https://"):
            return "wss://" + http_url[len("https://") :]
        return "ws://" + http_url[len("http://") :]

    def origin(self) -> str:
        return _origin_of(self.base_url)

    def link(self, route: object) -> str:
        """The dashboard page at *route*, the route a list names for an item or a piece of work.

        Anything that is not a dashboard route (none, another site, a line break) opens Home
        instead. It carries no credential: a browser without a session signs in on the gateway's
        sign-in page, which lands it on this route afterwards.
        """
        shown = route if isinstance(route, str) and DASHBOARD_ROUTE.fullmatch(route) else HOME
        return f"{self.base_url}/{shown}"

    def review_link(self, approval_id: str) -> str:
        """Where an approval is reviewed whole and answered: ``#/companion?approval=<id>``.

        The dashboard's approvals page shows every pending approval with all of its arguments,
        and ``?approval=`` brings this one into view (or says it was already answered). No
        credential in it, for the same reason as :meth:`link`.
        """
        if not approval_id:
            return f"{self.base_url}/#/companion"
        return f"{self.base_url}/#/companion?approval={urllib.parse.quote(approval_id, safe='')}"

    # ── requests ──

    def _request(self, path: str, method: str = "GET"):
        req = urllib.request.Request(  # noqa: S310 - scheme comes from user config
            self.url(path),
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": self.origin(),
                "Authorization": bearer(self.token),
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

    def get_waiting(self) -> tuple[list[dict], list[str]]:
        """Everything waiting on the owner, in the list's order, and the kinds it could not read.

        The shape is asserted, not tolerated: a body that is not the list is a failed read, so it
        is said as one rather than shown as nothing waiting.
        """
        payload = self._request("/api/waiting")
        if not isinstance(payload, dict) or not isinstance(payload.get("waiting"), list):
            raise GatewayError("GET /api/waiting did not return the waiting list")
        unreadable = payload.get("unreadable")
        return (
            [row for row in payload["waiting"] if isinstance(row, dict)],
            [str(kind) for kind in unreadable] if isinstance(unreadable, list) else [],
        )

    def get_running(self) -> list[dict]:
        """Everything running now, each row saying what its work is doing."""
        payload = self._request("/api/running")
        if not isinstance(payload, dict) or not isinstance(payload.get("running"), list):
            raise GatewayError("GET /api/running did not return the running list")
        return [row for row in payload["running"] if isinstance(row, dict)]

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
