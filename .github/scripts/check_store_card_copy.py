#!/usr/bin/env python3
"""Store-card copy rail for first-party app bundles.

`docs/app-creation-guide.md` defines `description` as "One or two sentences shown on
the Store card." The card is therefore a DECLARED consumer of this field, and it is a
narrow one: `AppsSection.tsx` renders the description in a `line-clamp-2` paragraph and
appends ` · by <author>` inside that same clamp. Anything past two lines is clipped by
CSS — still in the DOM (so search and the detail panel keep every word), but never read.

That makes the LEAD SENTENCE the whole of what the Store grid communicates. When the
lead sentence is longer than two lines the card shows a fragment that stops mid-phrase:

    "Non-gated speaker diarization ("who spoke when") via a sherpa-onnx segmentation + spea"

which reads as a rendering bug rather than as a summary. When the lead sentence fits, the
clamp is invisible and the card reads as a complete thought with more detail available on
click. This rail keeps every bundle on the second side of that line.

The three rules:

  R1  Every app declares a non-empty `description`.
  R2  It ends in terminal punctuation. Two reasons, both load-bearing: the card
      concatenates ` · by <author>` directly onto it, and without a terminator the
      lead-sentence detector below cannot find a boundary and would silently measure
      the ENTIRE description as one sentence — a rail that always passes.
  R3  The lead sentence fits the card's two-line budget (<= 90 characters).

Calibration of R3 — this is a character proxy for a PIXEL constraint, so the number is
measured, not guessed. Driving the real Store grid (51 bundles, seeded gateway, Chromium)
and binary-searching the longest prefix of each card's own paragraph that still lays out
in two lines at its own rendered width:

    1440x1000 (3-up grid):  the two-line budget is 81-98 characters
     390x844  (1-up):       the two-line budget is 88-111 characters

The desktop grid is the binding constraint, and the range is a range because DM Sans is
proportional — a lead of "WWWW..." consumes more of the box than one of "illi...". A
90-character threshold reproduced the live per-card verdict EXACTLY on all 51 cards, in
both directions: it flagged the same 17 cards Chromium measured as cutting mid-sentence,
and cleared the same 34 (including four leads of 81-89 characters that genuinely fit).

If the card's geometry changes — width, font, type role, or the clamp itself — the live
measurement is the authority and this number must be re-derived from it, never nudged to
make a red go green.

Run from the repository root:  python .github/scripts/check_store_card_copy.py
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

# Measured against the shipped Store card; see the calibration note above.
_MAX_LEAD_CHARS = 90

# A lead sentence ends at the first `.`/`!`/`?` that is followed by whitespace or ends the
# string. Requiring the lookahead is what keeps "search.brave.com." and "v0.1.0" from
# reading as sentence ends mid-clause.
_LEAD = re.compile(r"^.*?[.!?](?=\s|$)", re.S)

_TERMINATORS = (".", "!", "?", "…")

# Vacuity floors. This rail is a loop over a glob: it passes trivially if the glob stops
# matching, if manifests stop parsing, or if the lead detector stops finding boundaries.
# Each floor sits BELOW today's count so ordinary growth never trips it, but high enough
# that a broken detector or a mass deletion fails here instead of going quiet.
_MIN_APPS = 40          # 51 bundles today
_MIN_MULTI_SENTENCE = 25  # 49 today: descriptions where a boundary is really INSIDE the
#                           string, which is the only case that proves R3 measured a lead
#                           sentence rather than the whole field.


def _lead_sentence(description: str) -> str:
    """The first sentence of a description, or the whole string if it has no boundary.

    R2 makes the fallback unreachable in a passing tree; it stays because a rule and the
    function it is checked with must not depend on each other to be correct.
    """
    match = _LEAD.match(description.strip())
    return match.group(0) if match else description.strip()


def main() -> int:
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    manifests = sorted(root.glob("*/app.json"))
    failures: list[str] = []
    measured: list[tuple[str, int, int]] = []  # (app, lead chars, description chars)
    multi_sentence = 0

    for manifest in manifests:
        app = manifest.parent.name
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # Not swallowed, and not double-reported either: `manifest-validate` owns the
            # "this file does not parse" red. Here it must not read as a copy pass.
            failures.append(f"{app}: app.json unreadable ({exc}); cannot check Store-card copy")
            continue

        description = (data.get("description") or "").strip()

        # R1
        if not description:
            failures.append(
                f"{app}: declares no description. The Store card renders it as the app's "
                f"only prose, so an empty one falls back to the bare kebab-case name."
            )
            continue

        # R2
        if not description.endswith(_TERMINATORS):
            failures.append(
                f"{app}: description does not end in terminal punctuation "
                f"(ends {description[-1]!r}). The card appends ' · by <author>' straight "
                f"onto it, so without a terminator the two run together as one clause."
            )

        lead = _lead_sentence(description)
        if len(lead) < len(description):
            multi_sentence += 1
        measured.append((app, len(lead), len(description)))

        # R3
        if len(lead) > _MAX_LEAD_CHARS:
            failures.append(
                f"{app}: lead sentence is {len(lead)} characters, over the Store card's "
                f"two-line budget of {_MAX_LEAD_CHARS}. The card will cut it mid-phrase:\n"
                f"        …{lead[_MAX_LEAD_CHARS - 40:_MAX_LEAD_CHARS]}┆\n"
                f"      Lead with a sentence that stands alone and move the detail into the "
                f"sentences after it — the detail panel and search read the full text, so "
                f"nothing is lost by resequencing."
            )

    # -- Vacuity floors ------------------------------------------------------
    for label, actual, floor in (
        ("app manifests discovered", len(manifests), _MIN_APPS),
        ("descriptions measured", len(measured), _MIN_APPS),
        ("multi-sentence descriptions", multi_sentence, _MIN_MULTI_SENTENCE),
    ):
        if actual < floor:
            failures.append(
                f"VACUITY: {label} = {actual}, below the floor of {floor}. This rail passes "
                f"trivially when it inspects nothing, so a shortfall means the glob or the "
                f"lead-sentence detector broke — not that the repository got cleaner."
            )

    if failures:
        print("Store-card copy rail FAILED:\n")
        for failure in failures:
            print(f"  - {failure}\n")
        return 1

    longest = max(measured, key=lambda row: row[1])
    print(
        f"OK: {len(manifests)} app manifests, every lead sentence inside the Store card's "
        f"{_MAX_LEAD_CHARS}-character two-line budget."
    )
    print(f"    longest lead: {longest[0]} at {longest[1]} chars (description {longest[2]} chars)")
    print(f"    multi-sentence descriptions: {multi_sentence}/{len(measured)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
