"""Every app's ``availability()`` answers from metadata, and names only install paths that exist.

The SDK's contract for the hook (``personalclaw.sdk.availability``) is "never import the library
you are checking for": importing a machine-learning stack runs its whole initialisation, and a hook
that imported sentence-transformers took 171.8 s cold, on a question every Models page asks. Four
first-party hooks imported their libraries all the same (faster-whisper, sentence-transformers,
pyannote and piper's download library). And one told the user to install
``personalclaw[diarization-pyannote]``, an extra PersonalClaw has never declared.
"""

from __future__ import annotations

import ast
import importlib.metadata
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: What an app's text names as PersonalClaw's extras, with any extras list in the brackets.
_EXTRA = re.compile(r"personalclaw\[([A-Za-z0-9_.,\s-]+)\]")
#: The files an app's words are in.
_TEXT = {".py", ".md", ".json", ".toml", ".txt"}
_SKIP = {"node_modules", "bundle", "__pycache__", ".venv"}


def _bundles() -> list[Path]:
    return sorted(p.parent for p in ROOT.glob("*/app.json"))


def _hooks() -> dict[str, ast.FunctionDef]:
    """``<bundle>/<file>`` → its module-level ``availability`` function, for every bundle."""
    found: dict[str, ast.FunctionDef] = {}
    for bundle in _bundles():
        for path in sorted(bundle.glob("*.py")):
            if path.name.startswith("test_"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if isinstance(node, ast.FunctionDef) and node.name == "availability":
                    found[str(path.relative_to(ROOT))] = node
    return found


def _imports(hook: ast.FunctionDef) -> list[str]:
    return [
        f"line {node.lineno}: {ast.unparse(node)}"
        for node in ast.walk(hook)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]


def test_no_availability_hook_imports_anything():
    importing = {where: lines for where, hook in _hooks().items() if (lines := _imports(hook))}
    assert not importing, (
        "these availability() hooks import inside the hook; find what is installed with "
        f"personalclaw.sdk.availability.missing_modules instead: {importing}"
    )


def test_the_rail_reads_every_hook():
    """The control: the hooks are found, so a rail that read nothing cannot pass."""
    found = {where.split("/")[0] for where in _hooks()}
    for bundle in (
        "faster-whisper",
        "sentence-transformers",
        "diarization-pyannote",
        "diarization-onnx",
        "piper-tts",
        "gemini-cli-agent",
        "kiro-cli-agent",
    ):
        assert bundle in found, f"{bundle}'s availability hook is no longer found: {sorted(found)}"


def test_the_rail_reds_on_a_hook_that_imports_its_library():
    old = ast.parse(
        "def availability():\n"
        "    try:\n"
        "        import faster_whisper  # noqa: F401\n"
        "        return True, ''\n"
        "    except ImportError:\n"
        "        return False, 'needs faster-whisper'\n"
    ).body[0]
    assert isinstance(old, ast.FunctionDef) and _imports(old)


def _named_extras(text: str) -> set[str]:
    return {
        extra.strip()
        for match in _EXTRA.finditer(text)
        for extra in match.group(1).split(",")
        if extra.strip()
    }


def _app_text() -> dict[str, str]:
    """What an app tells its user: its code, manifest and documents. Its tests are not, and a test
    may name a wrong extra to assert it is gone."""
    files: dict[str, str] = {}
    for bundle in _bundles():
        for path in bundle.rglob("*"):
            if path.suffix not in _TEXT or not path.is_file() or path.name.startswith("test_"):
                continue
            if not _SKIP & set(path.parts):
                files[str(path.relative_to(ROOT))] = path.read_text(encoding="utf-8", errors="ignore")
    return files


def test_every_extra_an_app_names_is_one_personalclaw_declares():
    declared = set(importlib.metadata.metadata("personalclaw").get_all("Provides-Extra") or [])
    assert {"stt", "tts", "embeddings"} <= declared, f"control: core's extras read {declared}"
    unknown = {
        where: sorted(extras - declared)
        for where, text in _app_text().items()
        if (extras := _named_extras(text)) - declared
    }
    assert not unknown, f"these name an extra PersonalClaw does not declare: {unknown}"


def test_the_extra_rail_reds_on_an_extra_that_does_not_exist():
    assert _named_extras("needs personalclaw[diarization-pyannote] (torch)") == {
        "diarization-pyannote"
    }
    assert _named_extras('pip install "personalclaw[stt, tts]"') == {"stt", "tts"}
