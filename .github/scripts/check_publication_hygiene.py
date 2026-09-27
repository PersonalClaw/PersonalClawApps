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
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

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


# ── internal references ─────────────────────────────────────────────────────────────────
#
# A public repository must not name a system that is not public: the name, host, identifier
# or document of an internal tool. Measured when this rule was added: 43 references on 28
# lines in 17 files, an internal guidance system cited by name, document id and numbered
# control id, and one bundle describing its CLI with an internal characterisation, an
# internal install path and an internal sign-in tool. Each was rewritten to the principle
# it stood for.
#
# ⚠️  THE DENYLIST IS SALTED SHA-256 DIGESTS, NEVER PLAINTEXT. A plaintext list would publish
#     the very names it keeps out, the self-defeat the home-path rule avoids by being an
#     allowlist. The text is folded into candidates (words, two-word phrases, host suffixes,
#     digit-shaped codes, id shapes), each distinct candidate is digested once, and digests are
#     compared. A compound kind is only checked in a file that carries one of its HEAD words.
#     These are the same salt and entries as core's ``publication-hygiene-baseline.json``
#     (``internal_reference_rule``): add a name there with
#     ``python3 scripts/check_publication_hygiene.py --digest KIND TEXT`` and copy the printed
#     lines here. Never write the plaintext into this repo, a commit message, a PR or an issue.

_INTERNAL_SALT = "personalclaw/publication-hygiene/internal-reference/v1"
_INTERNAL_DENIED: dict[str, frozenset[str]] = {
    "word": frozenset(
        {
            "0444b2822dea634cb80766def3d34e6b353782477fbc13a89f6a021e35de690d",
            "07033a04b483cd43007e384d9b62f520abbe454e65a7fac021e337cd71d1b0dc",
            "4426b9576e46a9c42fa8db9a925f4566dddc639bd15100eb4cf952226c975cde",
            "71815162309381b90fb9ea36aa48238569ddc77dfae630605683f6660e570900",
            "895f53ee4f0159610882ef6deb7286b19266eca78fc27675106e8281006c816b",
            "8e521af0973c943353c34d9d4a79b6e8422f969c329e75dfb8bf6245dab5cadb",
            "9106e0ca58e299c880b5f86c8adfce00b865ffdeb482393d28ee451dc012b491",
            "95840cc5d1d97158e6c8c88aa1d7150838288426588ef52bdaa069ee39518e6c",
            "992fb7d5b2cc000e8b07290e7a69c2c579f21a263abffa827502e4534e70639f",
            "aace0b9457a44b6a6abd968a2bd24a5fd450f45ed5024c08284971dac3f0dff7",
            "addee44a6d5105b29aa03ee3254b47a8b8f8804f34fa262b95fa642c72909737",
            "b0c58bbd65e32c250c0a7f135876e1b98ee010e70fcb108ae27c42daaa9ea1ee",
            "b3c56fa98f47e08d5504caeab42c0d579018a4cc8de52c76c43951bbf170e2a0",
            "b4458220b493e7372186d12d41ec3a11b3b51cba216ec8b8e06ce50e9e5da9df",
            "c260e7233828ae659c1aa62f1c4f66f5a7e60aeba64e044fbd243f69ab637af3",
            "ce5ad0e99ecec5a4ce3b99b6c855f7d5eb15527288f674cae5ab0a5b51ef87d5",
            "d5bc6b861b3dbf69a2b05eae2e9a8b7576f693bfbe553ad804ca1b3f9c21090b",
            "d8dbeac984c0e85b897cb0ea1f6069d99788bf053d742793245105dfd8d3ca12",
            "dae1a55b4a9ad96fe148dba49114ccd1b31a2b4fa6c92d914e1e6dca4d3007a4",
            "e8ba88389cc86a066718b1cddc0526b68bb91ae2400257c9d62a71ae60621593",
            "eec31b3081f2eb75fb4db885c0c87e6bb23ea749daa24acd11402d7a9b01da7f",
        }
    ),
    "phrase-head": frozenset(
        {
            "23a1c488492c4df41202702dbb3c3926fc3a671befe18f7d7419f3b16d96faee",
            "37a4a37276e02aeacf325734663c8af74aa627fba6e882f68a19b8ad4957c15a",
            "4493e86310a94aa9589f2e0506cb7941347ce8ec161942119693523eecacfb86",
            "59b4e2be39ac27d18e57662a3a9794dc95bfd6a25769124e25ec2ac4b589f686",
            "5d2b26e52c049ddcc0f3f6b4c96570b638ef5b00abbb1649b648106ed0ab1648",
            "6dc86d1639f3248cbeca0150b3df6f4173f18f59cd6141b0cd553150abe13774",
            "81a6fa897614fe1d035d766a142c578a5f4dd35b8cb512f536601c757100714a",
            "cf448ff05cb806014d7e1da6427dbf656bd49d8294aef07614876cfd78fcbe2f",
            "e30d31e5cb7660399dbcac990e1362292b62d1327c37469cd2a1326e6ea9c229",
        }
    ),
    "phrase": frozenset(
        {
            "05529191f6d271f5e05d7b7d56442115322060cef309e4ca90ce4f6e4824992e",
            "104858ded40458a8458a834710652114dfb0dd0da85e998d3f08aaff4a68c3bf",
            "1f36bad97bb313e6b22bed6acdf391cee42f58ef8f9ea02030010a3a11f0473f",
            "2508e0cf8bb001233e2e9fdd711c367e705946f7aa6e1f710f831157591771b8",
            "37fd4901ede43fe7e66a74da88dbc181953a3de9daf93812fea131d2e9c1071c",
            "4bbc426ec45aec2c3451b8b7dc5ce6c9cc8ece2178a8bac45060e7237f96b059",
            "674ad68379a44d97d9541ed098b42c1714e7b281aa208fa9aad1f452028c5091",
            "6a5d084c7dd5e06348edece5e08ddc6f1c01503966768073777b3061bf415818",
            "717d6e0c97a7eb4e4ae75b6a9cb7e6e393876bd3bf215f4844f58d971128220f",
            "79c1827c5256961beaccce097880aa4804e85f1e684fcde6b71a823862d60691",
            "7a6cd2179c6475241dc6ccef2ef062dd67f7fe825043be2f7b4510000d9a8b12",
            "87d0d4a8957b3035ca1e5d0478ce53bea037c8b91bc64b5672c9ce0149838581",
            "91b1e58c62e54699947a1a24c261e9a8045d7d19505e61c94cb21deffc219106",
            "925812fdfbce1f39d0f4b402449f40e9d84d8022a909fac81ee7c47a60be1450",
            "9d40d2d54925f39f1b12283103058c48eab8ebd1e0bd60ade132704a8ccfc6fa",
            "a8673ac7b4fa3f796171a7879e50cd8a8e167a4249e8e5d7c70c5aed47a32e5a",
            "b297f3c7ec4c8a44f512da468f3999cc2fd8a182788ea331749a2da9ef41083d",
            "c64245100313537f85380b0cd372225e0ec121351c8f656ce6e246a1661b2465",
            "d9cba52d45c7822e84f376a0b2633a58d5e105218a832ca2036e3531b285781a",
            "dbdba375511ea0a21e4806d1ea7de2894187ef68c758243866c0b94c916cbd47",
            "e194dffc8a4f1c2de6cea7c80913953629d6c73e2c176affc6a50678af2bb4ee",
        }
    ),
    "host-head": frozenset(
        {
            "1900d929711c4df704ac73d1f16f6526db2d0b3038b3ee76496b3c8e12a480bd",
            "58d96806ccb22280a7458a7e16d5dad51b6e4bf11f810df9dc3a37b50a962e54",
            "7df4f5ddf3ea57cd4d26b3567685dfdfc2c821adf508ef9e26930c9d2c6cf3ff",
            "817679047f83f26ef395ac4cac1d13bb8c9da6f6b487cd7a3a174816d8e78081",
            "8312255990c3ace840700c3792fcd0bc3a8b82b361a02256f65acfb162cf211f",
        }
    ),
    "host": frozenset(
        {
            "02b96c3fe24d18e4f84e359cb917b76b990d79c087a97185dac170580d3048c2",
            "18cd57f2de9a98b9e239094201cf5bf9d09f15105dca66ca69a1b39c42b769e8",
            "1902c5f5b5d5ed2d599ff8a0ec5e75a7fd6f552756135af41781e60132c272e9",
            "1c282ab8173d00a3097106a76d4d46f4e44953c6748c396f1681ac807469adb9",
            "2bdf29378f151caadb1299f3978c10d63a3307d00e401d8103f2a5cfcf74d991",
            "3ffb2600fd14d1e98259b02792c21abbe571437de65ea630d149432d20a83737",
            "41db1ab246b5d998e5ce76101f31e18f7e2da25c9746a01cd98d0ee7ba5fa419",
            "594ecf0f0ffef28a5d1d2917fec942677f54cd9558fd961cb0426a0462a80778",
            "718a7364d645903e0e8d1f869c1447d0ef9613d1ca4a0587ddfb949d321568d7",
            "771b778dd1d09c372cf6c1a6d9c241389c959704501a7b0073a737fa85fad412",
            "7775d7096b4109725238bd4038295ae25b03b227f6b7cb2960464c226eb53f16",
            "788b5ffbfea26f7d7a70b9bc9cd6fc92baf73ed54cda4ed3f3be2580208fcfde",
            "80f73cc0c34eab9567944b97b5512a5febf558023841ca43ad7abef75cacb817",
            "831bc8aeff783bdf941d23a9824d5214875a3336b99a53061da02768ecea96df",
            "8fa0ca7b7bb09779fff4fdfbfed2330c5ba80cf3ad0d1a00e4d43f55877d94ce",
            "9fff2c416039d28452c1340e017e0bbf4312ebfa7eb2b62da6e3354f811534dd",
            "d7f0adfe9fae54e1a6a72c56e125d08a45e9d94d2d39832879bbd95d2a1a9cad",
            "d9ff8292dcf5d83980a35868ece8722c1af06aa53f87cdc221ec68756ac91098",
            "dd1ec7cfbb21da615caf085aa27b6b8498f9d34bd4fd220dbefaa80ef852f2ff",
        }
    ),
    "code-head": frozenset(
        {
            "2f65f63950e1360dfbfa3cd15bb43e42dd7c40f9cd3a85dfb58e9cdc781b1085",
            "cf12c36f98d71b327eaaa962d63f379b7b7c9618cc69e4128ad1c41e4ddd7278",
            "d29297bae03a500bce6eb99de84ba6a4670f20f026a3c8eb706db7aef4dfe466",
        }
    ),
    "code": frozenset(
        {
            "09a3fb1084488a1833e0b9ff5b762218d1b36665c3e727849c33ce37c6938f7d",
            "935ce3bf4a63ad11fe7f631cd0016d9b2d0aedd2e8c7018a6de5c53ce4a612ed",
            "94d8fc651dacb5b4c5bdb5f653c70a27e1a6c353a6eb08cb4d12c040a2cc490c",
            "9cfecf91ee209ca5bc932d41ac9db5d988ebed84080e1c07d340037100e76ce2",
            "a010f93c06f0dfc276d2f120a262673ac7aa871f77729998719ba2f713ddb7d9",
            "ce61ecc721e399a724e57a760d518fe0355558564a4d0e781156dce5f79e0a7c",
            "d2b470e092869ba8a2e5ba3793147e70ab49423fc06115c178a7c918f480e746",
        }
    ),
    "id-head": frozenset(
        {
            "185f7b2a5bd93cf5e8a59663e29eede241eb1ce87b5d37604265bc6d1d4c704a",
            "4d8e9ce6289d2ce26a3a44ca9365e7ae2e3a3226b9bb72e017189cf026d28bdf",
            "e730ec88247bdd82aac3206b75d02d9c0e5db6a35eba83e9c383ac9489cfb328",
        }
    ),
    "id": frozenset(
        {
            "39e91b94c8c0e0ac11b13a71176040c34e034e5f8cf808d057e34168a2b085e7",
            "a78adae15e911f0bb8338be3097422012f19dfe5988b08cab013e441399ed5b5",
            "fd95149a539d282bbfab3a17159d761fd6ce21d53ce19f0022a7badb081fb4a0",
        }
    ),
}
_DIGITS = "0123456789"
#: A word: a maximal run of letters and digits, case-folded, plus the CamelCase parts of a
#: mixed-case run — so ``snake_case``, ``kebab-case``, ``dotted.names`` and ``FooService``
#: all fold into the words a reader sees.
_RUN = re.compile(r"[A-Za-z0-9]+")
_CAMEL_PART = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")
#: A dotted, host-like run, anchored at a label start. Every suffix of two or more labels is a
#: candidate, so a denied host also denies each of its subdomains.
_HOST_RUN = re.compile(r"(?<![a-z0-9-])[a-z0-9-]+(?:\.[a-z0-9-]+)+")
#: A catalogue code (letters, optional hyphen, 1-3 digits; not a ``pkg-1.6.0`` version),
#: digits folded to 9s so one entry covers a numbered family.
_CODE = re.compile(r"(?<![a-z0-9])([a-z]{2,6})(-?)([0-9]{1,3})(?![a-z0-9]|\.[0-9])")
#: A document-store id: a three-letter prefix, ``_``, then 14 mixed-case base62 characters or
#: 8 lowercase-and-digit ones. Folded to prefix and shape. Matched case-sensitively.
_ID = re.compile(
    r"(?<![A-Za-z0-9])(?P<prefix>[a-z]{3})_(?:"
    r"(?P<m14>(?=[a-z0-9]*[A-Z])(?=[A-Za-z]*[0-9])(?=[A-Z0-9]*[a-z])[A-Za-z0-9]{14})"
    r"|(?P<l8>(?=[a-z]*[0-9])(?=[0-9]*[a-z])[a-z0-9]{8})"
    r")(?![A-Za-z0-9])"
)
#: How far apart two words may sit and still read as one phrase (a wrapped comment included).
_PHRASE_GAP = 12

#: Each kind's CONTROL (whose digest ``_INTERNAL_DENIED`` carries) planted in ordinary text, and
#: a near miss that must stay green. Assembled at runtime for the reason ``_UNLISTED_OWNER`` is:
#: this script is inside its own input set, so a literal control would make it a published
#: match. Each piece is split so that no fragment is itself a candidate.
_INTERNAL_CONTROLS = (
    ("word", "per the " + "ZZ" + "HYGIENECONTROL" + "ZZ" + " guidance", "zz" + "hygienecontrol" + "zzish"),
    (
        "phrase",
        "sign in with " + "zzcontrol" + "head\n#   " + "zzcontrol" + "tail first",
        "zzcontrol" + "head unrelated",
    ),
    ("host", "https://docs." + "zzcontrol" + ".invalid/page", "not" + "zzcontrol" + ".invalid"),
    ("code", "(" + "ZZQ" + "-42 boundary)", "zzq" + "-1.6.0"),
    ("id", "cited as " + "zzq" + "_" + "Ab3dE5gH7jK9mN", "zzq" + "_processed1"),
)


def _internal_digest(kind: str, candidate: str) -> str:
    return hashlib.sha256(f"{_INTERNAL_SALT}\0{kind}\0{candidate}".encode("utf-8")).hexdigest()


def _words(text: str) -> set[str]:
    runs = set(_RUN.findall(text))
    words = {run.lower() for run in runs}
    for run in runs:
        tail = run[1:]
        if tail != tail.lower() and not run.isupper():
            words.update(part.lower() for part in _CAMEL_PART.findall(run))
    return words


def _phrase_pattern(heads: list[str]) -> re.Pattern[str]:
    alternation = "|".join(map(re.escape, heads))
    return re.compile(rf"(?<![a-z0-9])({alternation})(?=[^a-z0-9]{{1,{_PHRASE_GAP}}}([a-z0-9]+))")


def _wrapped_phrase(phrase: str) -> re.Pattern[str]:
    head, tail = (re.escape(part) for part in phrase.split(" ", 1))
    return re.compile(rf"(?<![a-z0-9]){head}(?=[^a-z0-9]{{1,{_PHRASE_GAP}}}{tail}(?![a-z0-9]))")


class _InternalMatcher:
    """Finds denylisted references while knowing the denylist only as digests; each distinct
    candidate is digested once per run and then checked with set arithmetic."""

    def __init__(self) -> None:
        self._seen: dict[str, set[str]] = {kind: set() for kind in _INTERNAL_DENIED}
        self._hits: dict[str, set[str]] = {kind: set() for kind in _INTERNAL_DENIED}

    def _denied_among(self, kind: str, candidates: set[str]) -> set[str]:
        seen = self._seen[kind]
        new = candidates - seen
        if new:
            denied = _INTERNAL_DENIED[kind]
            self._hits[kind].update(c for c in new if _internal_digest(kind, c) in denied)
            seen |= new
        return candidates & self._hits[kind]

    def references(self, text: str) -> list[tuple[str, str]]:
        """Every ``(kind, surface)`` denied reference in *text*, sorted, once each."""
        low = text.lower()
        words = _words(text)
        found = {("word", w) for w in self._denied_among("word", words)}
        heads = self._denied_among("phrase-head", words)
        if heads:
            phrases = {f"{m.group(1)} {m.group(2)}" for m in _phrase_pattern(sorted(heads)).finditer(low)}
            found.update(("phrase", p) for p in self._denied_among("phrase", phrases))
        if self._denied_among("host-head", words):
            for run in set(_HOST_RUN.findall(low)):
                labels = run.split(".")
                suffixes = {".".join(labels[start:]) for start in range(len(labels) - 1)}
                denied = self._denied_among("host", suffixes)
                if denied:
                    found.add(("host", max(denied, key=len)))
        letters = {w.rstrip(_DIGITS) for w in words if w[-1] in _DIGITS}
        if self._denied_among("code-head", words | letters):
            codes = {(f"{a}{h}{'9' * len(d)}", f"{a}{h}{d}") for a, h, d in set(_CODE.findall(low))}
            denied = self._denied_among("code", {shape for shape, _ in codes})
            found.update(("code", surface) for shape, surface in codes if shape in denied)
        if self._denied_among("id-head", words):
            ids = {
                (f"{m.group('prefix')}_{'m14' if m.group('m14') else 'l8'}", m.group(0))
                for m in _ID.finditer(text)
            }
            denied = self._denied_among("id", {shape for shape, _ in ids})
            found.update(("id", surface) for shape, surface in ids if shape in denied)
        return sorted(found)

    def located(self, text: str) -> list[tuple[int, str, str]]:
        """``(line, kind, surface)`` for each reference; a phrase wrapped across a line break is
        placed on its first word's line."""
        hits = self.references(text)
        if not hits:
            return []
        located = {
            (number, kind, surface)
            for number, line in enumerate(text.splitlines(), 1)
            for kind, surface in self.references(line)
        }
        placed = {(kind, surface) for _, kind, surface in located}
        low = text.lower()
        for kind, surface in hits:
            if kind == "phrase" and (kind, surface) not in placed:
                for match in _wrapped_phrase(surface).finditer(low):
                    located.add((low.count("\n", 0, match.start()) + 1, kind, surface))
        return sorted(located)


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
    matcher = _InternalMatcher()
    for kind, planted, near_miss in _INTERNAL_CONTROLS:
        if kind not in {found for found, _ in matcher.references(planted)}:
            failures.append(f"detector floor: the {kind} control no longer fires on {planted!r}")
        if matcher.references(near_miss):
            failures.append(f"detector floor: internal-reference falsely catches {near_miss!r}")
    for kind, entries in _INTERNAL_DENIED.items():
        if len(entries) < 2:
            failures.append(f"detector floor: the {kind} denylist holds nothing but its control")

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

    matcher = _InternalMatcher()
    for path in tracked:
        for kind, surface in matcher.references(path):
            violations.append(f"internal-reference: {path} (its PATH) names a denied {kind} ({surface!r})")

    for path in tracked:
        full = ROOT / path
        if not full.is_file():
            continue
        blob = full.read_bytes()[:_CONTENT_READ_CAP]
        if _is_binary(blob):
            size = full.stat().st_size
            if size > _MAX_BINARY_BYTES:
                violations.append(
                    f"oversized-binary: {path} is {size} bytes (ceiling {_MAX_BINARY_BYTES})"
                )
            continue
        text = blob.decode("utf-8", "replace")
        for name in set(_HOME_PATH.findall(text)):
            if name not in _PLACEHOLDER_HOMES:
                violations.append(f"real-home-path: {path} names home directory {name!r}")
        for line, kind, surface in matcher.located(text):
            violations.append(f"internal-reference: {path}:{line} names a denied {kind} ({surface!r})")

    if violations:
        print("publication-hygiene: FAIL")
        for line in sorted(violations):
            print(f"  {line}")
        if any(line.startswith("internal-reference:") for line in violations):
            print(
                "\nAn internal reference is fixed by stating the PRINCIPLE instead of naming its "
                "source (e.g. 'never trust an upload's declared type; cap decoded size'), and by "
                "renaming a fixture that borrows a non-public name. There is no exemption."
            )
        print(
            "\nDelete the path, or move it somewhere this repo publishes deliberately. Do not "
            "widen the allowlist to make CI green — every entry is reviewed as policy."
        )
        return 1

    print(f"OK: {len(tracked)} tracked paths, none unfit to publish")
    return 0


if __name__ == "__main__":
    sys.exit(main())
