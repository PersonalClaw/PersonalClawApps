"""The macOS status-item host — the one part of this app that needs a GUI toolkit.

Everything else in the package is plain stdlib and runs (and is tested) anywhere. That
split is deliberate: the menu's CONTENT, its badge, its writes and its socket discipline
are all verifiable without a window server, and only the chrome depends on a Mac.

The host is ``rumps`` (a thin wrapper over PyObjC's ``NSStatusItem``). It is imported
lazily and is NOT a declared ``pythonDependencies`` entry: a client app's dependency is
installed by the ``platform.clientInstall`` one-liner on the user's own Mac, and pinning
a darwin-only wheel in the manifest would also break the apps-repo test job, which runs
on Linux and installs declared dependencies before collecting.

If no backend is present, :func:`resolve_host` says so with a reason the caller can
print, and ``run.py`` falls back to a headless render. A companion that reports why it
cannot draw itself is more useful than one that dies importing Cocoa.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from menubar_companion.model import (
    APPROVE,
    DENY,
    REVIEW,
    ApprovalRow,
    CompanionModel,
    render,
)
from menubar_companion.settings import Settings


class HostUnavailable(RuntimeError):
    """No macOS status-item backend is importable here."""


@dataclass(frozen=True)
class MenuItem:
    """One line of the status-item menu, with what clicking it does."""

    title: str
    action: Callable[[], None] | None = None
    #: A URL to open (where an item is answered, a piece of work's page, an approval's review).
    #: Kept as data so the menu is assertable without a browser and without a toolkit.
    url: str = ""
    #: The submenu this item opens, in order (an approval's brief and its answers).
    children: tuple["MenuItem", ...] = ()


def build_menu(
    model: CompanionModel,
    settings: Settings,
    *,
    on_resolve: Callable[[str, str], None],
    on_toggle_mute: Callable[[], None],
    on_refresh: Callable[[], None],
    open_url: Callable[[str], None],
) -> list[MenuItem]:
    """The menu as data: titles plus the exact callback each one fires.

    Built from ``model``'s derived properties — the same ones ``badge`` counts — so the
    rows and the badge cannot describe different worlds.
    """
    items: list[MenuItem] = []

    rows = model.waiting_rows
    items.append(MenuItem(title=f"Waiting on you ({len(rows)})"))
    for row in rows:
        if row.approval is not None:
            # Each approval opens a submenu: its brief first, then what may answer it. There
            # is no way to Approve except from under the brief.
            items.append(
                MenuItem(
                    title=f"  {row.title}",
                    children=_approval_menu(row.approval, on_resolve, open_url),
                )
            )
            continue
        # Every other kind is answered where the list says: one click lands on it.
        items.append(
            MenuItem(title=f"  {row.title}", action=_opener(open_url, row.link), url=row.link)
        )
        # What it asks, under it: the first lines when its row cuts them, and where it came from.
        items.extend(MenuItem(title=f"    {line}") for line in row.text_lines)
        if row.source:
            items.append(MenuItem(title=f"    {row.source}"))

    runs = model.runs
    items.append(MenuItem(title=f"Running ({len(runs)})"))
    for run in runs:
        items.append(
            MenuItem(
                title=f"  {run.name} — {run.line}" if run.line else f"  {run.name}",
                action=_opener(open_url, run.link),
                url=run.link,
            )
        )

    if model.unreadable_line:
        # A source the gateway could not read is said, never shown as nothing waiting.
        items.append(MenuItem(title=f"⚠ {model.unreadable_line}"))
    if model.last_error:
        # A failed write or read is shown IN THE MENU the click happened in.
        items.append(MenuItem(title=f"⚠ {model.last_error}"))

    items.append(MenuItem(title="Refresh now", action=on_refresh))
    muted = settings.notifications_muted
    items.append(
        MenuItem(
            title="Settings: Unmute notifications" if muted else "Settings: Mute notifications",
            action=on_toggle_mute,
        )
    )
    return items


def _approval_menu(
    approval: ApprovalRow,
    on_resolve: Callable[[str, str], None],
    open_url: Callable[[str], None],
) -> tuple[MenuItem, ...]:
    """One approval's submenu: the brief's lines (read-only), then the answers it offers."""
    answer: dict[str, MenuItem] = {
        APPROVE: MenuItem(title=APPROVE, action=lambda: on_resolve(approval.id, "approve")),
        DENY: MenuItem(title=DENY, action=lambda: on_resolve(approval.id, "deny")),
        REVIEW: MenuItem(
            title=REVIEW,
            action=_opener(open_url, approval.review_link),
            url=approval.review_link,
        ),
    }
    return (
        *(MenuItem(title=line) for line in approval.brief),
        *(answer[a] for a in approval.answers),
    )


def _opener(open_url: Callable[[str], None], url: str) -> Callable[[], None]:
    """The click that opens *url*: bound now, so each item opens its own link."""
    return lambda: open_url(url)


def resolve_host() -> tuple[object | None, str]:
    """Return ``(rumps_module, "")`` or ``(None, reason)``."""
    try:
        import rumps  # noqa: PLC0415 - the whole point is that this import is lazy
    except Exception as exc:  # noqa: BLE001 - any import failure is "no host here"
        return None, (
            "no macOS status-item backend: "
            f"{type(exc).__name__}: {exc}. Install it with "
            "`python3 -m pip install --user rumps` on the Mac that should show the "
            "menu bar item (see platform.clientInstall in app.json)."
        )
    return rumps, ""


def headless_render(model: CompanionModel, settings: Settings) -> str:
    """The menu as text — what ``run.py --check`` prints, and the fallback surface."""
    menu = render(model, muted=settings.notifications_muted)
    badge = menu.badge or "0"
    return "\n".join([f"PersonalClaw • badge {badge}", *menu.lines])
