"""SDK contract rails: every call an app makes into ``personalclaw.sdk.*`` holds on the INSTALLED core.

Why this exists. Core #3599 changed ``compress_thread_history`` to take a session's prior TURNS
(``list[dict]``) instead of a ``ConversationLog``. Same arity, same position: every import still
resolved, every call still bound, and the Slack app raised ``TypeError: 'ConversationLog' object is
not iterable`` whenever a fresh runtime picked up a thread. Its suite stayed green, because the one
test that reached the call asserted an ABSENCE that the crash produced too. Nothing in either repo
compared what the app passes with what core now declares.

Three rails over one census of the app's shipped source:

1. **Published** (:func:`unpublished_imports`) — every name imported from ``personalclaw.sdk.X`` is
   in ``X.__all__`` on the installed core. A removed or renamed export fails by name, and so does a
   name the facade happens to carry without publishing it.
2. **Binds** (:func:`unbindable_calls`) — every call whose target resolves statically binds its
   positional count and keyword names to the installed signature. Resolved: an imported SDK
   function or class, an attribute of an imported SDK module or class, a method on a parameter
   annotated with an SDK class, a method on an attribute of one (typed by the class's annotations
   or its ``__init__``), and a method on the annotated return of an SDK function (``sel().log…``).
3. **Conforms** (:func:`record_sdk_calls`) — at RUNTIME, every argument the app passes satisfies
   the installed annotation. A type change keeps the arity, so only the values can show it. The
   recorder wraps the SDK functions and methods the census found for the length of one test and
   records each APP-originated call (the calling frame is a shipped app file) that does not conform.

Tests may import core internals; this module deliberately does not — it is linted with the apps
(``boundary`` job) and reaches the SDK only through :func:`importlib.import_module` on names the
app itself imported.
"""

from __future__ import annotations

import ast
import collections
import collections.abc
import contextlib
import functools
import importlib
import inspect
import sys
import types
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

SDK = "personalclaw.sdk"

#: Directories under a bundle that hold no shipped app code.
_NOT_SHIPPED = {"tests", "__pycache__", "node_modules", "dist", "build", "a11y", "assets", "ui"}


def shipped_sources(app_dir: Path) -> list[Path]:
    """The bundle's shipped Python: every ``.py`` except tests and test support."""
    out: list[Path] = []
    for path in sorted(app_dir.rglob("*.py")):
        parts = path.relative_to(app_dir).parts
        if any(p in _NOT_SHIPPED or p.startswith(".") for p in parts[:-1]):
            continue
        if path.name.startswith("test_") or path.name == "conftest.py":
            continue
        out.append(path)
    return out


# ── The census ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SdkImport:
    path: Path
    lineno: int
    module: str
    name: str  # "" for ``import personalclaw.sdk.x``
    alias: str


@dataclass(frozen=True)
class SdkCall:
    path: Path
    lineno: int
    #: How the app named the target, e.g. ``compress_thread_history`` or ``sel().log_api_access``.
    spelled: str
    #: Dotted name of what it resolved to on the installed core.
    target: str
    obj: Any = field(compare=False)
    #: The first parameter is supplied by the receiver (an instance method, a classmethod).
    bound: bool
    positional: int
    keywords: tuple[str, ...]
    starred: bool
    double_starred: bool
    #: For a method: the class it was looked up on and the attribute name (the recorder patches it).
    owner: Any = field(default=None, compare=False)
    attr: str = ""


@dataclass
class Census:
    app_dir: Path
    imports: list[SdkImport]
    calls: list[SdkCall]
    #: ``(path, lineno, spelled)`` of calls into an SDK object that could not be typed statically.
    unresolved: list[tuple[Path, int, str]]


def _is_sdk(module: str | None) -> bool:
    return bool(module) and (module == SDK or module.startswith(SDK + "."))  # type: ignore[union-attr]


def _resolve_import(imp: SdkImport) -> Any:
    """The installed object an import names, or raise ``LookupError`` saying why not."""
    try:
        mod = importlib.import_module(imp.module)
    except ImportError as exc:
        raise LookupError(f"module {imp.module} does not import: {exc}") from exc
    if not imp.name:
        return mod
    if not hasattr(mod, imp.name):
        # ``from personalclaw.sdk import channel`` names a submodule, not an attribute.
        try:
            return importlib.import_module(f"{imp.module}.{imp.name}")
        except ImportError:
            raise LookupError(f"{imp.module} has no {imp.name!r}") from None
    return getattr(mod, imp.name)


def _annotation_names(node: ast.expr | None) -> list[str]:
    """Plain names an annotation mentions: ``X | None`` → [X], ``Optional[X]`` → [X], ``"X"`` → [X]."""
    if node is None:
        return []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            return _annotation_names(ast.parse(node.value, mode="eval").body)
        except SyntaxError:
            return []
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _annotation_names(node.left) + _annotation_names(node.right)
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
        if node.value.id in {"Optional", "Union"}:
            inner = node.slice
            elts = inner.elts if isinstance(inner, ast.Tuple) else [inner]
            return [n for e in elts for n in _annotation_names(e)]
    return []


def _sdk_namespace() -> dict[str, Any]:
    """Every name the imported ``personalclaw.sdk.*`` modules publish.

    The second place a string annotation is resolved: core often imports an annotation's type
    under ``TYPE_CHECKING`` only (``conversation_log: "ConversationLog | None"``), so the name is
    absent from the defining module at runtime — but the facade publishes that very type, and it
    is the one the app itself imported.
    """
    ns: dict[str, Any] = {}
    for name, module in list(sys.modules.items()):
        if module is not None and _is_sdk(name):
            ns.update({n: getattr(module, n) for n in getattr(module, "__all__", ()) if hasattr(module, n)})
    return ns


def _hints(obj: Any) -> dict[str, Any]:
    """``typing.get_type_hints`` per name, so one TYPE_CHECKING-only annotation cannot blind the rest."""
    try:
        return typing.get_type_hints(obj)
    except Exception:  # noqa: BLE001 — fall through to the per-name resolution
        pass
    raw = getattr(obj, "__annotations__", None) or {}
    globalns = getattr(obj, "__globals__", None)
    if globalns is None:
        module = sys.modules.get(getattr(obj, "__module__", ""), None)
        globalns = vars(module) if module is not None else {}
    out: dict[str, Any] = {}
    for name, ann in raw.items():
        for namespace in (globalns, None):
            try:
                ns = globalns if namespace is not None else {**_sdk_namespace(), **globalns}
                out[name] = typing.get_type_hints(
                    types.SimpleNamespace(__annotations__={name: ann}), globalns=ns
                )[name]
                break
            except Exception:  # noqa: BLE001 — an unresolvable annotation is simply not checked
                continue
    return out


def _classes_in(hint: Any) -> list[type]:
    """The concrete classes a resolved hint admits (``X | None`` → [X])."""
    origin = typing.get_origin(hint)
    if origin is typing.Union or origin is types.UnionType:
        return [c for a in typing.get_args(hint) for c in _classes_in(a)]
    # ``typing.Any`` is a class on 3.11+, and says nothing about the value's attributes.
    if isinstance(hint, type) and hint not in (type(None), Any, object):
        return [hint]
    return []


def _single_class(hint: Any) -> type | None:
    classes = _classes_in(hint)
    return classes[0] if len(classes) == 1 else None


def _attribute_type(cls: type, name: str) -> type | None:
    """The declared type of instance attribute *name*: a class annotation, else the
    ``__init__`` parameter of the same name (the ``self.x = x`` convention)."""
    for klass in cls.__mro__:
        ann = _hints(klass).get(name) if "__annotations__" in vars(klass) else None
        if ann is not None:
            return _single_class(ann)
    init = vars(cls).get("__init__")
    if init is not None:
        return _single_class(_hints(init).get(name))
    return None


@dataclass(frozen=True)
class _CallableField:
    """An instance attribute DECLARED as ``Callable[[A, B], R]`` (a dataclass field such as
    ``SetupContext.get_credential``): no method to look up, but its annotation is a signature."""

    owner: Any
    attr: str
    signature: inspect.Signature


def _callable_field(owner: type, attr: str) -> _CallableField | None:
    hint = _hints(owner).get(attr)
    if typing.get_origin(hint) is not collections.abc.Callable:
        return None
    params, _ret = typing.get_args(hint)
    if params is Ellipsis or not isinstance(params, list):
        return None  # ``Callable[..., R]`` declares no parameters to hold a call to
    sig = inspect.Signature([
        inspect.Parameter(f"arg{i}", inspect.Parameter.POSITIONAL_ONLY, annotation=p)
        for i, p in enumerate(params)
    ])
    return _CallableField(owner, attr, sig)


def _method(owner: type, attr: str, *, on_instance: bool) -> tuple[Any, bool] | None:
    """``(callable, bound)`` for ``owner.attr``: a :class:`_MissingAttribute` when the class no
    longer has it, ``None`` when it is not something with a signature to hold (a property)."""
    if on_instance:
        declared = _callable_field(owner, attr)
        if declared is not None:
            return declared, False
    try:
        raw = inspect.getattr_static(owner, attr)
    except AttributeError:
        if on_instance and attr in _hints(owner):
            return None  # a declared, non-callable-typed field: nothing to bind
        return _MissingAttribute(owner, attr), False
    if isinstance(raw, staticmethod):
        return raw.__func__, False
    if isinstance(raw, classmethod):
        return raw.__func__, True
    if inspect.isfunction(raw):
        return raw, on_instance
    if isinstance(raw, type):  # a nested class, called as a constructor
        return raw, False
    return None


class _Resolver(ast.NodeVisitor):
    def __init__(self, path: Path, env: dict[str, Any]) -> None:
        self.path = path
        self.scopes: list[dict[str, Any]] = [env]
        self.calls: list[SdkCall] = []
        self.unresolved: list[tuple[Path, int, str]] = []

    # A local name → an installed SDK object (module-level imports + function-local imports), or
    # the marker ("instance", cls) for a parameter annotated with an SDK class.
    def _lookup(self, name: str) -> Any:
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        return None

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        scope: dict[str, Any] = {}
        args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        for arg in args:
            for n in _annotation_names(arg.annotation):
                obj = self._lookup(n)
                if isinstance(obj, type):
                    scope[arg.arg] = ("instance", obj)
                    break
            else:
                scope[arg.arg] = None  # a parameter shadows any outer binding of its name
        # Function-local ``from personalclaw.sdk… import x`` binds for the whole function body.
        for inner in ast.walk(node):
            if isinstance(inner, ast.ImportFrom) and _is_sdk(inner.module) and inner.level == 0:
                for alias in inner.names:
                    imp = SdkImport(self.path, inner.lineno, inner.module or "", alias.name,
                                    alias.asname or alias.name)
                    with contextlib.suppress(LookupError):
                        scope[imp.alias] = _resolve_import(imp)
        self.scopes.append(scope)
        self.generic_visit(node)
        self.scopes.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def _receiver_class(self, node: ast.expr) -> type | None:
        """The SDK class an expression's VALUE is an instance of, when statically known."""
        if isinstance(node, ast.Name):
            obj = self._lookup(node.id)
            if isinstance(obj, tuple) and obj[0] == "instance":
                return obj[1]
            return None
        if isinstance(node, ast.Attribute):
            owner = self._receiver_class(node.value)
            return _attribute_type(owner, node.attr) if owner is not None else None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            fn = self._lookup(node.func.id)
            if inspect.isfunction(fn):
                return _single_class(_hints(fn).get("return"))
        return None

    def visit_Call(self, node: ast.Call) -> None:
        self.generic_visit(node)
        func = node.func
        spelled = ast.unparse(func)
        target: Any = None
        bound = False
        owner: Any = None
        attr = ""
        if isinstance(func, ast.Name):
            obj = self._lookup(func.id)
            if obj is not None and not isinstance(obj, tuple) and callable(obj):
                target = obj
        elif isinstance(func, ast.Attribute):
            base = func.value
            base_obj = self._lookup(base.id) if isinstance(base, ast.Name) else None
            if isinstance(base_obj, types.ModuleType) and base_obj.__name__.startswith("personalclaw"):
                # ``trust_mode.x(...)``: a core module the facade re-exports.
                target = getattr(base_obj, func.attr, None) or _MissingAttribute(base_obj, func.attr)
            else:
                cls = base_obj if isinstance(base_obj, type) else self._receiver_class(base)
                if cls is not None:
                    # On the class itself (``ProviderSettings.load(...)``) nothing is bound; on an
                    # instance an ordinary method's first parameter is.
                    found = _method(cls, func.attr, on_instance=not isinstance(base_obj, type))
                    if found is None:
                        return  # a property or a callable attribute: no signature to hold
                    (target, bound), owner, attr = found, cls, func.attr
                    if isinstance(target, (_MissingAttribute, _CallableField)):
                        owner, attr = None, ""  # nothing on the class for the recorder to patch
                elif base_obj is not None and not isinstance(base_obj, tuple):
                    self.unresolved.append((self.path, node.lineno, spelled))
        if target is None:
            return
        self.calls.append(
            SdkCall(
                path=self.path,
                lineno=node.lineno,
                spelled=spelled,
                target=_qualname(target),
                obj=target,
                bound=bound,
                positional=sum(1 for a in node.args if not isinstance(a, ast.Starred)),
                keywords=tuple(k.arg for k in node.keywords if k.arg is not None),
                starred=any(isinstance(a, ast.Starred) for a in node.args),
                double_starred=any(k.arg is None for k in node.keywords),
                owner=owner,
                attr=attr,
            )
        )


@dataclass(frozen=True)
class _MissingAttribute:
    """A call to ``owner.attr`` where the installed ``owner`` (a class or module) has no ``attr``."""

    owner: Any
    attr: str


def _qualname(obj: Any) -> str:
    if isinstance(obj, (_MissingAttribute, _CallableField)):
        owner = obj.owner
        prefix = owner.__name__ if isinstance(owner, types.ModuleType) else (
            f"{owner.__module__}.{owner.__qualname__}"
        )
        return f"{prefix}.{obj.attr}"
    mod = getattr(obj, "__module__", "") or ""
    name = getattr(obj, "__qualname__", "") or getattr(obj, "__name__", "") or repr(obj)
    return f"{mod}.{name}" if mod else name


@functools.cache
def census(app_dir: Path) -> Census:
    """Every SDK import in the bundle's shipped source, and every call into what they name."""
    imports: list[SdkImport] = []
    calls: list[SdkCall] = []
    unresolved: list[tuple[Path, int, str]] = []
    for path in shipped_sources(app_dir):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        env: dict[str, Any] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and _is_sdk(node.module):
                for alias in node.names:
                    imports.append(SdkImport(path, node.lineno, node.module or "", alias.name,
                                             alias.asname or alias.name))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_sdk(alias.name):
                        imports.append(SdkImport(path, node.lineno, alias.name, "",
                                                 alias.asname or alias.name.split(".")[0]))
        # Module scope: top-level imports only (function-local ones bind per function).
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.level == 0 and _is_sdk(node.module):
                for alias in node.names:
                    imp = SdkImport(path, node.lineno, node.module or "", alias.name,
                                    alias.asname or alias.name)
                    with contextlib.suppress(LookupError):
                        env[imp.alias] = _resolve_import(imp)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_sdk(alias.name) and alias.asname:
                        with contextlib.suppress(ImportError):
                            env[alias.asname] = importlib.import_module(alias.name)
        resolver = _Resolver(path, env)
        resolver.visit(tree)
        calls += resolver.calls
        unresolved += resolver.unresolved
    return Census(app_dir, imports, calls, unresolved)


def _where(path: Path, lineno: int, app_dir: Path) -> str:
    return f"{path.relative_to(app_dir.parent)}:{lineno}"


# ── Rail 1: published ─────────────────────────────────────────────────────────


def unpublished_imports(app_dir: Path) -> list[str]:
    """Imports that name something the installed SDK does not publish."""
    out: list[str] = []
    for imp in census(app_dir).imports:
        where = _where(imp.path, imp.lineno, app_dir)
        try:
            _resolve_import(imp)
        except LookupError as exc:
            out.append(f"{where}: {exc}")
            continue
        if not imp.name:
            continue
        mod = importlib.import_module(imp.module)
        published = getattr(mod, "__all__", None)
        if published is not None and imp.name not in published and not isinstance(
            getattr(mod, imp.name, None), types.ModuleType
        ):
            out.append(f"{where}: {imp.module}.{imp.name} exists but is not in its __all__")
    return out


# ── Rail 2: binds ─────────────────────────────────────────────────────────────


def _bind_error(call: SdkCall) -> str | None:
    if isinstance(call.obj, _MissingAttribute):
        return f"{call.target} no longer exists"
    if isinstance(call.obj, _CallableField):
        sig = call.obj.signature
    else:
        try:
            sig = inspect.signature(call.obj)
        except (TypeError, ValueError):
            return None  # a C builtin with no signature: nothing to hold the call to
    params = list(sig.parameters.values())
    if call.bound and params and params[0].kind in (
        inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD
    ):
        params = params[1:]
    sig = sig.replace(parameters=params)
    if call.starred or call.double_starred:
        takes_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params)
        named = {p.name for p in params if p.kind is not inspect.Parameter.POSITIONAL_ONLY}
        unknown = [k for k in call.keywords if k not in named and not takes_kwargs]
        return f"unknown keyword(s) {unknown}" if unknown else None
    try:
        sig.bind(*([None] * call.positional), **dict.fromkeys(call.keywords))
    except TypeError as exc:
        return str(exc)
    return None


def unbindable_calls(app_dir: Path) -> list[str]:
    """Statically resolved call sites that do not bind to the installed signature."""
    out: list[str] = []
    for call in census(app_dir).calls:
        err = _bind_error(call)
        if err:
            where = _where(call.path, call.lineno, app_dir)
            out.append(f"{where}: {call.spelled}(...) → {call.target}: {err}")
    return out


# ── Rail 3: conforms (runtime) ────────────────────────────────────────────────


def _is_test_double(value: Any) -> bool:
    """A ``unittest.mock`` object, or an instance of a class defined in test code (a ``tests/``
    directory, ``test_*.py``, ``conftest.py``): what a test hands the app in place of a core
    object. The app did not choose it, so it says nothing about the app's contract."""
    if isinstance(value, mock.NonCallableMock):
        return True
    module = sys.modules.get(type(value).__module__)
    source = Path(getattr(module, "__file__", None) or "")
    return "tests" in source.parts or source.name.startswith("test_") or source.name == "conftest.py"


def _conforms(value: Any, hint: Any) -> bool:
    """Whether *value* satisfies *hint* as far as a runtime check can honestly tell.

    Shallow on purpose, and permissive wherever the question has no runtime answer (a TypeVar, a
    non-runtime Protocol, a hint that did not resolve): the rail exists to catch a value of the
    WRONG KIND — a log where a list is declared — not to re-type-check the app. A test double
    stands in for anything. One level into a concrete list/tuple/set is checked, because
    ``list[dict]`` holding the wrong elements is the same break one level down.
    """
    if hint is Any or hint is object or hint is inspect.Parameter.empty:
        return True
    if _is_test_double(value):
        return True
    if hint is None or hint is type(None):
        return value is None
    origin = typing.get_origin(hint)
    if origin is typing.Union or origin is types.UnionType:
        return any(_conforms(value, arg) for arg in typing.get_args(hint))
    if origin is typing.Literal:
        return value in typing.get_args(hint)
    if origin is typing.Annotated:
        return _conforms(value, typing.get_args(hint)[0])
    if origin is type:
        return isinstance(value, type)
    if origin is collections.abc.Callable:
        return callable(value)
    if origin is not None:
        if not isinstance(origin, type):
            return True
        try:
            if not isinstance(value, origin):
                return False
        except TypeError:
            return True
        args = typing.get_args(hint)
        if len(args) == 1 and isinstance(value, (list, tuple, set, frozenset)) and value:
            return _conforms(next(iter(value)), args[0])
        return True
    if isinstance(hint, type):
        if hint is float and isinstance(value, int):
            return True  # PEP 484: an int is acceptable where a float is declared
        try:
            return isinstance(value, hint)
        except TypeError:
            return True
    return True


@functools.cache
def _signature_and_hints(fn: Any) -> tuple[inspect.Signature, dict[str, Any]] | None:
    try:
        return inspect.signature(fn), _hints(fn)
    except (TypeError, ValueError):
        return None


@dataclass
class SdkCallRecorder:
    """What one test's APP-originated SDK calls did: the call sites seen and any non-conformance."""

    app_dir: Path
    violations: list[str] = field(default_factory=list)
    seen: collections.Counter = field(default_factory=collections.Counter)

    def _from_app(self, frame: types.FrameType | None) -> tuple[Path, int] | None:
        if frame is None:
            return None
        path = _shipped_file(frame.f_code.co_filename, self.app_dir)
        return None if path is None else (path, frame.f_lineno)

    def check(self, target: str, fn: Any, args: tuple, kwargs: dict, frame: types.FrameType | None) -> None:
        origin = self._from_app(frame)
        if origin is None:
            return
        self.seen[(str(origin[0]), origin[1], target)] += 1
        sig_hints = _signature_and_hints(fn)
        if sig_hints is None:
            return
        sig, hints = sig_hints
        where = _where(origin[0], origin[1], self.app_dir)
        try:
            bound = sig.bind(*args, **kwargs)
        except TypeError as exc:
            self.violations.append(f"{where}: {target}: {exc}")
            return
        for name, value in bound.arguments.items():
            param = sig.parameters[name]
            if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            hint = hints.get(name)
            if hint is not None and not _conforms(value, hint):
                self.violations.append(
                    f"{where}: {target}({name}=…) was passed {type(value).__qualname__}, "
                    f"the installed core declares {_render(hint)}"
                )


@functools.lru_cache(maxsize=4096)
def _shipped_file(filename: str, app_dir: Path) -> Path | None:
    """*filename* as a shipped file of the bundle at *app_dir*, or ``None`` (core, a test, …).

    Cached per file: every call into a patched method comes through here, core's own included."""
    path = Path(filename)
    try:
        rel = path.resolve().relative_to(app_dir)
    except (ValueError, OSError):
        return None
    if rel.parts[0] == "tests" or path.name.startswith("test_") or path.name == "conftest.py":
        return None
    return path


def _render(hint: Any) -> str:
    """``list[dict]``, ``ConversationLog | None``: a generic alias forwards ``__qualname__`` to its
    origin (``list``), which would drop the half of the declaration that was violated."""
    if typing.get_origin(hint) is None and isinstance(hint, type):
        return hint.__qualname__
    return repr(hint).replace("typing.", "")


def _checked(target: str, fn: Any, recorder: SdkCallRecorder) -> Any:
    """*fn* wrapped so each call is checked first; async functions stay async."""
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def _async(*args: Any, **kwargs: Any) -> Any:
            recorder.check(target, fn, args, kwargs, sys._getframe(1))
            return await fn(*args, **kwargs)

        return _async

    @functools.wraps(fn)
    def _sync(*args: Any, **kwargs: Any) -> Any:
        recorder.check(target, fn, args, kwargs, sys._getframe(1))
        return fn(*args, **kwargs)

    return _sync


def import_shipped_packages(app_dir: Path) -> None:
    """Import every shipped module that lives in a package under *app_dir* (``slack_runtime.*``).

    Done BEFORE any patching, so a module's top-level ``from personalclaw.sdk… import f`` binds the
    real ``f``. A module first imported mid-test would otherwise bind that test's wrapper and keep
    it after the test unwound — checking later calls into a recorder nobody reads.
    """
    for path in shipped_sources(app_dir):
        parts = path.relative_to(app_dir).with_suffix("").parts
        if len(parts) < 2 or not (app_dir / parts[0] / "__init__.py").is_file():
            continue
        importlib.import_module(".".join(p for p in parts if p != "__init__"))


@contextlib.contextmanager
def record_sdk_calls(app_dir: Path, monkeypatch: Any) -> Iterator[SdkCallRecorder]:
    """Check every app-originated call into the SDK functions and methods the census found.

    Functions are patched on the SDK facade AND in every imported app module that bound them at
    import time; methods are patched on the class the census resolved them on. The patches are
    *monkeypatch*'s, so they unwind with the test.
    """
    app_dir = app_dir.resolve()
    recorder = SdkCallRecorder(app_dir)
    found = census(app_dir)
    import_shipped_packages(app_dir)
    wrapped: dict[int, Any] = {}

    def wrapper_for(fn: Any, target: str) -> Any:
        if id(fn) not in wrapped:
            wrapped[id(fn)] = _checked(target, fn, recorder)
        return wrapped[id(fn)]

    # Module-level SDK functions, wherever the app can reach them from. Async GENERATOR functions
    # are left alone: a plain wrapper would hand back the generator but stop reading as one.
    def wrappable(obj: Any) -> bool:
        return inspect.isfunction(obj) and not inspect.isasyncgenfunction(obj)

    functions: dict[int, tuple[Any, str]] = {}
    for imp in found.imports:
        with contextlib.suppress(LookupError):
            obj = _resolve_import(imp)
            if wrappable(obj):
                functions[id(obj)] = (obj, f"{imp.module}.{imp.name}")
    for call in found.calls:
        if call.owner is None and wrappable(call.obj):
            functions.setdefault(id(call.obj), (call.obj, call.target))
    app_modules = [
        m for m in list(sys.modules.values())
        if getattr(m, "__file__", None) and _inside(m.__file__, app_dir)
    ]
    sdk_modules = [m for name, m in list(sys.modules.items()) if _is_sdk(name) and m is not None]
    replacements = {key: wrapper_for(fn, target) for key, (fn, target) in functions.items()}
    for module in (*sdk_modules, *app_modules):
        for name, value in list(vars(module).items()):
            replacement = replacements.get(id(value))
            if replacement is not None:
                monkeypatch.setattr(module, name, replacement)

    # Methods, on the class the call resolved against — once per (class, name).
    patched: set[tuple[int, str]] = set()
    for call in found.calls:
        if call.owner is None or not call.attr or (id(call.owner), call.attr) in patched:
            continue
        patched.add((id(call.owner), call.attr))
        raw = inspect.getattr_static(call.owner, call.attr)
        if isinstance(raw, staticmethod) and wrappable(raw.__func__):
            monkeypatch.setattr(call.owner, call.attr, staticmethod(wrapper_for(raw.__func__, call.target)))
        elif isinstance(raw, classmethod) and wrappable(raw.__func__):
            monkeypatch.setattr(call.owner, call.attr, classmethod(wrapper_for(raw.__func__, call.target)))
        elif wrappable(raw):
            monkeypatch.setattr(call.owner, call.attr, wrapper_for(raw, call.target))
    yield recorder


@functools.lru_cache(maxsize=8192)
def _inside(filename: str, root: Path) -> bool:
    """Whether *filename* lies under *root* — asked of every loaded module, once per test."""
    try:
        Path(filename).resolve().relative_to(root)
    except (ValueError, OSError):
        return False
    return True
