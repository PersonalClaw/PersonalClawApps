#!/usr/bin/env python3
"""Fail when a tracked path is not fit to publish from a PUBLIC repository.

This is a repository-wide fact, so it cannot live in one bundle's test suite: CI runs
pytest separately per bundle, and the defects this catches are precisely the ones that
belong to no bundle. Scan Git's tracked-file list rather than a filesystem glob, so build
artifacts and local worktrees cannot affect the result.

``git ls-files`` is exactly what a clone receives, and ``git rm`` takes a path out of the
tree but never out of history — so the only cheap moment to refuse an unfit path is the PR
that adds it.

What this rail was built from, measured on ``origin/main``:

* ``docs/plans/OPENROUTER-MODELS.md`` — a 1,389-line internal implementation plan carrying
  **five hardcoded ``/Users/<maintainer>/...`` absolute paths**. Its own header read
  "Status: PLAN ONLY — no production code written yet" while ``openrouter-models/`` shipped
  as a complete bundle, so it was stale, false, unreferenced, and a leak of the author's
  machine layout at once. Removed; the core repo had already made the same call when it
  stopped publishing its internal roadmap tree.
* ``.worktrees/`` and ``.local/`` absent from ``.gitignore`` — a plain ``git add -A`` stages
  every linked worktree as an embedded git repository (a gitlink pointing at a local
  absolute path). Reproduced here before the fix by simply creating a worktree.

⚠️  THERE ARE NO EXEMPTIONS — not even for this file. The first version of this rail wrote
    its control home path as a LITERAL, which made the script itself a published real-home
    path, and then bought itself a blanket `(SELF, rule)` exemption to stay green. Six of
    those seven entries were dead weight (this path matches none of the six path rules) and
    the seventh existed only to hide the literal. Both are gone: the control name is now
    assembled at runtime, so nothing needs exempting. A rail that exempts its own file is
    precisely the shape that lets a real defect hide later, because the exempted file is the
    one nobody re-reads.

⚠️  AND THE DETECTOR FLOOR RUNS FIRST. A rail that matched nothing would print OK forever, so
    every pattern is proved against a sample it must catch AND one it must spare before a
    clean repository scan is believed — including a re-check of this script's own source.

An OFFICE DOCUMENT is read as well as tracked: its metadata (custom properties, a sensitivity
label, the creator, last modifier, company and manager, each revision's and comment's author)
sits in compressed XML parts no text rule sees, so the office-document rule opens every one and
refuses it when that metadata names anyone. The home-path rule reads those parts too. Core's
``publication-hygiene-baseline.json`` holds the same rule as policy.

⚠️  NO RULE HERE MATCHES A VOCABULARY. Whether a text names something private (an
    organisation's internal system, a person, a ticket, a private plan) is not a question a
    list of words can answer. A list in this public repository publishes the names it keeps
    out, and so does a list of their digests, which anyone can hash a dictionary of guesses
    against; a list kept anywhere else cannot run where contributors and CI run; and no list
    is ever complete. Keeping such names out is a contributor practice, written in
    ``AGENTS.md`` and ``CONTRIBUTING.md``. Every rule below judges structure instead: a
    path's shape, a file's size, and, for a home path or an office document's identity
    field, an allowlist of declared placeholders rather than a list of the names to keep out.
"""

from __future__ import annotations

import io
import re
import subprocess
import sys
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[2]
SELF = Path(__file__).resolve().relative_to(ROOT).as_posix()

#: Read cap for the content rule. The longest real-home path worth seeing is a few hundred
#: bytes; reading whole multi-megabyte files to find one makes the rail slow enough to be
#: switched off.
_CONTENT_READ_CAP = 2_000_000

#: A tracked binary is permanent, unshrinkable weight in every clone (no Git LFS here).
#: Measured ceiling: the largest tracked binary in this repo is 40 KB. 512 KB leaves ample
#: room for a legitimate app icon while still refusing a committed screenshot dump.
_MAX_BINARY_BYTES = 512 * 1024

#: Path shapes that must never be published. Each is anchored on a full path SEGMENT so an
#: ordinary name that merely starts with a residue word stays green (`template.py` is not
#: `temp/`); `test_the_detector_floor` below pins both directions.
_PATH_RULES: tuple[tuple[str, str], ...] = (
    (
        "residue-name",
        r"(?:^|/)(?:temp|tmp|scratch|wip|old|bak|debug)(?:[-_.][^/]*)?(?:/|$)",
    ),
    ("residue-suffix", r"(?:\.(?:orig|rej|bak|tmp|swp|swo)|~)$"),
    (
        "build-output-or-cache",
        r"(?:^|/)(?:dist|build|node_modules|__pycache__|htmlcov|coverage|reports|"
        r"\.pytest_cache|\.mypy_cache|\.hypothesis|\.cache)(?:/|$)",
    ),
    (
        "embedded-repo-or-editor-state",
        r"(?:^|/)(?:\.worktrees|\.local|\.claude|\.idea|\.vscode)(?:/|$)",
    ),
    (
        "dev-home-or-secret-material",
        r"(?:^|/)(?:\.dev-home[^/]*|\.secrets\.env|\.local_secret|session_key|"
        r"sel_hmac\.key)(?:/|$)",
    ),
    ("dotenv-with-real-values", r"(?:^|/)\.env(?:\.local)?$"),
)

#: An absolute path into a home directory whose owner is not a recognised placeholder
#: publishes a real username and machine layout. This is an ALLOWLIST of placeholder names,
#: not a denylist of real ones, for two reasons: a denylist would have to write the
#: maintainer's username INTO the public repo to keep it out, and an allowlist reds an
#: unrecognised name the FIRST time it appears. The set below is the complete measured set
#: across the tracked tree — every entry a synthetic fixture. Adding a name here is a claim
#: that it is a placeholder, not a person.
_HOME_PATH = re.compile(r"/(?:Users|home)/([A-Za-z0-9._-]+)")
_PLACEHOLDER_HOMES = frozenset(
    {"alice", "alice.linux", "bob", "dev", "me", "runner", "someone", "u", "user", "you"}
)

#: A home-directory owner deliberately absent from the allowlist above, so the detector floor
#: proves the rule fires on a genuine-looking home path.
#:
#: ⚠️  ASSEMBLED AT RUNTIME, AND THAT IS THE WHOLE POINT. This script is itself a tracked file
#:     inside its own input set, so writing the name as a literal `/Users/<name>` would make
#:     this file a published real-home path — and the rule would be RIGHT to flag it. The
#:     first version of this rail carried that literal and bought itself a blanket
#:     `(SELF, rule)` exemption to stay green. That exemption is gone: six of its seven
#:     entries were dead weight (this path matches none of the six path rules), and the
#:     seventh only existed to hide this string. Interpolating instead means the bytes that
#:     reach the tree are `/Users/{` — and `{` is outside the owner class `[A-Za-z0-9._-]`,
#:     so the exemption is UNNECESSARY rather than merely unused.
#:
#:     THERE ARE NOW NO EXEMPTIONS AT ALL. A rail that exempts its own file is exactly the
#:     shape that lets a real defect hide later, because the exempted file is the one nobody
#:     re-reads. `_detector_floor` re-checks this script's own source on every run.
_UNLISTED_OWNER = "zz" + "notaplaceholder" + "zz"


def _tracked_files() -> list[str]:
    """Every tracked path as a repo-relative POSIX string. ``-z`` because a path may
    legitimately contain a space and line-splitting would silently drop its tail."""
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [
        name.decode("utf-8", errors="surrogateescape")
        for name in result.stdout.split(b"\0")
        if name
    ]


def _is_binary(blob: bytes) -> bool:
    """A NUL byte in the first 8 KB — the heuristic ``git diff`` uses to decide a file has
    no textual diff, and the right definition for the size rule's text exemption."""
    return b"\0" in blob[:8192]


# ── office documents ────────────────────────────────────────────────────────────────────
#
# An office document carries more than its text: the software that saved it records who wrote
# it, who last changed it, their company and manager, the name, initials and account id of each
# revision's and comment's author, and whatever properties a document-management tool stamps on
# it, such as a sensitivity label. It all sits in compressed XML parts, so this rule opens every
# tracked office document (recognised by its CONTENT, so a renamed one is read too) and refuses
# one that carries custom properties, a sensitivity label, or a person or organisation in an
# identity field. A format the rule cannot read is refused rather than trusted.

#: Extensions of the office formats. A tracked file carrying one must be readable as an office
#: document or it is refused; extensions other files also use (``.dot``, ``.pot``, ``.key``) are
#: left to the content check.
_OFFICE_EXTENSIONS = frozenset(
    # Office Open XML documents, workbooks, presentations and drawings.
    ".docx .docm .dotx .dotm .xlsx .xlsm .xlsb .xltx .xltm .xlam".split()
    + ".pptx .pptm .potx .potm .ppsx .ppsm .ppam .sldx .sldm .thmx".split()
    + ".vsdx .vsdm .vssx .vssm .vstx .vstm".split()
    # OpenDocument, packaged and flat.
    + ".odt .ott .ods .ots .odp .otp .odg .otg .odf .odc .odb .fodt .fods .fodp .fodg".split()
    # The binary formats before those, rich text, and other word processors' packages.
    + ".doc .xls .xlt .ppt .pps .vsd .rtf .pages .numbers".split()
)
#: The values an identity field may hold, because each names no one. An ALLOWLIST for the reason
#: ``_PLACEHOLDER_HOMES`` is one: a denylist would publish the names it keeps out. The empty
#: string; ``Author``, which Word's remove-personal-information option writes in place of every
#: revision author; ``Unknown``, the first entry of every RTF revision table; and the creator
#: python-docx and openpyxl sign a new file with. python-pptx is deliberately absent: its
#: template records a real person as the last modifier.
_PLACEHOLDER_IDENTITY = frozenset({"", "Author", "Unknown", "openpyxl", "python-docx"})
_ZIP_MAGIC = b"PK\x03\x04"
#: The compound-document container of the binary office formats, of mail items and of embedded
#: objects, whose property sets this rule does not parse.
_COMPOUND_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")
_RTF_MAGIC = b"{\\rtf"
_ODF_MIMETYPE = b"application/vnd.oasis.opendocument."
_ODF_OFFICE_NS = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
_ODF_META_NS = "urn:oasis:names:tc:opendocument:xmlns:meta:1.0"
_OFFICE_XML_SUFFIXES = (".xml", ".rels", ".vml", ".rdf")
#: Caps on what one document can make the rule read: bytes per part, parts per package, and
#: packages nested inside packages. Each one fails CLOSED.
_OFFICE_PART_CAP = 8_000_000
_OFFICE_MAX_PARTS = 2_000
_OFFICE_MAX_DEPTH = 3
_ZIP_ERRORS = (
    zipfile.BadZipFile,
    zipfile.LargeZipFile,
    zlib.error,
    OSError,
    EOFError,
    RuntimeError,
    NotImplementedError,
    ValueError,
)
#: Elements whose TEXT names a person or an organisation (a package's creator, last-modified-by,
#: company and manager, a spreadsheet comment's author, an OpenDocument's creators and
#: printed-by); attributes that name a person wherever they sit (a revision's or comment's author
#: and initials, a signed-in author's account id, a shared workbook's user); and elements that
#: describe a person, whose ``name`` or ``displayName`` is that person's name.
_IDENTITY_ELEMENTS = frozenset(
    {
        "creator",
        "lastModifiedBy",
        "Company",
        "Manager",
        "author",
        "initial-creator",
        "creator-initials",
        "sender-initials",
        "printed-by",
    }
)
_IDENTITY_ATTRIBUTES = frozenset({"author", "initials", "userId", "userName"})
_PERSON_ELEMENTS = frozenset({"cmAuthor", "author", "person", "userInfo"})
_PERSON_NAME_ATTRIBUTES = frozenset({"name", "displayName"})
#: Root namespaces of a part holding CUSTOM properties, and the relationship types that attach
#: custom properties or a sensitivity label to a package.
_CUSTOM_PROPERTY_NAMESPACES = frozenset(
    {
        "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties",
        "http://purl.oclc.org/ooxml/officeDocument/customProperties",
        "http://schemas.microsoft.com/office/2006/metadata/properties",
    }
)
_CUSTOM_PROPERTY_RELATIONSHIPS = ("/custom-properties", "/customProperties")
_LABEL_RELATIONSHIPS = ("/classificationlabels",)
#: A sensitivity label is written as custom properties whose names start with this marker, or
#: into a label part of its own.
_LABEL_MARKER = "msip_label"
_LABEL_PART = "docmetadata/labelinfo.xml"
_RTF_IDENTITY = re.compile(r"\{\\(?:\*\\)?(author|operator|company|manager)(?![a-z])\s?([^{}]*)\}")
_RTF_CUSTOM = re.compile(r"\{\\\*\\userprops(?![a-z])")
_RTF_REVISION_TABLE = re.compile(r"\{\\\*\\revtbl(?![a-z])((?:\s*\{[^{}]*\})*)")
_RTF_REVISION_ENTRY = re.compile(r"\{([^{}]*)\}")
_OFFICE_HOW_TO_FIX = (
    "An office-document finding is fixed in the document: clear the named field (Word's "
    "remove-personal-information option or its document inspector does it on save), delete a "
    "custom-properties or label part together with its content-type override and its package "
    "relationship, or convert a legacy binary file to its XML format. Do not add a value to "
    "_PLACEHOLDER_IDENTITY unless it names no person and no organisation."
)


def _local(name: str) -> str:
    return name.rsplit("}", 1)[-1]


def _namespace(name: str) -> str:
    return name[1:].split("}", 1)[0] if name.startswith("{") else ""


def _office_kind(blob: bytes) -> str | None:
    """``package``, ``flat``, ``rtf`` or ``compound``, judged by CONTENT; ``None`` for anything
    else, a zip that is not an office package included."""
    if blob.startswith(_COMPOUND_MAGIC):
        return "compound"
    if blob.startswith(_RTF_MAGIC):
        return "rtf"
    if blob.startswith(_ZIP_MAGIC):
        try:
            with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                names = set(archive.namelist())
                if "[Content_Types].xml" in names:
                    return "package"
                if "mimetype" in names:
                    with archive.open("mimetype") as fh:
                        if fh.read(len(_ODF_MIMETYPE)) == _ODF_MIMETYPE:
                            return "package"
        except _ZIP_ERRORS:
            return None
        return None
    head = blob[:4096]
    if head.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<") and _ODF_OFFICE_NS.encode() in head:
        try:
            for _, element in ElementTree.iterparse(io.BytesIO(blob), events=("start",)):
                return "flat" if _namespace(element.tag) == _ODF_OFFICE_NS else None
        except ElementTree.ParseError:
            return None
    return None


def _read_part(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes | None:
    """A part's bytes, or ``None`` when it cannot be read within the cap. The size is what the
    read yields, never what the member's header claims."""
    try:
        with archive.open(info) as fh:
            data = fh.read(_OFFICE_PART_CAP + 1)
    except _ZIP_ERRORS:
        return None
    return data if len(data) <= _OFFICE_PART_CAP else None


def _xml_problems(part: str, data: bytes) -> list[str]:
    """One XML part's problems. Fields are judged by NAME, never by searching for a person."""
    where = f" ({part})" if part else ""
    found = []
    if _LABEL_MARKER in data.decode("utf-8", "replace").lower():
        found.append(f"carries a sensitivity label{where}")
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        return [*found, f"has a part that is not well-formed XML{where}"]
    if _namespace(root.tag) in _CUSTOM_PROPERTY_NAMESPACES:
        found.append(f"carries custom properties{where}")
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        tag = _local(element.tag)
        if tag == "user-defined" and _namespace(element.tag) == _ODF_META_NS:
            found.append(f"carries custom properties{where}")
        if tag == "Relationship":
            relationship = element.get("Type", "")
            if relationship.endswith(_CUSTOM_PROPERTY_RELATIONSHIPS):
                found.append(f"attaches custom properties{where}")
            if relationship.endswith(_LABEL_RELATIONSHIPS):
                found.append(f"attaches a sensitivity label{where}")
        text = "".join(element.itertext()).strip()
        if tag in _IDENTITY_ELEMENTS and text not in _PLACEHOLDER_IDENTITY:
            found.append(f"names someone in <{tag}>{where}")
        for key, value in element.attrib.items():
            attribute = _local(key)
            named = attribute in _IDENTITY_ATTRIBUTES or (
                tag in _PERSON_ELEMENTS and attribute in _PERSON_NAME_ATTRIBUTES
            )
            if named and value.strip() not in _PLACEHOLDER_IDENTITY:
                found.append(f"names someone in <{tag} {attribute}>{where}")
    return found


def _package_problems(blob: bytes, depth: int) -> list[str]:
    """The problems in every part of one package, and of every package embedded in it."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
        infos = [info for info in archive.infolist() if not info.is_dir()]
    except _ZIP_ERRORS:
        return ["is not a readable package"]
    if len(infos) > _OFFICE_MAX_PARTS:
        return [f"has more parts than this rule reads ({len(infos)} > {_OFFICE_MAX_PARTS})"]
    found = []
    for info in infos:
        part = info.filename
        data = _read_part(archive, info)
        if data is None:
            found.append(f"has a part this rule cannot read ({part})")
            continue
        if part.lower() == _LABEL_PART:
            found.append(f"carries a sensitivity label ({part})")
        if part.lower().endswith(_OFFICE_XML_SUFFIXES):
            found += _xml_problems(part, data)
        elif data.startswith(_COMPOUND_MAGIC):
            found.append(f"embeds a compound document this rule cannot read ({part})")
        elif data.startswith(_ZIP_MAGIC) and _office_kind(data) == "package":
            if depth >= _OFFICE_MAX_DEPTH:
                found.append(f"nests documents deeper than this rule reads ({part})")
            else:
                found += [f"{problem} inside {part}" for problem in _package_problems(data, depth + 1)]
    return found


def _rtf_problems(text: str) -> list[str]:
    """An RTF document's information group, custom properties and revision table."""
    found = [
        f"names someone in \\{field}"
        for field, value in _RTF_IDENTITY.findall(text)
        if value.strip() not in _PLACEHOLDER_IDENTITY
    ]
    if _RTF_CUSTOM.search(text):
        found.append("carries custom properties (\\userprops)")
    for table in _RTF_REVISION_TABLE.findall(text):
        entries = (entry.strip().rstrip(";").strip() for entry in _RTF_REVISION_ENTRY.findall(table))
        if any(entry not in _PLACEHOLDER_IDENTITY for entry in entries):
            found.append("names someone in its revision table")
    if _LABEL_MARKER in text.lower():
        found.append("carries a sensitivity label")
    return found


def _office_problems(blob: bytes, *, claimed: bool = False) -> list[str]:
    """Why *blob* is unfit to publish as an office document, one phrase each, sorted; empty when
    it is fit, and when it is no office document at all unless *claimed* (its extension names an
    office format). A phrase names the field or part, never its value: the report is published."""
    kind = _office_kind(blob)
    if kind is None:
        found = ["is not readable as the office document its extension names"] if claimed else []
    elif kind == "compound":
        found = ["is a compound document, whose author and custom properties this rule cannot read"]
    elif kind == "rtf":
        found = _rtf_problems(blob.decode("latin-1"))
    elif kind == "flat":
        found = _xml_problems("", blob)
    else:
        found = _package_problems(blob, depth=0)
    return sorted(set(found))


def _office_part_texts(blob: bytes, depth: int = 0) -> list[tuple[str, str]]:
    """``(part, text)`` for every XML part of an office package and of the packages embedded in
    it (as ``<part>!<inner part>``); empty for any other blob."""
    if _office_kind(blob) != "package":
        return []
    archive = zipfile.ZipFile(io.BytesIO(blob))
    texts = []
    for info in [info for info in archive.infolist() if not info.is_dir()][:_OFFICE_MAX_PARTS]:
        data = _read_part(archive, info)
        if data is None:
            continue
        if info.filename.lower().endswith(_OFFICE_XML_SUFFIXES):
            texts.append((info.filename, data.decode("utf-8", "replace")))
        elif depth < _OFFICE_MAX_DEPTH and data.startswith(_ZIP_MAGIC):
            texts += [(f"{info.filename}!{inner}", t) for inner, t in _office_part_texts(data, depth + 1)]
    return texts


def _may_be_office(head: bytes) -> bool:
    if head.startswith((_ZIP_MAGIC, _COMPOUND_MAGIC, _RTF_MAGIC)):
        return True
    stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    return stripped.startswith(b"<") and _ODF_OFFICE_NS.encode() in head


def _office_sample(parts: dict[str, str | bytes]) -> bytes:
    """A small package built at run time for the detector floor: nothing binary is committed."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        for name, data in parts.items():
            archive.writestr(name, data)
    return out.getvalue()


def _detector_floor() -> list[str]:
    """Prove every pattern catches what it exists for, and spares what it must, BEFORE a
    clean scan is believed. A rail that matches nothing prints OK for the rest of its life."""
    must_match = {
        "residue-name": ("temp-screenshots/before.png", "scratch/notes.md", "docs/old.md"),
        "residue-suffix": ("provider.py.orig", "app.json.rej", "notes.md~"),
        "build-output-or-cache": ("slack-channel/ui/dist/index.js", "node_modules/x/i.js"),
        "embedded-repo-or-editor-state": (".worktrees/lane-x/README.md", ".local/state/gh/id"),
        "dev-home-or-secret-material": (".dev-home/.local_secret", ".secrets.env"),
        "dotenv-with-real-values": (".env", "slack-channel/.env.local"),
    }
    must_not_match = (
        "openrouter-models/template.py",
        "growth/bakeoff.py",
        ".env.example",
        "docs/oldest-first.md",
        "ops/tempo.py",
    )
    failures = []
    rules = dict(_PATH_RULES)
    for rule, samples in must_match.items():
        for sample in samples:
            if not re.search(rules[rule], sample):
                failures.append(f"detector floor: {rule} no longer catches {sample!r}")
    for sample in must_not_match:
        for rule, pattern in _PATH_RULES:
            if re.search(pattern, sample):
                failures.append(f"detector floor: {rule} falsely catches {sample!r}")
    if _HOME_PATH.findall(f"HOME = '/Users/{_UNLISTED_OWNER}'") != [_UNLISTED_OWNER]:
        failures.append("detector floor: the home-path pattern no longer extracts the owner")
    if _UNLISTED_OWNER in _PLACEHOLDER_HOMES:
        failures.append("detector floor: the control owner was added to the allowlist")
    if _HOME_PATH.findall("HOME = '/Users/me'") != ["me"] or "me" not in _PLACEHOLDER_HOMES:
        failures.append("detector floor: '/Users/me' is not treated as a placeholder")
    dc = 'xmlns:dc="http://purl.org/dc/elements/1.1/"'
    signed = _office_sample({"docProps/core.xml": f"<p {dc}><dc:creator>python-docx</dc:creator></p>"})
    if _office_problems(signed):
        failures.append("detector floor: office-document falsely catches a package naming no one")
    planted = "zz-planted-person"
    office_samples = {
        "a creator": _office_sample(
            {"docProps/core.xml": f"<p {dc}><dc:creator>{planted}</dc:creator></p>"}
        ),
        "a revision author": _office_sample(
            {"word/document.xml": f'<d xmlns:w="urn:w"><w:ins w:author="{planted}"/></d>'}
        ),
        "custom properties": _office_sample(
            {
                "docProps/custom.xml": '<Properties xmlns="http://schemas.openxmlformats.org/'
                'officeDocument/2006/custom-properties"/>'
            }
        ),
        "a sensitivity label": _office_sample({"docProps/custom.xml": "<p name='MSIP_Label_x'/>"}),
        "a compound document": _COMPOUND_MAGIC + b"\0" * 504,
        "an unreadable claim": b"not a document",
    }
    for label, sample in office_samples.items():
        if not _office_problems(sample, claimed=label == "an unreadable claim"):
            failures.append(f"detector floor: office-document no longer catches {label}")
    if planted in _PLACEHOLDER_IDENTITY:
        failures.append("detector floor: the planted identity was added to the allowlist")

    # This script polices itself — there are no exemptions. A literal `/Users/<name>` here
    # would make this file a published real-home path, so the guard is on the MECHANISM (an
    # inlined literal) rather than on waiting for the scan below to notice.
    own = Path(__file__).read_text(encoding="utf-8")
    inlined = sorted(o for o in set(_HOME_PATH.findall(own)) if o not in _PLACEHOLDER_HOMES)
    if inlined:
        failures.append(
            f"detector floor: {SELF} inlines {inlined} as a matchable home path — "
            f"interpolate _UNLISTED_OWNER instead of writing the name as a literal"
        )
    return failures


def main() -> int:
    """Print the report; exit ``0`` iff every tracked path is fit to publish."""
    violations = _detector_floor()
    if violations:
        print("publication-hygiene: FAIL (detector floor)")
        for line in violations:
            print(f"  {line}")
        return 1

    tracked = _tracked_files()
    for rule, pattern in _PATH_RULES:
        compiled = re.compile(pattern)
        for path in tracked:
            if compiled.search(path):
                violations.append(f"{rule}: {path}")

    for path in tracked:
        full = ROOT / path
        if not full.is_file():
            continue
        blob = full.read_bytes()[:_CONTENT_READ_CAP]
        claimed = PurePosixPath(path).suffix.lower() in _OFFICE_EXTENSIONS
        if claimed or _may_be_office(blob[:4096]):
            for problem in _office_problems(full.read_bytes(), claimed=claimed):
                violations.append(f"office-document: {path} {problem}")
        if _is_binary(blob):
            size = full.stat().st_size
            if size > _MAX_BINARY_BYTES:
                violations.append(
                    f"oversized-binary: {path} is {size} bytes (ceiling {_MAX_BINARY_BYTES})"
                )
            # An office document's XML parts are text every reader of it gets: hold them to
            # the home-path rule below, located as `<path>!<part>`.
            texts = [(f"{path}!{part}", text) for part, text in _office_part_texts(full.read_bytes())]
        else:
            texts = [(path, blob.decode("utf-8", "replace"))]
        for shown, text in texts:
            for name in set(_HOME_PATH.findall(text)):
                if name not in _PLACEHOLDER_HOMES:
                    violations.append(f"real-home-path: {shown} names home directory {name!r}")

    if violations:
        print("publication-hygiene: FAIL")
        for line in sorted(violations):
            print(f"  {line}")
        if any(line.startswith("office-document:") for line in violations):
            print("\n" + _OFFICE_HOW_TO_FIX)
        if any(not line.startswith("office-document:") for line in violations):
            print(
                "\nDelete the path, or move it somewhere this repo publishes deliberately. Do not "
                "widen the allowlist to make CI green — every entry is reviewed as policy."
            )
        return 1

    print(f"OK: {len(tracked)} tracked paths, none unfit to publish")
    return 0


if __name__ == "__main__":
    sys.exit(main())
