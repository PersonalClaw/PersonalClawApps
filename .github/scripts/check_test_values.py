#!/usr/bin/env python3
"""Repo rail: no value in the apps' test code has a real token's format, a provider's key prefix, or
a real workspace's or account's id.

This repository is public, and secret scanners read public code: a placeholder written in a
provider's token format is reported as a leaked credential, and a real-looking one teaches the
next contributor that pasting a key into a test is normal. An app's test hands its provider an
API key, a bot token or an access key id that nothing on the path reads by shape, so a neutral
fake (``fake-anthropic-test``, ``fake-bot-token-saved``) is what the test needs.

Three rules over every bundle's test code (``test_*.py``, ``tests/``, ``fixtures/``,
``conftest.py``, UI ``*.test.*``) and ``apps_testkit/``:

1. No value matches a published token format (an AWS key id in that alphabet, a GitHub token of
   its real length, a fine-grained PAT, a Slack token with its number groups, a Stripe key, a
   signed JWT, a whole private-key block).
2. No value carries a provider's key prefix (``sk-``, ``sk-ant-``, ``ghp_``, ``xoxb-``, ``xapp-``,
   an ``AKIA`` id, ``hf_``) — the core redactor an app's masking test relies on sees the AWS
   documentation's example key id, so that is the shape such a test uses.
3. A workspace or account id reads as made up. A Slack id (a type letter, then eight to ten
   capitals and digits holding at least two of each) carries ``EXAMPLE``, ``ABC``, ``AB12``,
   ``0123``, ``OWNER`` or ``BAD``, or one character four times running; an AWS account in an ARN
   or a registry host is one the AWS documentation uses in its examples. A real one says which
   workspace or account the test was copied from, so a finding prints only its first characters.

The AWS documentation's example values (ending ``EXAMPLE``) are exempt from both: public, fake by
construction, and known to every scanner. The patterns below are written with character classes
so this file matches none of them.

**Vacuity floor.** The walk must reach at least as many test files as the bundles hold today, and
the detectors are checked against samples assembled at run time before anything is read.

Run locally exactly as CI does:

    python .github/scripts/check_test_values.py
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]

REAL_FORMATS = (
    re.compile(r"\b(?:A3T[A-Z0-9]|A[K]IA|A[S]IA|A[B]IA|A[C]CA)[A-Z2-7]{16}\b"),
    re.compile(r"\bg[h][pousr]_[0-9a-zA-Z]{36}"),
    re.compile(r"g[i]thub_pat_\w{82}"),
    re.compile(r"x[o]xb-[0-9]{10,13}-[0-9]{10,13}[a-zA-Z0-9-]*"),
    re.compile(r"x[o]x[pe](?:-[0-9]{10,13}){3}-[a-zA-Z0-9-]{28,34}"),
    re.compile(r"(?i)x[a]pp-\d-[A-Z0-9]+-\d+-[a-z0-9]+"),
    re.compile(r"s[k]-ant-(?:api03|admin01)-[a-zA-Z0-9_\-]{93}AA"),
    re.compile(r"s[k]-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{58,74}T3BlbkFJ"),
    re.compile(r"s[k]-[a-zA-Z0-9]{20}T3BlbkFJ[a-zA-Z0-9]{20}"),
    re.compile(r"h[f]_[a-zA-Z]{34}"),
    re.compile(r"(?:s[k]|r[k])_(?:test|live|prod)_[a-zA-Z0-9]{10,99}"),
    re.compile(r"A[I]za[0-9A-Za-z\-_]{35}"),
    re.compile(r"g[s]k_[a-zA-Z0-9]{52}"),
    re.compile(r"-{5}B[E]GIN[ A-Z0-9_-]{0,100}PRIVATE KEY( BLOCK)?-{5}[\s\S-]{64,}?KEY( BLOCK)?-{5}"),
    re.compile(r"\be[y][a-zA-Z0-9]{17,}\.e[y][a-zA-Z0-9/\\_-]{17,}\.(?:[a-zA-Z0-9/\\_-]{10,}={0,2})?"),
)

PREFIXED = re.compile(
    r"s[k]-ant-|(?<![A-Za-z0-9_])s[k]-[A-Za-z0-9_\-]{2,}"
    r"|(?<![A-Za-z0-9])(?:A[K]IA|A[S]IA)[0-9A-Z]{4,}"
    r"|(?<![A-Za-z0-9])g[h][pousr]_[A-Za-z0-9]{2,}|g[i]thub_pat_"
    r"|(?<![A-Za-z0-9])x[o]x[abeoprs]-[A-Za-z0-9\-]{2,}|(?<![A-Za-z0-9])x[a]pp-[0-9A-Za-z\-]{2,}"
    r"|(?<![A-Za-z0-9_])h[f]_[A-Za-z0-9]{6,}"
)

DOCUMENTED_EXAMPLE = re.compile(r"EXAMPLE")

#: Rule 3. Not after a backslash, so a ``\U0001F…`` escape is not read as a user id.
SLACK_ID = re.compile(
    r"(?<![A-Za-z0-9_\\])[BCDEGTUW](?=(?:[A-Z]*[0-9]){2})(?=(?:[0-9]*[A-Z]){2})[A-Z0-9]{8,10}"
    r"(?![A-Za-z0-9_])"
)
PLACEHOLDER_SLACK_ID = re.compile(r"EXAMPLE|ABC|AB12|0123|OWNER|BAD|([A-Z0-9])\1{3}")
AWS_ACCOUNT = re.compile(
    r"\barn:aws[a-z-]*:[a-z0-9-]*:[a-z0-9-]*:(\d{12}):|\b(\d{12})\.dkr\.ecr\."
)
#: The accounts the AWS documentation uses in its examples, and the all-zero one.
PLACEHOLDER_ACCOUNTS = frozenset({"000000000000", "111122223333", "123456789012", "444455556666"})

TEXT_SUFFIXES = {".py", ".json", ".ts", ".tsx", ".js", ".md", ".txt", ".yaml", ".yml", ".toml", ".sh"}

#: Test files the walk reaches today; fewer means a glob stopped matching.
MIN_TEST_FILES = 150


def is_test_code(path: pathlib.Path, root: pathlib.Path) -> bool:
    rel = path.relative_to(root)
    parts = set(rel.parts)
    if parts & {"node_modules", ".venv", "__pycache__", "dist", ".git"}:
        return False
    name = path.name
    return (
        name.startswith("test_")
        or name == "conftest.py"
        or ".test." in name
        or ".spec." in name
        or "tests" in parts
        or "fixtures" in parts
        or rel.parts[0] == "apps_testkit"
    )


def test_files(root: pathlib.Path = ROOT) -> list[pathlib.Path]:
    bundles = {manifest.parent for manifest in root.glob("*/app.json")}
    roots = sorted(bundles) + [root / "apps_testkit"]
    out: list[pathlib.Path] = []
    for base in roots:
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and path.suffix in TEXT_SUFFIXES and is_test_code(path, root):
                out.append(path)
    return out


def real_formats(text: str) -> list[str]:
    return [
        m.group(0)[:24]
        for rx in REAL_FORMATS
        for m in rx.finditer(text)
        if not DOCUMENTED_EXAMPLE.search(m.group(0))
    ]


def prefixed(text: str) -> list[str]:
    return [
        m.group(0)[:24]
        for m in PREFIXED.finditer(text)
        if not DOCUMENTED_EXAMPLE.search(text[m.start() : m.end() + 16])
    ]


def real_ids(text: str) -> list[str]:
    """Rule 3's findings, each cut to its first three characters."""
    found = [
        m.group(0)
        for m in SLACK_ID.finditer(text)
        if not PLACEHOLDER_SLACK_ID.search(m.group(0)[1:])
    ]
    found += [
        account
        for m in AWS_ACCOUNT.finditer(text)
        for account in m.groups()
        if account and account not in PLACEHOLDER_ACCOUNTS
    ]
    return [f"{value[:3]}…" for value in found]


def _detector_problems() -> list[str]:
    """The positive control, on samples assembled here so this file holds none of them."""
    found = []
    for sample in (
        "A" + "KIA" + "WWWWWWWWWWWWWWWW",
        "gh" + "p_" + "a1" * 18,
        "sk" + "_live_" + "9f8e7d6c5b4a39",
        "-----" + "BEGIN PRIVATE KEY-----\n" + "M" * 70 + "\n-----" + "END PRIVATE KEY-----",
    ):
        if not real_formats(sample):
            found.append(f"the format detector missed {sample[:12]}…")
    for sample in ("s" + "k-ant-test", "g" + "hp_fixture", "x" + "oxb-saved", "x" + "app-1-saved"):
        if not prefixed(f'"{sample}"'):
            found.append(f"the prefix detector missed {sample[:8]}…")
    if real_formats("A" + "KIA" + "IOSFODNN7EXAMPLE") or prefixed('"fake-bot-token-saved"'):
        found.append("a detector flags an exempt or neutral value")
    account = "2109" + "87654321"
    for sample, masked in (
        ("C" + "0Q7W2R9Z5K", "C0Q…"),
        ("U" + "07HX4LM2QR", "U07…"),
        (f"arn:aws:sts::{account}:assumed-role/Dev/x", "210…"),
        (f"{account}.dkr.ecr.us-east-1.amazonaws.com/app", "210…"),
    ):
        if real_ids(f'"{sample}"') != [masked]:
            found.append(f"the id detector no longer reports {masked}")
    for sample in (
        "U0EXAMPLE01",
        "C0123ABC456",
        "C07AB12CD",
        "U0OWNER01",
        "CBADCHAN01",
        "C0AAAA1111",
        "C0123456789",
        "SECP256R1",
        "\\U0001FAFF",
        "arn:aws:iam::111122223333:role/x",
    ):
        if real_ids(f'"{sample}"'):
            found.append(f"the id detector flags the placeholder {sample}")
    # The scan applies all three rules: one planted value per rule must come back from it.
    planted = f'"{"A" + "KIA" + "W" * 16}" "{"s" + "k-ant-test"}" "{"C" + "0Q7W2R9Z5K"}"'
    reported = _file_problems("planted", planted)
    for rule in ("a real token's format", "a provider's key prefix", "a real-looking"):
        if not any(rule in line for line in reported):
            found.append(f"the scan no longer applies the rule for {rule}")
    return found


def _file_problems(rel: str, text: str) -> list[str]:
    """Every rule's findings in one test file's text."""
    found = [f"{rel}: {hit}… has a real token's format" for hit in real_formats(text)]
    found += [
        f"{rel}: {hit}… carries a provider's key prefix; use a neutral fake"
        for hit in prefixed(text)
    ]
    found += [
        f"{rel}: {hit} is a real-looking workspace or account id; use a made-up one "
        "(U0EXAMPLE01, 111122223333)"
        for hit in real_ids(text)
    ]
    return found


def problems(root: pathlib.Path = ROOT) -> list[str]:
    found = _detector_problems()
    files = test_files(root)
    if len(files) < MIN_TEST_FILES:
        found.append(f"only {len(files)} test files found (expected at least {MIN_TEST_FILES})")
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        found += _file_problems(path.relative_to(root).as_posix(), text)
    return found


def main() -> int:
    found = problems()
    if found:
        print(
            "Test values must not look like keys or real ids (use neutral fakes such as "
            "fake-anthropic-test and U0EXAMPLE01):"
        )
        for line in found:
            print(f"  {line}")
        return 1
    print(f"OK: {len(test_files())} test file(s), none with a key-shaped value or a real-looking id")
    return 0


if __name__ == "__main__":
    sys.exit(main())
