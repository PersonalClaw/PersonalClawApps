"""CLI seams for docs-slides: a setup step and a doctor probe.

``personalclaw setup`` calls :func:`setup` after the core steps; ``personalclaw doctor``
calls :func:`doctor` and renders the lines it returns as this app's section.

There is nothing to collect at setup — the app has no key, no endpoint and no network.
What it DOES have is a dependency on which document writers the HOST build registered,
and that is exactly the class of thing doctor exists to say out loud: whether .pptx and
.docx can be rendered is a property of the installed core, not of this bundle, and a
user told "no writer" before their first brief is a user who does not lose one.
"""

from __future__ import annotations

from personalclaw.sdk.cli import DoctorLine, SetupContext
from personalclaw.sdk.documents import available_formats

from provider import DECK_FORMAT, DOCUMENT_FORMATS

LABEL = "Docs & Slides"


def _renderable() -> tuple[list[str], list[str]]:
    formats = available_formats()
    decks = [DECK_FORMAT] if DECK_FORMAT in formats else []
    docs = [f for f in DOCUMENT_FORMATS if f in formats]
    return decks, docs


def setup(ctx: SetupContext) -> None:
    """No credential to collect — just tell the user what this build can render."""
    decks, docs = _renderable()
    if decks and docs:
        ctx.print(
            f"{LABEL}: ready — decks ({', '.join(decks)}) and documents "
            f"({', '.join(docs)}). Nothing to configure; the app opens no network "
            "connection and writes only inside its own data directory."
        )
        return
    missing = ", ".join(
        part
        for part in (
            "" if decks else "decks (.pptx)",
            "" if docs else "documents (.docx/.pdf)",
        )
        if part
    )
    ctx.print(
        f"{LABEL}: this build cannot render {missing}. The renderers live in core, not in "
        "this app — reinstall PersonalClaw with its document extras and re-run doctor."
    )


def doctor() -> list[DoctorLine]:
    """Report which of this app's two outputs the HOST build can actually render."""
    decks, docs = _renderable()
    if decks and docs:
        return [
            DoctorLine(
                label=LABEL,
                status="ok",
                detail=f"decks: {', '.join(decks)}; documents: {', '.join(docs)}",
            )
        ]
    if decks or docs:
        return [
            DoctorLine(
                label=LABEL,
                status="warn",
                detail=(
                    f"only {', '.join(decks + docs)} is renderable in this build — the other "
                    "tool refuses rather than writing a file nothing can open"
                ),
            )
        ]
    return [
        DoctorLine(
            label=LABEL,
            status="fail",
            detail=(
                "no document writer is registered in this build — reinstall PersonalClaw "
                "with its document extras"
            ),
        )
    ]
