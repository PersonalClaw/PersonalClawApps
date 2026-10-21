"""Every app that declares what its tools do past this machine needs a PersonalClaw that reads it.

A tool declares what a call may do past this machine in ``ToolDefinition.consequences``: send
something to anyone but the owner, spend money, or neither. A tool that declares nothing is taken
to be able to do both, so every call to it that is not a read asks the owner a second time. The
field arrived in PersonalClaw 0.2.3, and no core feature names it: on a PersonalClaw before it,
``ToolDefinition(…, consequences=…)`` is a keyword the constructor does not take, so the app's
tool list fails and every one of its tools is gone. So every app whose shipped code passes it sets
``minPersonalClawVersion`` to 0.2.3 or later, and PersonalClaw refuses to install or switch on the
app where its tools would not load.

Core reads a declaration strictly (``consequences_of``): one it cannot follow is nothing declared,
and asks. So every declaration in the shipped code must read, through core's own reader at the
commit ``core.lock`` pins, as exactly the kinds it names.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

from packaging.version import InvalidVersion, Version

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # for apps_testkit

from apps_testkit import sdk_contract  # noqa: E402

# Core's own reading of a declaration, at the commit core.lock pins: the differential below holds
# every declaration to what core makes of it, not to what its own table says.
from personalclaw.tool_providers.base import consequences_of  # noqa: E402

BUNDLES = sorted(manifest.parent for manifest in ROOT.glob("*/app.json"))

#: The first PersonalClaw release whose ``ToolDefinition`` takes ``consequences``.
FIRST_CORE_THAT_READS_THEM = Version("0.2.3")

#: The apps that declare their tools' consequences as this rail lands: a declarer the scan stopped
#: recognising would leave the rail checking nothing.
KNOWN_DECLARERS = {
    "code-review",
    "companion",
    "design-critique",
    "docs-slides",
    "issue-radar",
    "notes",
    "ops",
    "research-lab",
    "spec-builder",
    "web-tools",
}


#: What a declaration this scan cannot read as a literal is recorded as.
UNREADABLE = object()


def _declarations(bundle: Path) -> list[tuple[str, object]]:
    """Each ``consequences=`` a ``ToolDefinition`` in the bundle's shipped code passes, as
    ``(where, value)``: the literal it names, or :data:`UNREADABLE` for an expression."""
    found: list[tuple[str, object]] = []
    for path in sdk_contract.shipped_sources(bundle):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "ToolDefinition":
                continue
            for kw in node.keywords:
                if kw.arg == "consequences":
                    where = f"{bundle.name}/{path.relative_to(bundle)}:{kw.value.lineno}"
                    try:
                        found.append((where, ast.literal_eval(kw.value)))
                    except ValueError:
                        found.append((where, UNREADABLE))
    return found


def _declares_consequences(bundle: Path) -> bool:
    """Whether the bundle's shipped code passes ``consequences=`` to a ``ToolDefinition``."""
    return bool(_declarations(bundle))


def _reads_as_written(value: object) -> bool:
    """Whether core reads *value* as exactly the kinds it names: a list or tuple of them, each
    spelled as core spells it. Anything else core reads as nothing declared, which asks."""
    if value is UNREADABLE or not isinstance(value, (list, tuple)):
        return False
    read = consequences_of(value)
    return read is not None and set(read) == set(value)


def _floor(bundle: Path) -> Version | None:
    manifest = json.loads((bundle / "app.json").read_text(encoding="utf-8"))
    try:
        return Version(str(manifest.get("minPersonalClawVersion") or ""))
    except InvalidVersion:
        return None


def test_every_app_that_declares_its_tools_consequences_needs_a_core_that_reads_them():
    declarers = {bundle.name for bundle in BUNDLES if _declares_consequences(bundle)}
    assert KNOWN_DECLARERS <= declarers, (
        f"the scan no longer sees these apps declare their tools' consequences: "
        f"{sorted(KNOWN_DECLARERS - declarers)}"
    )
    too_low = {}
    for name in sorted(declarers):
        floor = _floor(ROOT / name)
        if floor is None or floor < FIRST_CORE_THAT_READS_THEM:
            too_low[name] = str(floor) if floor else "none"
    assert too_low == {}, (
        f"these apps pass ToolDefinition(consequences=...) but would install on a PersonalClaw "
        f"whose tools cannot take it; set 'minPersonalClawVersion' to at least "
        f"'{FIRST_CORE_THAT_READS_THEM}' in app.json: {too_low}"
    )


def test_every_declaration_reads_through_cores_strict_reader_as_written():
    """The differential. Core reads a declaration strictly (``consequences_of``): a kind it does not
    know, a case variant, a bare string, a set or a value it cannot see is nothing declared, and
    every call that is not a read asks a second time. So a tool meant to declare "sends" but
    misspelt asks for every call while its own table says "sends", and one meant to declare
    neither asks too. Every declaration in the shipped code reads, through core's reader, as
    exactly the kinds it names."""
    declared = [d for bundle in BUNDLES for d in _declarations(bundle)]
    assert len(declared) >= len(KNOWN_DECLARERS), declared
    unread = {where: repr(value) for where, value in declared if not _reads_as_written(value)}
    assert unread == {}, (
        f"core reads these declarations as nothing declared, so each call asks a second time; "
        f"name each kind as a tuple of 'sends' and 'spends', spelled exactly: {unread}"
    )


def test_the_differential_tells_a_declaration_core_reads_from_one_it_does_not(tmp_path):
    """Positive and negative control: the declarations core reads as written, and the near-misses
    it reads as none."""
    source = (
        "from personalclaw.sdk.tool import ToolDefinition\n"
        "KINDS = ('sends',)\n"
        "TOOLS = [\n"
        "    ToolDefinition(name='a', description='d', consequences=()),\n"
        "    ToolDefinition(name='b', description='d', consequences=('sends',)),\n"
        "    ToolDefinition(name='c', description='d', consequences=('spends', 'sends')),\n"
        "    ToolDefinition(name='d', description='d', consequences=['sends']),\n"
        "    ToolDefinition(name='e', description='d', consequences=('send',)),\n"
        "    ToolDefinition(name='f', description='d', consequences='sends'),\n"
        "    ToolDefinition(name='g', description='d', consequences=('Sends',)),\n"
        "    ToolDefinition(name='h', description='d', consequences={'sends'}),\n"
        "    ToolDefinition(name='i', description='d', consequences=KINDS),\n"
        "    ToolDefinition(name='j', description='d', consequences=None),\n"
        "]\n"
    )
    app = tmp_path / "planted-app"
    app.mkdir()
    (app / "provider.py").write_text(source, encoding="utf-8")
    read = {where.rsplit(":", 1)[1]: _reads_as_written(value) for where, value in _declarations(app)}
    assert read == {
        "4": True, "5": True, "6": True, "7": True,
        "8": False, "9": False, "10": False, "11": False, "12": False, "13": False,
    }, read


def test_the_scan_tells_an_app_that_declares_consequences_from_one_that_does_not(tmp_path):
    """Positive and negative control: a declaration by name and through the module, and a tool that
    declares only its risk."""
    sources = {
        "named-app": (
            "from personalclaw.sdk.tool import ToolDefinition\n"
            "TOOL = ToolDefinition(name='t', description='d', consequences=())\n"
        ),
        "module-app": (
            "from personalclaw.sdk import tool\n"
            "TOOL = tool.ToolDefinition(name='t', description='d', consequences=('sends',))\n"
        ),
        "other-app": (
            "from personalclaw.sdk.tool import RiskLevel, ToolDefinition\n"
            "TOOL = ToolDefinition(name='t', description='d', risk_level=RiskLevel.SAFE)\n"
        ),
    }
    seen = {}
    for name, source in sources.items():
        app = tmp_path / name
        app.mkdir()
        (app / "app.json").write_text(f'{{"name": "{name}", "version": "0.1.0"}}')
        (app / "provider.py").write_text(source, encoding="utf-8")
        seen[name] = _declares_consequences(app)
    assert seen == {"named-app": True, "module-app": True, "other-app": False}


def test_a_floor_that_is_missing_or_not_a_version_is_no_floor(tmp_path):
    for raw, expected in (("", None), ("latest", None), ("0.2.2", Version("0.2.2"))):
        app = tmp_path / f"app-{raw or 'none'}"
        app.mkdir()
        (app / "app.json").write_text(json.dumps({"name": app.name, "minPersonalClawVersion": raw}))
        assert _floor(app) == expected
