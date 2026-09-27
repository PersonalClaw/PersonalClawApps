"""The publication-hygiene rail reds on a planted internal reference, and the tree is clean.

The rail is ``.github/scripts/check_publication_hygiene.py``, which CI runs as a script. These
tests drive that same script, unmodified, against seeded git repositories: a copy sits at the
same place in each, so its ``git ls-files`` input is exactly the seeded tree.

The internal-reference rule's vocabulary is PRIVATE: it lives in a file outside every repository,
which ``PERSONALCLAW_PRIVATE_DENYLIST`` names, so this public suite never sees it. Its controls
hand the script a list of INVENTED words instead, through that same variable; the real tree meets
the real list only where the variable is set (the maintainer's landing), and the rule is skipped,
with a notice, everywhere else.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github" / "scripts" / "check_publication_hygiene.py"
ENV = "PERSONALCLAW_PRIVATE_DENYLIST"

#: One invented entry per kind, as the real list spells its entries, and an invented salt. None
#: of these names anything, and none may ever be swapped for a real name: this file is public.
_SYNTHETIC = {
    "word": "zzbrindlecopse",
    "phrase": "zzmarrowfen quelt",
    "host": "zzwhitlowgate.invalid",
    "code": "zzv-17",
    "id": "zzv_Qm4pR8sT2wX6yZ",
}
_SALT = "zzsynthetic/publication-hygiene/v0"
_LIST = "".join(f"{kind:<7} {text}\n" for kind, text in _SYNTHETIC.items()) + f"salt    {_SALT}\n"

_PLANTED = {
    "word": "# Validated per the ZZBRINDLECOPSE guidance.\n",
    "phrase": "# Sign in with zzmarrowfen\n#   quelt before a run.\n",
    "host": "See https://docs.zzwhitlowgate.invalid/page for the rule.\n",
    "code": "# (ZZV-17 boundary validation)\n",
    "id": "# cited as ``zzv_Qm4pR8sT2wX6yZ``\n",
}


def _seeded(tmp_path: Path, files: dict[str, str | bytes]) -> Path:
    """A real git repository holding the rail's script and exactly *files*, all tracked."""
    repo = tmp_path / "repo"
    script = repo / ".github" / "scripts" / SCRIPT.name
    script.parent.mkdir(parents=True)
    shutil.copy2(SCRIPT, script)
    for rel, content in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            (repo / rel).write_bytes(content)
        else:
            (repo / rel).write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "add", "-A", "-f"], cwd=repo, check=True)
    return repo


def _listed(tmp_path: Path, text: str = _LIST) -> Path:
    path = tmp_path / "private-denylist.txt"
    path.write_text(text, encoding="utf-8")
    return path


def _check(repo: Path, denylist: Path | str | None) -> subprocess.CompletedProcess:
    """Run the script in *repo*. *denylist* is required, so no test inherits the developer's
    own ``PERSONALCLAW_PRIVATE_DENYLIST`` by accident."""
    script = repo / ".github" / "scripts" / SCRIPT.name
    env = {k: v for k, v in os.environ.items() if k != ENV}
    if denylist is not None:
        env[ENV] = str(denylist)
    return subprocess.run(
        [sys.executable, str(script)], cwd=repo, capture_output=True, text=True, env=env
    )


def test_the_tracked_tree_is_fit_to_publish():
    """The real repository through the real script, exactly as CI runs it — and, where the
    maintainer's environment names the private list, with the internal-reference rule too."""
    run = _check(ROOT, os.environ.get(ENV))
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.splitlines()[-1].startswith("OK: "), run.stdout


def test_a_clean_seeded_tree_is_green(tmp_path):
    """Vacuity. The seeded harness must be able to go green, or every red below proves nothing."""
    repo = _seeded(tmp_path, {"docs/notes.md": "Never trust an upload's declared type.\n"})
    run = _check(repo, _listed(tmp_path))
    assert run.returncode == 0, run.stdout
    assert run.stdout.splitlines()[-1].endswith("(5 private internal-reference entries checked)")


@pytest.mark.parametrize("kind", sorted(_PLANTED))
def test_a_planted_internal_reference_reds(tmp_path, kind):
    """One positive control per kind. The planted line is line 2, and the phrase, wrapped across
    a line break, must be placed on its first word's line."""
    repo = _seeded(tmp_path, {"docs/notes.md": "An ordinary line.\n" + _PLANTED[kind]})
    run = _check(repo, _listed(tmp_path))
    assert run.returncode == 1, run.stdout
    assert f"internal-reference: docs/notes.md:2 names a denied {kind} " in run.stdout, run.stdout


def test_a_planted_name_in_a_path_reds(tmp_path):
    """A file NAME is published exactly as its content is."""
    name = "docs/" + _SYNTHETIC["word"] + "-notes.md"
    run = _check(_seeded(tmp_path, {name: "Ordinary content.\n"}), _listed(tmp_path))
    assert run.returncode == 1, run.stdout
    assert f"internal-reference: {name} (its PATH)" in run.stdout, run.stdout


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


_WORD = _SYNTHETIC["word"].encode("utf-8")
_ENCODED = {
    "md5": (hashlib.md5(_WORD).hexdigest(), "md5", "word"),
    "sha256": (_sha(_WORD), "sha256", "word"),
    "sha512": (hashlib.sha512(_WORD).hexdigest(), "sha512", "word"),
    "a-base64-digest": (
        base64.b64encode(hashlib.sha256(_WORD).digest()).decode("ascii"),
        "base64 sha256",
        "word",
    ),
    "a-salted-digest": (
        hashlib.sha256(f"{_SALT}\0word\0{_SYNTHETIC['word']}".encode("utf-8")).hexdigest(),
        "salted sha256",
        "word",
    ),
    "base64": (base64.b64encode(_WORD).decode("ascii"), "base64", "word"),
    "hex": (_WORD.hex(), "hex", "word"),
    "a-head-on-its-own": (_sha(b"zzmarrowfen"), "sha256", "phrase-head"),
    "an-echoed-newline": (_sha(_WORD + b"\n"), "sha256", "word"),
}


@pytest.mark.parametrize("case", sorted(_ENCODED))
def test_an_encoded_entry_reds(tmp_path, case):
    """A digest of an entry (hex or base64, plain or salted with a salt the list names) or its
    base64 or hex keeps the name in the tree as surely as the name does."""
    token, encoding, kind = _ENCODED[case]
    repo = _seeded(tmp_path, {"docs/notes.md": f"An ordinary line.\nDENIED = {{'{token}'}}\n"})
    run = _check(repo, _listed(tmp_path))
    assert run.returncode == 1, run.stdout
    expected = f"internal-reference: docs/notes.md:2 carries the {encoding} of a denied {kind} "
    assert expected in run.stdout, run.stdout


def test_an_encoded_entry_in_a_path_reds(tmp_path):
    """A file named after a digest publishes it as surely as a line does."""
    name = f"docs/{_sha(_WORD)}.json"
    run = _check(_seeded(tmp_path, {name: "{}\n"}), _listed(tmp_path))
    assert run.returncode == 1, run.stdout
    expected = f"internal-reference: {name} (its PATH) carries the sha256 of a denied word"
    assert expected in run.stdout, run.stdout


def _load_rail():
    spec = importlib.util.spec_from_file_location("hygiene_rail", SCRIPT)
    rail = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(rail)
    return rail


def test_the_detector_floor_catches_a_matcher_that_fires_on_nothing_or_everything(monkeypatch):
    """The floor is what makes a clean scan believable, so it must itself red: on a matcher that
    finds nothing (every control and every encoding goes quiet) and on one that finds everything
    (every near miss and the unlisted digest start to red)."""
    rail = _load_rail()
    assert rail._detector_floor() == []
    with monkeypatch.context() as patched:
        patched.setattr(rail._InternalMatcher, "references", lambda self, text: [])
        patched.setattr(rail._InternalMatcher, "encodings", lambda self, text: [])
        quiet = rail._detector_floor()
    assert sum("control no longer fires" in f for f in quiet) == 5, quiet
    assert sum("of a denied word no longer fires" in f for f in quiet) == 3, quiet
    with monkeypatch.context() as patched:
        patched.setattr(rail._InternalMatcher, "references", lambda self, t: [("word", t)])
        patched.setattr(rail._InternalMatcher, "encodings", lambda self, t: [("md5", "word", t)])
        loud = rail._detector_floor()
    assert sum("falsely catches" in f for f in loud) == 6, loud


def test_without_the_list_the_rule_is_skipped_and_the_rest_runs(tmp_path):
    """Unset, the internal-reference rule alone is skipped, and says so: the same tree still
    reds on a real home path, and is silent on the planted name the list would catch."""
    owner = "zz" + "notaplaceholder" + "zz"
    repo = _seeded(
        tmp_path,
        {
            "docs/notes.md": "An ordinary line.\n" + _PLANTED["word"],
            "tests/leak.py": f"HOME = '/Users/{owner}'\n",
        },
    )
    run = _check(repo, None)
    assert run.returncode == 1, run.stdout
    assert "real-home-path: tests/leak.py" in run.stdout, run.stdout
    assert "internal-reference:" not in run.stdout, run.stdout
    notice = "publication-hygiene: internal-reference rule skipped: " + ENV + " is not set"
    assert run.stdout.splitlines()[0].startswith(notice), run.stdout
    named = _check(repo, _listed(tmp_path))
    assert "internal-reference: docs/notes.md:2 names a denied word" in named.stdout, named.stdout
    assert "skipped" not in named.stdout


@pytest.mark.parametrize(
    "text, reason",
    [
        ("# only a comment\n", "holds no entry"),
        (f"salt {_SALT}\n", "holds no entry"),
        (_LIST + "word\n", ":7: a line is KIND, whitespace, then the TEXT"),
        (_LIST + "colour zzcerulean\n", ":7: unknown kind 'colour'"),
        (_LIST + "phrase zzsolitary\n", ":7: a phrase is exactly two words"),
    ],
    ids=["no-entry", "only-a-salt", "no-text", "unknown-kind", "a-one-word-phrase"],
)
def test_an_unusable_list_fails_and_never_skips(tmp_path, text, reason):
    """A named list that cannot be read as written fails the rail — a typo must never turn
    "checked" into "silently passed", even among good lines — and the refusal names the line,
    never the private entry on it."""
    repo = _seeded(tmp_path, {"docs/notes.md": "Ordinary.\n"})
    run = _check(repo, _listed(tmp_path, text))
    assert run.returncode == 1, run.stdout
    assert "the private denylist is unusable" in run.stdout and "skipped" not in run.stdout
    assert reason in run.stdout, run.stdout
    assert "zzsolitary" not in run.stdout and "zzcerulean" not in run.stdout
    missing = _check(repo, tmp_path / "missing.txt")
    assert missing.returncode == 1 and "cannot be read" in missing.stdout, missing.stdout


def test_the_script_publishes_no_vocabulary():
    """No entry, digest or salt of the private vocabulary is in the rail itself: its only
    words are the invented ones, and it holds no digest at all."""
    source = SCRIPT.read_text(encoding="utf-8")
    assert "_INTERNAL_DENIED" not in source and "_INTERNAL_SALT" not in source
    assert not re.search(r"[0-9a-f]{32,}", source), "the rail carries a digest"


def test_the_private_lists_encoded_forms_fire():
    """Vacuity for the tree test, with the REAL list: a digest of a real entry, plain and salted
    with each salt the list names, built in memory, must red — so a green tree test is the tree
    being clean, not the encoded forms never matching anything."""
    path = os.environ.get(ENV, "").strip()
    if not path:
        pytest.skip(f"{ENV} is not set: the vocabulary is private")
    rail = _load_rail()
    listed = rail._parse_denylist(Path(path).expanduser().read_text(encoding="utf-8"), path)
    kind = next(k for k in rail._ENTRY_KINDS if listed[k])
    entry = min(listed[kind])
    planted = [_sha(entry.encode("utf-8"))] + [
        rail._salted(salt, kind, entry) for salt in sorted(listed["salt"])
    ]
    found = rail._InternalMatcher(listed).encodings("\n".join(planted))
    assert sorted((encoding, hit) for encoding, hit, _ in found) == sorted(
        [("sha256", kind)] + [("salted sha256", kind)] * len(listed["salt"])
    )


# ── office documents: metadata no text rule can read ─────────────────────────────────────

#: A made-up value for a planted identity field: on no placeholder list, and nobody's name.
_NAME = "zz-planted-person"
_WORDML = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_ODF = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/"'
)
_COMPOUND = bytes.fromhex("d0cf11e0a1b11ae1") + b"\0" * 504


def _zip(parts: dict[str, str | bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    return out.getvalue()


def _core(creator: str = "", modifier: str = "") -> str:
    return (
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/'
        'core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"<dc:creator>{creator}</dc:creator><cp:lastModifiedBy>{modifier}</cp:lastModifiedBy>"
        "</cp:coreProperties>"
    )


def _app(company: str = "", manager: str = "", template: str = "Normal.dotm") -> str:
    return (
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/'
        f'extended-properties"><Template>{template}</Template><Company>{company}</Company>'
        f"<Manager>{manager}</Manager></Properties>"
    )


def _body(inner: str) -> str:
    return f'<w:document xmlns:w="{_WORDML}"><w:body>{inner}</w:body></w:document>'


def _ooxml(parts: dict[str, str | bytes] | None = None) -> bytes:
    """A minimal, clean Office Open XML package, with *parts* replacing or adding parts."""
    return _zip(
        {
            "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/'
            '2006/content-types"/>',
            "_rels/.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/'
            '2006/relationships"/>',
            "docProps/core.xml": _core(),
            "docProps/app.xml": _app(),
            "word/document.xml": _body(""),
            **(parts or {}),
        }
    )


def _custom(name: str) -> str:
    return (
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/'
        f'custom-properties"><property pid="2" name="{name}"/></Properties>'
    )


def _odt(meta: str) -> bytes:
    return _zip(
        {
            "mimetype": "application/vnd.oasis.opendocument.text",
            "meta.xml": (
                f"<office:document-meta {_ODF}><office:meta>{meta}</office:meta>"
                "</office:document-meta>"
            ),
        }
    )


@pytest.mark.parametrize(
    "path, content, problem",
    [
        pytest.param(
            "docs/a.docx",
            lambda: _ooxml({"docProps/custom.xml": _custom("zzControl")}),
            "carries custom properties (docProps/custom.xml)",
            id="custom-properties",
        ),
        pytest.param(
            "docs/a.docx",
            lambda: _ooxml({"docProps/custom.xml": _custom("MSIP" + "_Label_0_Enabled")}),
            "carries a sensitivity label (docProps/custom.xml)",
            id="label",
        ),
        pytest.param(
            "docs/a.docx",
            lambda: _ooxml({"docMetadata/LabelInfo.xml": "<labelList/>"}),
            "carries a sensitivity label (docMetadata/LabelInfo.xml)",
            id="label-part",
        ),
        pytest.param(
            "docs/a.docx",
            lambda: _ooxml({"docProps/core.xml": _core(creator=_NAME)}),
            "names someone in <creator> (docProps/core.xml)",
            id="creator",
        ),
        pytest.param(
            "docs/a.docx",
            lambda: _ooxml({"docProps/core.xml": _core(modifier=_NAME)}),
            "names someone in <lastModifiedBy> (docProps/core.xml)",
            id="last-modified-by",
        ),
        pytest.param(
            "docs/a.docx",
            lambda: _ooxml({"docProps/app.xml": _app(company=_NAME)}),
            "names someone in <Company> (docProps/app.xml)",
            id="company",
        ),
        pytest.param(
            "docs/a.docx",
            lambda: _ooxml({"docProps/app.xml": _app(manager=_NAME)}),
            "names someone in <Manager> (docProps/app.xml)",
            id="manager",
        ),
        pytest.param(
            "docs/a.docx",
            lambda: _ooxml({"word/document.xml": _body(f'<w:ins w:id="0" w:author="{_NAME}"/>')}),
            "names someone in <ins author> (word/document.xml)",
            id="revision-author",
        ),
        pytest.param(
            "docs/b.pptx",
            lambda: _ooxml({"ppt/commentAuthors.xml": f'<cmAuthorLst><cmAuthor name="{_NAME}"/></cmAuthorLst>'}),
            "names someone in <cmAuthor name> (ppt/commentAuthors.xml)",
            id="comment-author",
        ),
        pytest.param(
            "docs/c.odt",
            lambda: _odt(f"<meta:initial-creator>{_NAME}</meta:initial-creator>"),
            "names someone in <initial-creator> (meta.xml)",
            id="opendocument-creator",
        ),
        pytest.param(
            "docs/c.odt",
            lambda: _odt('<meta:user-defined meta:name="zzControl">x</meta:user-defined>'),
            "carries custom properties (meta.xml)",
            id="opendocument-custom-property",
        ),
        pytest.param(
            "docs/d.rtf",
            lambda: "{\\rtf1{\\info{\\author " + _NAME + "}}}",
            "names someone in \\author",
            id="rtf-author",
        ),
        pytest.param(
            "docs/e.doc",
            lambda: _COMPOUND,
            "is a compound document, whose author and custom properties this rule cannot read",
            id="binary-office-file",
        ),
        pytest.param(
            "docs/e.bin",
            lambda: _COMPOUND,
            "is a compound document, whose author and custom properties this rule cannot read",
            id="compound-by-content",
        ),
        pytest.param(
            "docs/f.docx",
            lambda: b"PK\x03\x04 cut short",
            "is not readable as the office document its extension names",
            id="truncated",
        ),
        pytest.param(
            "docs/b.pptx",
            lambda: _ooxml({"ppt/embeddings/s.xlsx": _ooxml({"docProps/core.xml": _core(_NAME)})}),
            "names someone in <creator> (docProps/core.xml) inside ppt/embeddings/s.xlsx",
            id="embedded-document",
        ),
        pytest.param(
            "docs/renamed.bin",
            lambda: _ooxml({"docProps/core.xml": _core(creator=_NAME)}),
            "names someone in <creator> (docProps/core.xml)",
            id="renamed-document",
        ),
    ],
)
def test_a_planted_office_document_defect_reds(tmp_path, path, content, problem):
    """One positive control per field and form, through the unmodified script."""
    run = _check(_seeded(tmp_path, {path: content()}), None)
    assert run.returncode == 1, run.stdout
    assert f"office-document: {path} {problem}" in run.stdout, run.stdout
    assert "An office-document finding is fixed in the document" in run.stdout, run.stdout


@pytest.mark.parametrize(
    "path, content",
    [
        pytest.param("docs/a.docx", _ooxml, id="clean-package"),
        pytest.param(
            "docs/a.docx",
            lambda: _ooxml({"word/document.xml": _body('<w:ins w:id="0" w:author="Author"/>')}),
            id="scrubbed-revision-author",
        ),
        pytest.param(
            "docs/a.docx",
            lambda: _ooxml({"docProps/core.xml": _core(creator="python-docx")}),
            id="library-signature",
        ),
        pytest.param(
            "docs/archive.zip",
            lambda: _zip({"docProps/custom.xml": _custom("zzControl")}),
            id="a-zip-that-is-no-document",
        ),
        pytest.param(
            "docs/notes.md",
            lambda: "An RTF file opens with {\\rtf1 and names its {\\author X} inside.\n",
            id="text-that-quotes-rtf",
        ),
    ],
)
def test_an_office_document_naming_no_one_is_green(tmp_path, path, content):
    """The near misses: nothing named, a declared placeholder, a zip that is no document, prose."""
    run = _check(_seeded(tmp_path, {path: content()}), _listed(tmp_path))
    assert run.returncode == 0, run.stdout


def test_the_content_rules_read_an_office_documents_parts(tmp_path):
    """A home path in a template field and a planted internal reference in the body, both in
    COMPRESSED parts, each red under its own rule, located at the part."""
    owner = "zz" + "notaplaceholder" + "zz"
    control = _SYNTHETIC["word"]
    blob = _ooxml(
        {
            "docProps/app.xml": _app(template=f"/Users/{owner}/t.dotx"),
            "word/document.xml": _body(f"<w:p><w:r><w:t>{control}</w:t></w:r></w:p>"),
        }
    )
    run = _check(_seeded(tmp_path, {"docs/a.docx": blob}), _listed(tmp_path))
    assert run.returncode == 1, run.stdout
    assert f"real-home-path: docs/a.docx!docProps/app.xml names home directory '{owner}'" in run.stdout
    assert "internal-reference: docs/a.docx!word/document.xml:1 names a denied word" in run.stdout
