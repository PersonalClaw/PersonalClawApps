"""CLI seams for rsync-sync: a setup step that marks the sync root, and a doctor probe.

``personalclaw setup`` calls :func:`setup` after the core steps, and ``personalclaw setup --app
rsync-sync`` calls it alone; ``personalclaw doctor`` calls :func:`doctor` and renders the lines
it returns as this app's section.

Rsync Sync syncs only with a sync root that holds its marker file (see
``rsync_sync_transport.py``). The empty folder a disk or share leaves when it isn't mounted looks
just like a new sync root, so the owner is the one who says it is one, here.
"""

from __future__ import annotations

import subprocess

from rsync_sync_transport import RootRefused, RsyncFailed, create_provider

from personalclaw.sdk.cli import DoctorLine, SetupContext
from personalclaw.sdk.settings import ProviderSettings

APP_NAME = "rsync-sync"
LABEL = "Rsync Sync"
#: How long doctor's look at the sync root may take: doctor stops waiting for a probe at 5s.
DOCTOR_TIMEOUT_SECS = 4
_SETUP = "personalclaw setup --app rsync-sync"


def _in_use(saved: dict) -> bool:
    """Whether the owner has set this transport up at all, rightly or not."""
    return any(str(saved.get(name, "") or "").strip() for name in ("host", "path"))


def setup(ctx: SetupContext) -> None:
    """Mark the sync root, once the owner says the disk or share it's on is mounted.

    A transport that isn't set up has nothing to mark. A root that isn't there, or a setting or
    listing that fails, fails the step with what to do; a root the owner doesn't confirm (or a
    run with no one to ask) stays unmarked, and the step says how to mark it later."""
    saved = ctx.settings.load(ctx.app_name) or {}
    if not _in_use(saved):
        ctx.print(f"{LABEL}: not set up, so there is no sync root to mark.")
        return
    provider = create_provider(saved)
    if not provider.configured:
        raise RsyncFailed(
            f"{provider._unconfigured_detail()}. Fix that on the Rsync Sync card in Settings → "
            f"Providers, then run {_SETUP} again.",
            "",
        )
    state, sentence = provider.root_state()
    if state == "missing":
        raise RootRefused(sentence)
    if state == "marked":
        ctx.print(f"{LABEL}: {sentence}")
        return
    ctx.print(
        f"{LABEL}: the sync root path {provider.root_label} isn't marked as the sync root yet. "
        "Rsync Sync syncs only with a folder that is, so that the empty folder a disk or share "
        "leaves when it isn't mounted is never synced in its place."
    )
    answer = ctx.input(
        "Is the disk or share it's on mounted, and is it the folder to sync with? Mark it? [y/N] "
    )
    if answer.strip().lower() not in ("y", "yes"):
        ctx.print(
            f"{LABEL}: not marked, so Rsync Sync won't sync until it is. Once its disk or share "
            f"is mounted, run {_SETUP} again."
        )
        return
    ctx.print(f"{LABEL}: {provider.mark_root()}")


def doctor() -> list[DoctorLine]:
    """Report whether the sync root is there and marked: without both, nothing syncs."""
    saved = ProviderSettings.load(APP_NAME) or {}
    if not _in_use(saved):
        return [DoctorLine("sync root", "info", "not set up, so Rsync Sync is idle")]
    provider = create_provider({**saved, "timeout_secs": DOCTOR_TIMEOUT_SECS})
    if not provider.configured:
        return [DoctorLine("sync root", "fail", provider._unconfigured_detail())]
    try:
        state, sentence = provider.root_state()
    except RsyncFailed as e:
        if isinstance(e.__cause__, subprocess.TimeoutExpired):
            return [DoctorLine(
                "sync root",
                "warn",
                f"no answer within {DOCTOR_TIMEOUT_SECS} seconds, so doctor couldn't check it. "
                f"{_SETUP} checks it with no such limit.",
            )]
        return [DoctorLine("sync root", "fail", str(e))]
    return [DoctorLine("sync root", "ok" if state == "marked" else "fail", sentence)]
