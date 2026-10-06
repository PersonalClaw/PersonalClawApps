#!/usr/bin/env python3
"""Repo rail: an app opens no HTTP client that the egress guard is not inside.

Settings → Security → Network egress is where the owner says what PersonalClaw may reach: a host on
Denied hosts is never reached. A request an app sends through an HTTP client it built itself asks
no guard, so it reaches a denied host all the same (a model provider's chat did, while the same
provider's Test, which went through the guard, was refused). Core's SDK hands out clients the guard
is inside: ``personalclaw.sdk.net.http_client`` and ``sync_http_client`` (httpx) and
``http_session`` (aiohttp), each asking the guard about every request it sends, each redirect hop
included. A client library that takes no HTTP client asks the guard itself (``RequestGuard``) from
the library's own hook: an AWS session's clients from a ``before-send`` handler registered on the
session, which the AWS SDK runs before every request any client of the session sends. That glue is
the library's, so the app holds it, and it is recognised here by its shape, not by a name. A
request ``fetch`` sends is asked too, and ``check_egress_policy.py`` reads its policy.

So every call in an app's code is read here, on the AST, for one that opens a client or sends a
request any other way:

* an ``httpx``, ``aiohttp``, ``requests``, ``urllib.request`` or ``http.client`` client or request,
  whatever name it was imported as;
* an ``openai`` or ``anthropic`` SDK client not handed a guarded ``http_client``, or the SDK's own
  default HTTP client;
* an AWS session (``boto3.Session``, ``botocore.session.get_session``) bound to a name in a function
  with no ``before-send`` handler registered on it there, before any client is made from it, whose
  body asks a guard (``.ask(...)``), and a client of boto3's default session (``boto3.client``,
  ``boto3.resource``), which nobody can hook first.

What it reads: every app's own code (its tests, ``conftest.py``, ``tests`` folders and hidden
folders left out), except the channel apps (a manifest whose provider is a ``channel``): a
channel's connection to its own service (Slack's, Telegram's and Discord's APIs) is a family of its
own, which this rail does not hold yet. A connection that is not HTTP (a WebSocket, a mail server's
IMAP or SMTP, a raw socket) is outside its vocabulary. Core holds its own provider modules and the
apps bundled in it to the same rule (``tests/test_network_clients_ask_the_guard_rail.py`` there).

**Vacuity floor.** A rail that matches nothing reads as clean, so the reader is checked against the
shapes it tells apart before anything is read, and each app in :data:`KNOWN_GUARDED` must be seen
making the guarded client it is named for.

Run locally exactly as CI does:

    python .github/scripts/check_network_clients.py
"""

from __future__ import annotations

import ast
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]

#: The constructors that put the guard inside the client they return, and the guard a client
#: library's own hook asks.
GUARDED = frozenset({"http_client", "sync_http_client", "http_session", "RequestGuard"})

#: The guarded constructors whose client an SDK takes as its ``http_client`` (an httpx client).
HTTPX_CLIENTS = frozenset({"http_client", "sync_http_client"})

#: What :func:`made` names an AWS session whose every request asks the guard first.
HOOKED_SESSION = "a hooked AWS session"

#: The calls that make an AWS session, ``(module, name)``: its clients send their own requests and
#: take no HTTP client.
AWS_SESSIONS = frozenset(
    {
        ("boto3", "Session"),
        ("boto3.session", "Session"),
        ("botocore.session", "Session"),
        ("botocore.session", "get_session"),
    }
)
#: The calls that make a client of boto3's default session, which nobody can hook first.
AWS_DEFAULT_CLIENTS = frozenset({("boto3", "client"), ("boto3", "resource")})
#: The event an AWS SDK client announces before each request it sends, a retry and a redirect
#: included, for every client of the session it is registered on.
BEFORE_SEND = "before-send"
_REGISTERS = frozenset({"register", "register_first", "register_last"})
#: What a session's client is made with.
_CLIENT_MAKERS = frozenset({"client", "resource", "create_client"})
#: The guard's own methods, which a hook asks.
_ASKS = frozenset({"ask", "ask_async"})
_SCOPES = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)

#: ``module -> names`` whose call opens a client or sends a request with no guard in it.
RAW = {
    "httpx": frozenset(
        {
            "AsyncClient",
            "Client",
            "AsyncHTTPTransport",
            "HTTPTransport",
            "get",
            "post",
            "put",
            "patch",
            "delete",
            "head",
            "options",
            "request",
            "stream",
        }
    ),
    "aiohttp": frozenset({"ClientSession", "request"}),
    "requests": frozenset(
        {"get", "post", "put", "patch", "delete", "head", "options", "request", "Session"}
    ),
    "urllib.request": frozenset({"urlopen", "build_opener"}),
    "http.client": frozenset({"HTTPConnection", "HTTPSConnection"}),
    "openai": frozenset({"DefaultHttpxClient", "DefaultAsyncHttpxClient", "DefaultAioHttpClient"}),
    "anthropic": frozenset(
        {"DefaultHttpxClient", "DefaultAsyncHttpxClient", "DefaultAioHttpClient"}
    ),
}

#: The SDK clients that must be handed a guarded ``http_client``, by name: an app may hold the SDK
#: as a module it was handed rather than one it imported.
SDK_CLIENTS = frozenset(
    {
        "OpenAI",
        "AsyncOpenAI",
        "AzureOpenAI",
        "AsyncAzureOpenAI",
        "Anthropic",
        "AsyncAnthropic",
        "AnthropicBedrock",
        "AsyncAnthropicBedrock",
        "AnthropicVertex",
        "AsyncAnthropicVertex",
    }
)

#: Apps the floor must see making their guarded clients: one per kind of client.
KNOWN_GUARDED = {
    "bedrock-models": frozenset({HOOKED_SESSION, "RequestGuard", "sync_http_client"}),
    "google-models": frozenset({"http_session"}),
    "alibaba-models": frozenset({"http_session"}),
    "fal-image": frozenset({"http_session"}),
    "openai-tools": frozenset({"sync_http_client"}),
    "skills-sh": frozenset({"sync_http_client"}),
}


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = _dotted(node.value)
        return f"{inner}.{node.attr}" if inner else ""
    return ""


def _named(call: ast.Call) -> str:
    """The last name a call is made by: ``http_client`` for ``net.http_client(...)``."""
    return _dotted(call.func).rpartition(".")[2]


def _aliases(tree: ast.AST) -> dict[str, str]:
    """Each local name bound to a module or a module's name: ``hx -> httpx``,
    ``urlopen -> urllib.request.urlopen``."""
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    names[alias.asname] = alias.name
                else:
                    head = alias.name.partition(".")[0]
                    names[head] = head
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                names[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return names


def _resolved(call: ast.Call, aliases: dict[str, str]) -> tuple[str, str]:
    """``(module, name)`` a call resolves to through *aliases*, or ``("", "")`` for a call not
    made through an imported name (a method of a local object)."""
    head, _, rest = _dotted(call.func).partition(".")
    if head not in aliases:
        return "", ""
    full = f"{aliases[head]}.{rest}" if rest else aliases[head]
    module, _, name = full.rpartition(".")
    return module, name


def _guarded_value(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Call) and _named(node) in HTTPX_CLIENTS


class _Scopes:
    """Which function (or the module) each node of a tree belongs to."""

    def __init__(self, tree: ast.AST) -> None:
        self.tree = tree
        self._parent: dict[ast.AST, ast.AST] = {
            child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
        }

    def parent(self, node: ast.AST) -> ast.AST | None:
        return self._parent.get(node)

    def of(self, node: ast.AST) -> ast.AST:
        """The nearest function, class or module *node* is in."""
        at = self._parent.get(node)
        while at is not None and not isinstance(at, _SCOPES):
            at = self._parent.get(at)
        return at if at is not None else self.tree

    def nodes(self, scope: ast.AST) -> list[ast.AST]:
        """Every node whose nearest scope is *scope*, in source order."""
        inside = [n for n in ast.walk(scope) if n is not scope and self.of(n) is scope]
        return sorted(inside, key=lambda n: (getattr(n, "lineno", 0), getattr(n, "col_offset", 0)))


def _bound_name(call: ast.Call, scopes: _Scopes) -> str:
    """The name *call*'s value is bound to (``session = boto3.Session()``), else ``""``."""
    stmt = scopes.parent(call)
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
        target = stmt.targets[0]
        return target.id if isinstance(target, ast.Name) else ""
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        return stmt.target.id
    return ""


def _asks_a_guard(node: ast.AST) -> bool:
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in _ASKS
        for n in ast.walk(node)
    )


def _hook_asks(handler: ast.AST | None, scope: ast.AST, scopes: _Scopes) -> bool:
    """Whether *handler*, registered in *scope*, asks a guard: a lambda that does, or a function
    of that name defined in the scope or the module that does."""
    if isinstance(handler, ast.Lambda):
        return _asks_a_guard(handler.body)
    if not isinstance(handler, ast.Name):
        return False
    for where in (scope, scopes.tree):
        for node in scopes.nodes(where):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == handler.id
            ):
                return _asks_a_guard(node)
    return False


def _registration(call: ast.Call, session: str) -> tuple[str, ast.AST | None] | None:
    """``(event, handler)`` when *call* registers a handler on *session*'s events
    (``session.events.register_first(...)``, or ``session.register(...)`` on a botocore one)."""
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr not in _REGISTERS:
        return None
    on = func.value
    if isinstance(on, ast.Attribute) and on.attr == "events":
        on = on.value
    if not (isinstance(on, ast.Name) and on.id == session):
        return None
    given = {kw.arg: kw.value for kw in call.keywords}
    event = call.args[0] if call.args else given.get("event_name")
    handler = call.args[1] if len(call.args) > 1 else given.get("handler")
    name = event.value if isinstance(event, ast.Constant) and isinstance(event.value, str) else ""
    return name, handler


def _unhooked(call: ast.Call, scopes: _Scopes) -> str:
    """What is wrong with the AWS session *call* makes, or ``""`` when every request its clients
    send asks the guard first: it is bound to a name, and in that function a ``before-send``
    handler that asks a guard is registered on it before any client is made from it."""
    session = _bound_name(call, scopes)
    if not session:
        return "an AWS session with no before-send hook that asks the egress guard"
    scope = scopes.of(call)
    hooked_at: tuple[int, int] | None = None
    first_client: tuple[int, int] | None = None
    for node in scopes.nodes(scope):
        if not isinstance(node, ast.Call):
            continue
        at = (node.lineno, node.col_offset)
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in _CLIENT_MAKERS
            and isinstance(func.value, ast.Name)
            and func.value.id == session
        ):
            first_client = first_client or at
        registered = _registration(node, session)
        if (
            registered is not None
            and registered[0] == BEFORE_SEND
            and hooked_at is None
            and _hook_asks(registered[1], scope, scopes)
        ):
            hooked_at = at
    if hooked_at is None:
        return "an AWS session with no before-send hook that asks the egress guard"
    if first_client is not None and first_client < hooked_at:
        return "an AWS session that makes a client before its before-send hook is registered"
    return ""


def _aws_sessions(tree: ast.AST) -> list[ast.Call]:
    aliases = _aliases(tree)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _resolved(node, aliases) in AWS_SESSIONS
    ]


def unguarded(tree: ast.AST) -> list[tuple[int, str]]:
    """Each client *tree* opens with no guard in it, as ``(line, what)``."""
    aliases = _aliases(tree)
    scopes = _Scopes(tree)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        module, name = _resolved(node, aliases)
        if name in RAW.get(module, frozenset()):
            found.append((node.lineno, f"{module}.{name}"))
        elif _named(node) in SDK_CLIENTS:
            given = {kw.arg: kw.value for kw in node.keywords}
            if not _guarded_value(given.get("http_client")):
                found.append((node.lineno, f"{_dotted(node.func)} with no guarded http_client"))
        elif (module, name) in AWS_SESSIONS:
            if wrong := _unhooked(node, scopes):
                found.append((node.lineno, wrong))
        elif (module, name) in AWS_DEFAULT_CLIENTS:
            found.append((node.lineno, f"a client of boto3's default session (boto3.{name})"))
    return sorted(found)


def made(tree: ast.AST) -> set[str]:
    """The guarded constructors *tree* calls, and :data:`HOOKED_SESSION` when it makes an AWS
    session every request of which asks the guard first."""
    called = {_named(node) for node in ast.walk(tree) if isinstance(node, ast.Call)} & GUARDED
    scopes = _Scopes(tree)
    if any(not _unhooked(session, scopes) for session in _aws_sessions(tree)):
        called.add(HOOKED_SESSION)
    return called


#: The positive control: each shape the reader tells apart, and the lines it must find.
_CONTROL = """
import aiohttp
import boto3
import httpx as hx
import openai
import urllib.request
from aiohttp import ClientSession as Session
from urllib.request import urlopen

from personalclaw.sdk.net import RequestGuard, http_client, http_session


async def calls(url, sdk):
    hx.AsyncClient()  # unguarded
    Session()  # unguarded
    aiohttp.ClientSession()  # unguarded
    urlopen(url)  # unguarded
    urllib.request.build_opener()  # unguarded
    openai.AsyncOpenAI(api_key="k")  # unguarded
    sdk.AsyncAnthropic(api_key="k", http_client=hx.AsyncClient())  # unguarded twice
    openai.AsyncOpenAI(api_key="k", http_client=RequestGuard())  # unguarded
    boto3.client("s3")  # unguarded
    boto3.Session().client("s3")  # unguarded
    http_session(model_provider=True)
    openai.AsyncOpenAI(api_key="k", http_client=http_client(model_provider=True))
    requests = {}
    requests.get(url)


def hooked(profile):
    session = boto3.Session(profile_name=profile)
    guard = RequestGuard(model_provider=True)

    def ask(request, **_event):
        guard.ask(str(request.url))

    session.events.register_first("before-send", ask)
    return session.client("bedrock")


def hooked_late():
    session = boto3.Session()  # unguarded
    first = session.client("s3")
    session.events.register_first("before-send", lambda request, **_: RequestGuard().ask(request.url))
    return first


def hooked_to_nothing():
    session = boto3.Session()  # unguarded
    session.events.register_first("before-send", print)
    return session
"""


def _reader_problems() -> list[str]:
    """The reader against :data:`_CONTROL`: each line marked ``# unguarded`` found once, the one
    marked ``# unguarded twice`` (an SDK client handed a raw client) twice, and nothing else; and
    the one hooked session in it read as hooked."""
    want: list[int] = []
    for n, text in enumerate(_CONTROL.splitlines(), start=1):
        if text.endswith("# unguarded"):
            want.append(n)
        elif text.endswith("# unguarded twice"):
            want += [n, n]
    tree = ast.parse(_CONTROL)
    got = [line for line, _what in unguarded(tree)]
    problems = [] if got == want else [f"the reader found lines {got}, expected {want}"]
    if HOOKED_SESSION not in made(tree):
        problems.append("the reader did not see the control's hooked AWS session")
    return problems


def _is_app_code(path: pathlib.Path, app: pathlib.Path) -> bool:
    rel = path.relative_to(app)
    return not (
        path.name.startswith("test_")
        or path.name == "conftest.py"
        or "tests" in rel.parts
        or "__pycache__" in rel.parts
        or any(part.startswith(".") for part in rel.parts)
    )


def _is_channel(manifest: pathlib.Path) -> bool:
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    kinds = [(data.get("provider") or {}).get("type")]
    kinds += [p.get("type") for p in data.get("providers") or [] if isinstance(p, dict)]
    return "channel" in kinds


def census(root: pathlib.Path = ROOT) -> tuple[list[str], dict[str, set[str]], list[str]]:
    """Every unguarded client in an app's code as ``"<file>:<line>: <what>"``, the guarded
    constructors each app calls, and the files that could not be read."""
    found: list[str] = []
    guarded: dict[str, set[str]] = {}
    unreadable: list[str] = []
    for manifest in sorted(root.glob("*/app.json")):
        app = manifest.parent
        if _is_channel(manifest):
            continue
        for path in sorted(app.rglob("*.py")):
            if not _is_app_code(path, app):
                continue
            rel = path.relative_to(root).as_posix()
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError, UnicodeDecodeError) as error:
                unreadable.append(f"cannot read {rel}: {error}")
                continue
            found.extend(f"{rel}:{line}: {what}" for line, what in unguarded(tree))
            guarded.setdefault(app.name, set()).update(made(tree))
    return found, guarded, unreadable


def problems(root: pathlib.Path = ROOT) -> list[str]:
    found = _reader_problems()
    unguarded_clients, guarded, unreadable = census(root)
    found.extend(unreadable)
    found.extend(
        f"{site}: a host on Denied hosts would still be reached through it. Use "
        "personalclaw.sdk.net's http_client, sync_http_client or http_session, or ask its "
        "RequestGuard from the library's own hook before each request (an AWS session's "
        "before-send, registered before any client is made)"
        for site in unguarded_clients
    )
    for app, constructors in KNOWN_GUARDED.items():
        missing = constructors - guarded.get(app, set())
        if missing:
            found.append(
                f"vacuity floor: {app} was not seen making {', '.join(sorted(missing))}"
            )
    return found


def main() -> int:
    found = problems()
    if found:
        print("An app must open its HTTP clients with the egress guard inside them:")
        for line in found:
            print(f"  {line}")
        return 1
    _found, guarded, _unreadable = census()
    apps = sum(1 for constructors in guarded.values() if constructors)
    print(f"OK: every HTTP client in the apps asks the egress guard ({apps} app(s) make one)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
