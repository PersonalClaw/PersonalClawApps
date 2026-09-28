"""The publication-hygiene rail reds on a planted defect, and the tree is clean.

The rail is ``.github/scripts/check_publication_hygiene.py``, which CI runs as a script. These
tests drive that same script, unmodified, against seeded git repositories: a copy sits at the
same place in each, so its ``git ls-files`` input is exactly the seeded tree.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github" / "scripts" / "check_publication_hygiene.py"


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


def _check(repo: Path) -> subprocess.CompletedProcess:
    """Run the script in *repo*, as CI runs it."""
    script = repo / ".github" / "scripts" / SCRIPT.name
    return subprocess.run([sys.executable, str(script)], cwd=repo, capture_output=True, text=True)


def test_the_tracked_tree_is_fit_to_publish():
    """The real repository through the real script, exactly as CI runs it."""
    run = _check(ROOT)
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.splitlines()[-1].startswith("OK: "), run.stdout


def test_a_clean_seeded_tree_is_green(tmp_path):
    """Vacuity. The seeded harness must be able to go green, or every red below proves nothing."""
    repo = _seeded(tmp_path, {"docs/notes.md": "Never trust an upload's declared type.\n"})
    run = _check(repo)
    assert run.returncode == 0, run.stdout
    assert run.stdout.splitlines() == ["OK: 2 tracked paths, none unfit to publish"], run.stdout


def test_a_real_home_path_reds(tmp_path):
    """An absolute path into a home whose owner is on no placeholder list reds, naming the file."""
    owner = "zz" + "notaplaceholder" + "zz"
    run = _check(_seeded(tmp_path, {"tests/leak.py": f"HOME = '/Users/{owner}'\n"}))
    assert run.returncode == 1, run.stdout
    assert f"real-home-path: tests/leak.py names home directory '{owner}'" in run.stdout, run.stdout


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
    run = _check(_seeded(tmp_path, {path: content()}))
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
    run = _check(_seeded(tmp_path, {path: content()}))
    assert run.returncode == 0, run.stdout


def test_the_home_path_rule_reads_an_office_documents_parts(tmp_path):
    """A home path in a template field sits in a COMPRESSED part; it reds, located at the part."""
    owner = "zz" + "notaplaceholder" + "zz"
    blob = _ooxml({"docProps/app.xml": _app(template=f"/Users/{owner}/t.dotx")})
    run = _check(_seeded(tmp_path, {"docs/a.docx": blob}))
    assert run.returncode == 1, run.stdout
    assert f"real-home-path: docs/a.docx!docProps/app.xml names home directory '{owner}'" in run.stdout
