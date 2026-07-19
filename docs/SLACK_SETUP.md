# Slack Setup

Connect PersonalClaw to a Slack workspace via the **slack-channel** app
(`apps/slack-channel`). End state: the bot answers DMs and mentions, tracks the
channels you choose, and streams agent responses into threads.

## Prerequisites

- The **Slack Channel** app installed in PersonalClaw (from the Store — add the
  `apps/` directory as a local source if it isn't listed; it installs through
  the normal scanner + lifecycle).
- Permission to create apps in your Slack workspace.

## 1. Create the Slack app

1. Go to <https://api.slack.com/apps> → **Create New App** → **From a manifest**.
2. Paste the contents of `apps/slack-channel/slack-manifest.yaml`, replacing
   `{{USERNAME}}` with your name (it names the bot `PersonalClaw-<you>`). The
   manifest pre-configures Socket Mode, the bot scopes, event subscriptions,
   and the `/personalclaw` slash command.
3. **App-Level Token (`xapp-…`)**: Socket Mode needs a token the manifest can't
   create. In the app's settings, toggle **Socket Mode** OFF and back ON to
   trigger the token-generation dialog → add the `connections:write` scope →
   **Generate** → copy the `xapp-…` token.
4. **Bot Token (`xoxb-…`)**: **Install to Workspace**, then copy the
   Bot User OAuth Token from OAuth & Permissions.

## 2. Configure the tokens

The tokens are credentials, stored in PersonalClaw's credential store
(`~/.personalclaw/.env`) under the keys `SLACK_BOT_TOKEN` and
`SLACK_APP_TOKEN` — the keys core exposes to the app as `CRED_SLACK_BOT_TOKEN`
/ `CRED_SLACK_APP_TOKEN`. Three equivalent ways:

**Interactive setup (recommended):**

```bash
personalclaw setup
```

The wizard's "Slack Channel App Credentials" step prompts for the App Token,
Bot Token, and (optionally) your Slack Member ID, and writes them to
`~/.personalclaw/.env` with `0600` permissions.

**Headless / scripted:** append the keys to `~/.personalclaw/.env` directly
(one `KEY=VALUE` per line):

```bash
cat >> ~/.personalclaw/.env <<'EOF'
SLACK_APP_TOKEN=xapp-...
SLACK_BOT_TOKEN=xoxb-...
EOF
chmod 600 ~/.personalclaw/.env
```

(Non-interactive `personalclaw setup --mode/--provider/--credential` flags exist
for deployment scripting, but the Slack pair is simplest to write to `.env`
directly.)

**Per-instance app config:** the app's Configure form (Apps → Slack Channel →
Configure) has `bot_token` / `app_token` fields. A value set there wins over the
`.env` credentials for that instance; leave them empty to fall back to `.env`.

Environment variables of the same names override `.env` values.

## 3. Start and verify

Restart the gateway (backend changes and new credentials load at boot):

```bash
personalclaw gateway
```

Verify:

- The startup banner reports the connected channel transport (an install
  without tokens logs "no tokens — inbound stays offline" and the channel
  simply stays disabled — nothing breaks).
- `personalclaw doctor` checks the credential pair.
- DM your bot in Slack. **The first person to DM the bot is auto-claimed as the
  owner** — do this from your own account.

## 4. Configure channels and users

Slack behavioral config (allowlist, tracked channels, activation modes) lives
in the app's own store (`~/.personalclaw/apps/slack-channel/data/config.json`),
editable from the app's Configure form or from Slack itself:

- `/personalclaw @user` — allowlist another user.
- `/personalclaw #channel` — track a channel.
- `/personalclaw dashboard [duration]` — get a tokenized dashboard link.

Key settings (Configure form):

| Setting | Meaning |
|---|---|
| `tracking_channels` | Channels the bot monitors (`{channel_id, name}` entries). |
| `open_channels` | Channel IDs where ALL users may interact without the allowlist. |
| `allowed_users` | Users allowed to interact (`{slack_id, name}`). |
| `dm_activation` | DM response mode: `always` (default) / `mention` / `observe` / `review` / `off`. |
| `channels` | Per-channel overrides: `{channel_id: {activation, agent}}`. |
| `command` | The slash-command trigger word (default `personalclaw`). |
| `reactions`, `reactions_enabled` | Phase-aware emoji reactions during processing (queued/thinking/coding/…), per-phase overridable. |
| `trusted_bot_ids` | Bot IDs allowed past the bot filter (multi-node mesh). |
| `allowed_enterprise_ids` | Slack Enterprise Grid org IDs allowed for workspace validation. |

Slack sessions appear in the chat UI alongside dashboard sessions
(origin = slack), and the dashboard can hand a conversation off to Slack and
back.

## Troubleshooting

- **Bot doesn't respond**: check both tokens are present (`personalclaw
  doctor`), the gateway was restarted after adding them, and you're either the
  owner, allowlisted, or in an open channel.
- **Socket Mode errors**: the `xapp-…` token must have the
  `connections:write` scope — regenerate it via the Socket Mode toggle dance in
  step 1.3.
- **Wrong workspace / Enterprise Grid**: if you set `allowed_enterprise_ids`,
  the connection refuses workspaces outside that list.
- **Token rotation**: update `.env` (or the Configure form) and restart the
  gateway.
