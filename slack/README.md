# Slack

Slack integration with three independent capabilities:

## Capabilities

### Chat with your agent in Slack (`channel`)
- Real-time messaging with the agent
- Supports reactions and threads
- Requires: bot token, app token, allowed users configuration

### Slack messages in your Inbox (`inbox`)
- Polls Slack for new messages
- Displays messages in your Inbox
- Requires: bot token only
- Can run independently of the channel capability

### Run automations on Slack messages (`trigger_source`)
- Triggers automations on Slack messages
- Events: `app:slack:direct_message`, `app:slack:channel_message`
- Requires: bot token, app token

## Settings

| Setting | Location | Description |
|---------|----------|-------------|
| bot_token | Account | Slack bot token (xoxb-*) |
| app_token | Account | Slack app token (xapp-*) |
| allowed_users | Channel | List of allowed user IDs |
| tracking_channels | Channel | List of channels to track |
| command | Channel | Custom slash command |
| trusted_bot_ids | Channel | Trusted bot user IDs |
| reactions | Channel | Emoji reactions to handle |
| reactions_enabled | Channel | Enable reaction processing |
| channels | Channel | Per-channel configuration |
| dm_activation | Channel | Enable DM activation |

## Migration from Slack Channel

If you have the previous `slack-channel` app installed, settings will automatically migrate to the new `slack` app on next startup. Your automations will be updated to use the new event names (`app:slack:direct_message` and `app:slack:channel_message`).
