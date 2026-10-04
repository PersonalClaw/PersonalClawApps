"""Every app reads a model's stream inside ``closing_stream``, so the stream is closed the moment
the app stops reading it.

A model's stream holds what its turn holds until it is closed: an agent CLI's session, which sends
no other prompt meanwhile, an open request, a spend hold. An app stops reading one before its end
for good reasons (its terminal event, a call whose approval was not given, an error, a stop), and a
stream it leaves open keeps all of that until the interpreter collects it: at once on one Python,
much later or never on another. Slack Channel's turns were left that way, and a thread's next
message waited, unsent, behind a session nobody was reading.

So every app reads a model's stream inside ``personalclaw.sdk.model.closing_stream``::

    async with closing_stream(provider.stream(prompt)) as events:
        async for event in events:
            ...

and leaving the block by any way out closes it. This rail reads every bundle's shipped code and
fails on a model's stream read any other way: an ``async for`` (or an async comprehension) over a
call of a provider's stream methods, or over a name such a call was assigned to, and an ``anext`` of
either. The stream methods are the installed core's: every async generator method of
``personalclaw.sdk.model.ModelProvider``.
"""

from __future__ import annotations

import ast
import inspect
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # for apps_testkit

from apps_testkit import sdk_contract  # noqa: E402

BUNDLES = sorted(manifest.parent for manifest in ROOT.glob("*/app.json"))

#: The apps that read a model's stream as this rail lands: an app the scan stopped recognising as
#: a reader would leave the rail checking nothing.
KNOWN_READERS = {"code-review", "issue-radar", "slack-channel"}


@pytest.fixture(autouse=True)
def _scratch_home(tmp_path, monkeypatch):
    """The SDK resolves core's home when it loads. Not the real one."""
    monkeypatch.setenv("PERSONALCLAW_HOME", str(tmp_path / "home"))


def _stream_methods() -> frozenset[str]:
    """The names of a model provider's stream methods on the installed core."""
    from personalclaw.sdk.model import ModelProvider

    return frozenset(
        name
        for name, member in inspect.getmembers(ModelProvider)
        if inspect.isasyncgenfunction(member)
    )


@dataclass
class Reads:
    """Where a bundle reads a model's stream: inside ``closing_stream``, and outside it."""

    inside: list[str] = field(default_factory=list)
    outside: list[str] = field(default_factory=list)


def _is_closing_stream(call: ast.expr) -> bool:
    if not isinstance(call, ast.Call):
        return False
    func = call.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    return name == "closing_stream"


class _Scan(ast.NodeVisitor):
    """One module's reads of a model's stream, function by function."""

    def __init__(self, methods: frozenset[str], where: str, reads: Reads) -> None:
        self.methods = methods
        self.where = where
        self.reads = reads
        #: Per function: the names a model stream call was assigned to, and the names
        #: ``closing_stream`` bound (``True`` when what it closes is a model's stream).
        self.scopes: list[tuple[set[str], dict[str, bool]]] = [(set(), {})]

    def _opens_a_stream(self, node: ast.expr) -> bool:
        """A call of a provider's stream method: ``<anything>.stream(...)``."""
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in self.methods
        )

    def _is_a_stream(self, node: ast.expr) -> bool:
        assigned, _bound = self.scopes[-1]
        return self._opens_a_stream(node) or (isinstance(node, ast.Name) and node.id in assigned)

    def _read(self, iterated: ast.expr, lineno: int) -> None:
        _assigned, bound = self.scopes[-1]
        at = f"{self.where}:{lineno}"
        if isinstance(iterated, ast.Name) and iterated.id in bound:
            if bound[iterated.id]:
                self.reads.inside.append(at)
        elif self._is_a_stream(iterated):
            self.reads.outside.append(at)

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        assigned, bound = self.scopes[-1]
        # A nested function sees the names of the one it is in, as a closure does.
        self.scopes.append((set(assigned), dict(bound)))
        self.generic_visit(node)
        self.scopes.pop()

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function

    def visit_Assign(self, node: ast.Assign) -> None:
        if self._opens_a_stream(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.scopes[-1][0].add(target.id)
        self.generic_visit(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        for item in node.items:
            call = item.context_expr
            if _is_closing_stream(call) and isinstance(item.optional_vars, ast.Name):
                closes = bool(call.args) and self._is_a_stream(call.args[0])
                self.scopes[-1][1][item.optional_vars.id] = closes
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._read(node.iter, node.lineno)
        self.generic_visit(node)

    def visit_comprehension(self, node: ast.comprehension) -> None:
        if node.is_async:
            self._read(node.iter, getattr(node.iter, "lineno", 0))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name) and func.id == "anext" and node.args:
            self._read(node.args[0], node.lineno)
        elif isinstance(func, ast.Attribute) and func.attr == "__anext__":
            self._read(func.value, node.lineno)
        self.generic_visit(node)


def reads_of(bundle: Path, methods: frozenset[str]) -> Reads:
    """Where *bundle*'s shipped code reads a model's stream, inside and outside ``closing_stream``."""
    reads = Reads()
    for path in sdk_contract.shipped_sources(bundle):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        _Scan(methods, str(path.relative_to(bundle)), reads).visit(tree)
    return reads


def test_the_stream_methods_are_the_installed_cores():
    assert {"stream", "stream_command", "complete"} <= _stream_methods()


def test_every_app_reads_a_models_stream_inside_closing_stream():
    methods = _stream_methods()
    scanned = {bundle.name: reads_of(bundle, methods) for bundle in BUNDLES}
    readers = {name for name, reads in scanned.items() if reads.inside or reads.outside}
    assert KNOWN_READERS <= readers, (
        f"the scan no longer sees these apps read a model's stream: "
        f"{sorted(KNOWN_READERS - readers)}"
    )
    outside = {name: reads.outside for name, reads in scanned.items() if reads.outside}
    assert outside == {}, (
        "these apps read a model's stream outside closing_stream, so a stream they stop reading "
        "part way stays open, and holds what it holds (an agent CLI's session among them), until "
        f"the interpreter collects it: {outside}. Read it inside "
        "personalclaw.sdk.model.closing_stream."
    )


def test_the_scan_tells_a_stream_read_inside_closing_stream_from_one_read_outside(tmp_path):
    """Positive and negative controls, in the shapes the rail reads."""
    app = tmp_path / "reader-app"
    app.mkdir()
    (app / "app.json").write_text('{"name": "reader-app", "version": "0.1.0"}')
    (app / "inside.py").write_text(
        "from personalclaw.sdk.model import closing_stream\n\n\n"
        "async def reply(client, message):\n"
        "    async with closing_stream(client.stream(message)) as events:\n"
        "        async for event in events:\n"
        "            if event.kind == 'complete':\n"
        "                break\n\n\n"
        "async def compact(provider):\n"
        "    turn = provider.stream_command('/compact')\n"
        "    async with closing_stream(turn) as events:\n"
        "        return [event async for event in events]\n\n\n"
        "async def download(resp):\n"
        "    async for chunk in resp.content.iter_chunked(8192):\n"
        "        yield chunk\n",
        encoding="utf-8",
    )
    (app / "outside.py").write_text(
        "async def reply(client, message):\n"
        "    async for event in client.stream(message):\n"
        "        break\n\n\n"
        "async def kept(client, message):\n"
        "    events = client.stream(message)\n"
        "    async for event in events:\n"
        "        break\n\n\n"
        "async def gathered(provider, messages):\n"
        "    return [event async for event in provider.complete(messages)]\n\n\n"
        "async def first(provider):\n"
        "    return await anext(provider.stream_command('/compact'))\n",
        encoding="utf-8",
    )
    reads = reads_of(app, _stream_methods())
    assert reads.inside == ["inside.py:6", "inside.py:14"]
    assert reads.outside == ["outside.py:2", "outside.py:8", "outside.py:13", "outside.py:17"]

    other = tmp_path / "other-app"
    other.mkdir()
    (other / "app.json").write_text('{"name": "other-app", "version": "0.1.0"}')
    (other / "provider.py").write_text(
        "async def pages(client):\n"
        "    async for page in client.pages():\n"
        "        yield page\n",
        encoding="utf-8",
    )
    assert reads_of(other, _stream_methods()) == Reads()
