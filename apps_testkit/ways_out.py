"""What an app's modules can reach past this machine, read from their source, for the tests that
hold each tool's ``consequences`` to what it does.

A run-every-tool test patches the ways out its app takes (a command started through ``asyncio``,
the SDK's page fetch) and checks what every call reached through them. A send by any other way
never reaches the patch, so the run cannot see it. :func:`ways_out` reads the modules instead:
what they import, and every name they use under the modules that hold their ways out, resolved
through whatever name each is imported as. A test holds both to what the modules use today, so a
new way out fails it: ``import aiohttp``, ``asyncio.subprocess.create_subprocess_shell``,
``os.system`` under an alias, a watched module handed on whole (``getattr(asyncio, …)``,
``sub = asyncio.subprocess``), a star import of one, or a function of the SDK's network module
the run does not patch.

It reads names, not scopes: a local name spelled like an imported one is read as the import, so
the test fails, naming a reach the code does not make, and never passes one it does. Code that
hides a module from any reading (``__import__``, ``exec``, a module reached through another
module's attribute) is beyond a static check and is not what this guards against.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path


def _under(name: str, watched: tuple[str, ...]) -> bool:
    return any(name == w or name.startswith(w + ".") for w in watched)


def ways_out(paths: Iterable[Path | str], watched: Iterable[str]) -> tuple[set[str], set[str]]:
    """``(imports, reached)`` for the modules at *paths*: every module they import, and every dotted
    name under one of the *watched* modules they use, whole at each use (``asyncio.subprocess.PIPE``
    for that expression, ``asyncio`` for ``getattr(asyncio, …)``), read through the local name the
    module or the name is bound to. A star import from a watched module reaches ``<module>.*``."""
    watched = tuple(watched)
    imports: set[str] = set()
    reached: set[str] = set()
    for path in paths:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"), filename=str(path))
        # Each local name an import binds, and the dotted name it stands for.
        bound: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name)
                    root = alias.name.split(".")[0]
                    bound[alias.asname or root] = alias.name if alias.asname else root
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                imports.add(module)
                for alias in node.names:
                    if alias.name == "*":
                        if _under(module, watched):
                            reached.add(f"{module}.*")
                        continue
                    bound[alias.asname or alias.name] = f"{module}.{alias.name}"
        # A name inside a longer one (``asyncio`` in ``asyncio.gather``) is read with it, once.
        inner = {id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        for node in ast.walk(tree):
            if id(node) in inner or not isinstance(node, (ast.Attribute, ast.Name)):
                continue
            parts: list[str] = []
            head: ast.expr = node
            while isinstance(head, ast.Attribute):
                parts.append(head.attr)
                head = head.value
            if isinstance(head, ast.Name) and head.id in bound:
                name = ".".join([bound[head.id], *reversed(parts)])
                if _under(name, watched):
                    reached.add(name)
    return imports, reached
