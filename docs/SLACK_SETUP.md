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

The "Slack Channel App Credentials" step prompts for the App Token and the Bot Token, and
says how to pair yourself as the owner (step 3). (A plain `personalclaw setup` runs the same
step after core's own.)

**The Configure form:** Apps → Slack Channel → Configure has the same `bot_token` /
`app_token` fields.

**Headless / containers:** when those settings are empty, the app reads
`SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` (the names core exposes as
`CRED_SLACK_BOT_TOKEN` / `CRED_SLACK_APP_TOKEN`) from the credential store and then
the process environment, so a container can pass them as environment variables.
Tokens supplied that way are not the app's: uninstalling it leaves them where you
put them.

## 3. Start and verify

Tokens saved on the Configure form reach a running gateway at once: it starts the Socket
Mode receiver on them, and the Channels page reads "starting" until it is connected. After
`personalclaw setup`, or with tokens in `.env` or the environment, start the gateway, or
restart it if it was already running:

```bash
personalclaw gateway
```

Verify:

- The startup banner reports the connected channel transport (an install
  without tokens logs "no tokens — inbound stays offline" and the channel
  simply stays disabled — nothing breaks).
- If Slack cannot be reached when the gateway starts (the network is not up
  yet, or Slack is busy), the channel's card says so and when it tries again;
  inbound starts on its own once Slack answers. Only Slack refusing a token
  turns inbound off, and the card then names the token to re-check.
- `personalclaw doctor` checks the credential pair, and says whether Slack has a paired owner.
- **Pair yourself as the owner.** In Settings → Providers → Slack Channel → Configure, press
  **Pair as owner** and send the bot the code it shows, in a direct message, from your own
  account. The bot answers *Paired — you're my owner here now.* Until an owner is paired the bot
  does nothing anyone asks, and a direct message or a mention gets a note saying how to pair. Nothing else
  names the owner: not the first message the bot gets, and not an id set in the environment. An
  install from an earlier release, whose owner was the first person to message the bot or an id
  typed into setup, forgets that owner when the channel next starts and pairs once more.

## 4. Configure channels and users

Slack behavioral config (allowlist, tracked channels, activation modes) lives
in the app's own store (`~/.personalclaw/apps/slack-channel/data/config.json`),
editable from the app's Configure form, and in part from Slack itself:

- `/personalclaw #channel` — asks you whether to track a channel.
- `/personalclaw dashboard [duration]` — DMs you, the owner, a tokenized dashboard link.

People who may talk to the bot besides you are added under Allowed Users in the Configure form.

Key settings (Configure form):

| Setting | Meaning |
|---|---|
| `tracking_channels` | Channels the bot monitors: a row per channel, its channel ID with a name to know it by. |
| `allowed_users` | Users allowed to interact: a row per person, their Slack member ID with a name to know them by. |
| `dm_activation` | DM response mode: `always` (default) / `mention` / `observe` / `review` / `off`. |
| `channels` | Per-channel overrides: a row per channel, its channel ID with the activation mode and the agent that answers there. |
| `command` | The slash-command trigger word (default `personalclaw`). |
| `reactions`, `reactions_enabled` | Phase-aware emoji reactions during processing: a field per phase (queued, thinking, coding, …) for its emoji's Slack name, blank for the default, or switched off for no reaction in that phase. |
| `trusted_bot_ids` | Bot IDs allowed past the bot filter (multi-node mesh). |

Slack sessions appear in the chat UI alongside dashboard sessions
(origin = slack), and the dashboard can hand a conversation off to Slack and
back.

## Troubleshooting

- **Bot doesn't respond**: check both tokens are present (`personalclaw
  doctor`), the Channels page shows Slack connected (tokens added outside the
  Configure form while the gateway ran need a gateway restart), that an owner is paired
  (until one is, the bot's only reply is how to pair), and that you're the owner or allowlisted.
- **Socket Mode errors**: the `xapp-…` token must have the
  `connections:write` scope — regenerate it via the Socket Mode toggle dance in
  step 1.3. The Slack row on Settings → Providers says when Socket Mode is not
  connected, and what Slack answered the last reconnect: `invalid_auth` or
  `token_revoked` means the App Token was revoked or regenerated — save the new one
  on the Configure form.
- **Wrong workspace**: the bot answers only the workspace its Bot Token belongs to, and a
  message from any other is refused. To move it, install the Slack app in the other workspace
  and save that workspace's tokens.
- **Token rotation**: save the new token on the Configure form, and the receiver
  reconnects on it at once. A token rotated in `.env` or the environment needs a
  gateway restart.
