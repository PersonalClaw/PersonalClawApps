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

The tokens are credentials. Setup and the Configure form both save them as this
app's `bot_token` / `app_token` settings, which keep each value in PersonalClaw's
credential store (the OS keychain when you enabled it, else `~/.personalclaw/.env`
at `0600`) under a key the app owns. The settings file holds only a reference, and
uninstalling the app removes both tokens.

**Interactive setup (recommended):**

```bash
personalclaw setup --app slack-channel
```

The "Slack Channel App Credentials" step prompts for the App Token, Bot Token, and
(optionally) your Slack Member ID. (A plain `personalclaw setup` runs the same step
after core's own.)

**The Configure form:** Apps → Slack Channel → Configure has the same `bot_token` /
`app_token` fields.

**Headless / containers:** when those settings are empty, the app reads
`SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` (the names core exposes as
`CRED_SLACK_BOT_TOKEN` / `CRED_SLACK_APP_TOKEN`) from the credential store and then
the process environment, so a container can pass them as environment variables.
Tokens supplied that way are not the app's: uninstalling it leaves them where you
put them.

## 3. Start and verify

Restart the gateway. The Socket Mode receiver starts at boot; outbound picks saved
tokens up without a restart, and the Channels page says so while inbound waits for it:

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
