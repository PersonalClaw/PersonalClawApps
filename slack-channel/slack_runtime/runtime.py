"""SlackRuntime — the app-side facade the Slack modules run against.

Historically the Slack event/interaction/handler modules received the core
``GatewayOrchestrator`` as ``orch`` and reached into ~15 Slack-specific attributes
on it (the Slack client, tokens, tracking-channels, socket connection, per-message
identity bookkeeping) *plus* a dozen genuine core services (sessions, cron, history,
dashboard state). Moving Slack into this app bundle, that split becomes explicit:

- **Slack-owned state** (Group A) lives HERE on the runtime.
- **Core services** (Group B) come from a :class:`GatewayServices` handle the
  gateway passes to ``start_inbound`` — proxied transparently via ``__getattr__``
  so the existing ``orch.sessions`` / ``orch.cron_svc`` / … call sites in the moved
  modules keep working unchanged.

The runtime therefore stands in for ``orch`` everywhere the Slack modules used it,
with NO import of the core orchestrator (only the public ``GatewayServices``
protocol). This is the clean core↔channel boundary.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from personalclaw.sdk.channel import (
    CRED_OWNER_ID,
    CRED_SLACK_APP_TOKEN,
    CRED_SLACK_BOT_TOKEN,
)

from slack_runtime.client import RealSlackClient

if TYPE_CHECKING:
    from personalclaw.sdk.channel import GatewayServices
    from slack_runtime.settings import SlackSettings

logger = logging.getLogger(__name__)


class SlackRuntime:
    """Holds Slack-owned state; proxies core services to a GatewayServices handle."""

    def __init__(self, services: "GatewayServices") -> None:
        self._services = services
        cfg = services.config

        creds = cfg.load_credentials()
        self._app_token: str = creds.get(CRED_SLACK_APP_TOKEN, "")
        self._bot_token: str = creds.get(CRED_SLACK_BOT_TOKEN, "")
        self._owner_id: str = creds.get(CRED_OWNER_ID, "") or services.owner_id

        # Slack behavioral config comes from the app's OWN store (SlackSettings) —
        # core AppConfig defines no Slack config. get_settings() caches one live
        # instance; !channel/!config writes call reload_settings() so this stays fresh.
        from slack_runtime.settings import reload_settings

        settings = reload_settings()

        # Owner-only access (multi-user disabled). Prune stale allowlist entries.
        self._allowed_users: set[str] = {self._owner_id} if self._owner_id else set()
        self._tracking_channels: set[str] = {
            c["channel_id"] for c in settings.tracking_channels if c.get("channel_id")
        }
        self._open_channels: set[str] = set(settings.open_channels)
        self._slack_enabled: bool = bool(self._app_token and self._bot_token)
        self.slack_command: str = settings.command

        # The live Slack client + socket connection (created in start()).
        self.slack: RealSlackClient | None = None
        self._socket_client: Any = None

        # Per-message identity + task bookkeeping (Slack-transport concerns).
        self._handler_tasks: set[asyncio.Task] = set()
        self._session_tasks: dict[str, asyncio.Task] = {}
        self._pending_queue: dict[str, list] = {}
        self._self_bot_id: str = ""
        self._self_bot_id_ts: float = 0.0
        self._auth_test_failures: int = 0
        self._auth_test_lock: asyncio.Lock = asyncio.Lock()
        self._last_trigger_id: str = ""

    # --- Group B: transparently proxy core services to the gateway handle ---
    def __getattr__(self, name: str) -> Any:
        # Only called for attributes NOT found on the instance — i.e. the core
        # services (sessions, ctx_builder, conv_log, consolidator, cron_svc,
        # subagent_mgr, channel_history, dashboard_state) + config/owner_id.
        services = self.__dict__.get("_services")
        if services is not None and hasattr(services, name):
            return getattr(services, name)
        raise AttributeError(name)

    # Some moved code reads orch._cfg directly (private). Expose it as the
    # services' public config so those call sites resolve without change.
    @property
    def _cfg(self) -> Any:
        return self._services.config

    @property
    def settings(self) -> "SlackSettings":
        """The app's live SlackSettings (cached; refreshed by reload_settings())."""
        from slack_runtime.settings import get_settings

        return get_settings()
