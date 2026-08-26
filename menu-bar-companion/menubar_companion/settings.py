"""Local preferences + gateway credentials for the companion.

State lives on the USER'S machine, not in the gateway's home: this app is never
server-installed, so the platform never hands it a ``DATA_DIR`` (that is exactly why
the manifest does not claim ``storage``). The location is resolved from the
environment on EVERY call and cached nowhere — a cached path is the bug that makes a
test leak into the real home once a consumer module has already imported.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

#: Overrides the state directory. Read live on every call, never cached.
HOME_ENV = "PERSONALCLAW_COMPANION_HOME"
#: Supply the gateway URL/token WITHOUT persisting them. An env value wins over the
#: file and is never written back, so a user who does not want a token on disk has a
#: first-class way to say so.
URL_ENV = "PERSONALCLAW_COMPANION_URL"
TOKEN_ENV = "PERSONALCLAW_COMPANION_TOKEN"

_DEFAULT_HOME = "~/Library/Application Support/PersonalClaw Companion"


def companion_home() -> Path:
    """The directory this app keeps its own state in."""
    raw = os.environ.get(HOME_ENV, "").strip() or _DEFAULT_HOME
    return Path(raw).expanduser()


def settings_path() -> Path:
    return companion_home() / "settings.json"


@dataclass
class Settings:
    """What the user configured.

    ``notifications_muted`` is the Settings-menu mute switch. It is a stored
    PREFERENCE (unlike the badge, which is derived) because nothing else can tell us
    what the user wants.
    """

    url: str = ""
    token: str = ""
    notifications_muted: bool = False
    #: Floor poll, seconds. The socket is the fast path; this is the guarantee that a
    #: change which happens to produce no frame still lands eventually.
    poll_seconds: int = 60
    #: Where this instance was loaded from, so ``save`` writes back to the same file
    #: even if the environment changes underneath a long-lived process.
    path: Path = field(default_factory=settings_path, compare=False)

    @classmethod
    def load(cls) -> "Settings":
        path = settings_path()
        raw: dict = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    raw = loaded
            except (OSError, ValueError):
                # A corrupt preferences file must not stop the companion from
                # starting; defaults plus the env are enough to be useful.
                raw = {}
        env_url = os.environ.get(URL_ENV, "").strip()
        env_token = os.environ.get(TOKEN_ENV, "").strip()
        return cls(
            url=env_url or str(raw.get("url", "")).strip(),
            token=env_token or str(raw.get("token", "")).strip(),
            notifications_muted=bool(raw.get("notifications_muted", False)),
            poll_seconds=max(5, int(raw.get("poll_seconds", 60) or 60)),
            path=path,
        )

    def to_dict(self) -> dict:
        """What gets persisted.

        An env-supplied URL/token is deliberately still written when the user asked us
        to remember it — but ``save_preferences`` is what the Settings menu calls, and
        that never touches credentials at all.
        """
        return {
            "url": self.url,
            "token": self.token,
            "notifications_muted": self.notifications_muted,
            "poll_seconds": self.poll_seconds,
        }

    def save(self) -> Path:
        """Persist, 0600. The token is a bearer credential for the whole gateway."""
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    def save_preferences(self) -> Path:
        """Write only the preference half, preserving whatever credentials are on disk.

        The Settings menu toggles mute. If that wrote ``self.token`` back, a user who
        supplied the token by environment variable would find it silently persisted by
        an unrelated click. So the mute write re-reads the file and edits one key.
        """
        path = self.path
        on_disk: dict = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    on_disk = loaded
            except (OSError, ValueError):
                on_disk = {}
        on_disk["notifications_muted"] = self.notifications_muted
        on_disk["poll_seconds"] = self.poll_seconds
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(on_disk, indent=2) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    def set_muted(self, muted: bool) -> Path:
        self.notifications_muted = bool(muted)
        return self.save_preferences()
