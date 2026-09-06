"""CLI seams for personalclaw-design-critique: a setup step and a doctor probe.

``personalclaw setup`` calls :func:`setup` after the core steps; ``personalclaw doctor``
calls :func:`doctor` and renders the lines it returns as this app's section.
"""

from __future__ import annotations

from personalclaw.sdk.cli import DoctorLine, SetupContext


def setup(ctx: SetupContext) -> None:
    """Nothing to collect: the app has no credentials and no external service.

    Both dependencies it does have (the guarded fetch for a URL, the Pillow decoder for a
    screenshot) ship with core, so setup states the posture instead of asking for input.
    """
    ctx.print(
        "Design Critique needs no configuration. A URL review goes through your "
        "Security → Network egress settings; a screenshot review reads only the file you "
        "name."
    )


def doctor() -> list[DoctorLine]:
    """Report whether each half of the review can actually run.

    The screenshot half is the one that can genuinely be unavailable (an image decoder
    missing from the install), and it fails at invoke time rather than at registration —
    exactly the class of problem doctor exists to surface early.
    """
    lines: list[DoctorLine] = []
    try:
        from PIL import Image  # noqa: F401

        lines.append(
            DoctorLine(
                label="Design Critique · screenshot review",
                status="ok",
                detail="Pillow available — design_critique_image can decode captures",
            )
        )
    except ImportError:
        lines.append(
            DoctorLine(
                label="Design Critique · screenshot review",
                status="warn",
                detail=(
                    "Pillow is not importable, so design_critique_image cannot run. "
                    "Reinstall dependencies (`pip install -e .`). The URL review is "
                    "unaffected."
                ),
            )
        )
    lines.append(
        DoctorLine(
            label="Design Critique · page review",
            status="ok",
            detail=(
                "design_critique_page fetches through core's egress guard, so a private or "
                "denied host is refused by Settings → Security → Network"
            ),
        )
    )
    return lines
