"""The owner's network settings, and a stand-in for a provider's host, for an app's egress tests.

Settings → Security → Network egress keeps the owner's hosts in ``config.json`` under
``security.egress``: a host on Denied hosts is never reached, and a host on Allowed hosts is
reached even when it resolves to this machine or a private network. An app's request honours
them only when it is made under ``personalclaw.sdk.net.egress_policy_for(profile)``. Under the
bare profile (``policy=CONNECTOR``) the guard judges the request by the profile alone, so a host
the owner denied is reached anyway.

An app proves it here with its real request, through the SDK's real ``fetch`` and guard, to a
server on ``127.0.0.1`` that stands in for the provider's host. The guard refuses a loopback
address unless the owner allowed it, so the one server shows both halves: Allowed hosts lets the
request through, and Denied hosts refuses it. Nothing here leaves this machine. A refused request
is refused before anything is sent, and :attr:`ProviderHost.requests` shows that none arrived.

:func:`owner_egress` saves the settings into the test's own home, which the repository's
``conftest.py`` gives every test.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, NamedTuple

#: The stand-in's address, as the owner names it in Allowed hosts or Denied hosts.
HOST = "127.0.0.1"

#: A host the owner allowed that is not the stand-in.
OTHER_HOST = "search.example.org"

class Refusal(NamedTuple):
    """One way the guard refuses the stand-in: its own reason (``str(EgressBlocked)``), and the
    sentence an app says for it (``personalclaw.sdk.net.egress_refusal``), which names the setting
    that lifts it. ``{url}`` in the sentence is the endpoint the app asked."""

    reason: str
    sentence: str

    def said(self, url: str) -> str:
        return self.sentence.format(url=url)


#: The owner denied the stand-in.
DENIED = Refusal(
    reason=f"host '{HOST}' is on the egress deny list",
    sentence=f"{{url}} was not reached: {HOST} is on Denied hosts in Settings → Security → Network "
    "egress.",
)

#: The owner did not allow the stand-in: an address on this machine, which the guard refuses by
#: default.
NOT_ALLOWED = Refusal(
    reason=(
        f"host '{HOST}' resolves to a non-public address ({HOST}, loopback); egress guard blocks "
        "loopback, private, link-local, multicast, and reserved IPs"
    ),
    sentence=(
        f"PersonalClaw's network settings refused {{url}}, which is on this computer ({HOST}). If "
        f"this endpoint is yours, add {HOST} to Allowed hosts in Settings → Security → Network "
        "egress, then test again."
    ),
)

#: ``(allow_hosts, deny_hosts, refusal)``: each setting that refuses the stand-in. The owner
#: denied it (and allowed it, which a deny outranks); the owner allowed another host only; and
#: the owner set nothing.
REFUSALS = [
    ((HOST,), (HOST,), DENIED),
    ((OTHER_HOST,), (), NOT_ALLOWED),
    ((), (), NOT_ALLOWED),
]
REFUSAL_IDS = ["the-owner-denied-it", "the-owner-allowed-another-host", "nothing-set"]


def owner_egress(*, allow_hosts: Iterable[str] = (), deny_hosts: Iterable[str] = ()) -> None:
    """Save the owner's Allowed hosts and Denied hosts in this test's home, where the Security
    page saves them."""
    from personalclaw.sdk.util import config_dir

    home = config_dir()
    home.mkdir(parents=True, exist_ok=True)
    egress = {"allow_hosts": list(allow_hosts), "deny_hosts": list(deny_hosts)}
    (home / "config.json").write_text(
        json.dumps({"security": {"egress": egress}}), encoding="utf-8"
    )


class ProviderHost:
    """An HTTP server on ``127.0.0.1`` that answers every request with one canned body, and keeps
    each request it was sent.

    ``with ProviderHost(body) as host:`` starts it; ``host.url`` is its base URL and
    ``host.requests`` the ``{"method", "path", "body"}`` of each request, oldest first. With
    *moved_to* it sends every request on there instead, as a host that redirects does: a 307 to
    *moved_to* followed by the request's path.
    """

    def __init__(
        self, body: str | bytes, *, content_type: str = "application/json", moved_to: str = ""
    ) -> None:
        self.body = body.encode("utf-8") if isinstance(body, str) else body
        self.content_type = content_type
        self.moved_to = moved_to
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._httpd = ThreadingHTTPServer((HOST, 0), _handler_for(self))
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def record(self, method: str, path: str, body: bytes) -> None:
        with self._lock:
            self.requests.append({"method": method, "path": path, "body": body})

    def __enter__(self) -> "ProviderHost":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def _handler_for(server: ProviderHost) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:  # keep test output quiet
            pass

        def _answer(self, method: str) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            server.record(method, self.path, raw)
            if server.moved_to:
                self.send_response(307)
                self.send_header("Location", f"{server.moved_to}{self.path}")
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", server.content_type)
            self.send_header("Content-Length", str(len(server.body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(server.body)

        def do_GET(self) -> None:  # noqa: N802 — the http.server hook name
            self._answer("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._answer("POST")

        def do_HEAD(self) -> None:  # noqa: N802
            self._answer("HEAD")

    return _Handler
