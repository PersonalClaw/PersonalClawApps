# Slack Channel

Slack workspace integration. Monitor channels, respond to mentions, and interact via Slack.

**Slack Channel** is a **channel-transport provider** — it implements the `personalclaw.sdk.channel` `ChannelTransportProvider` contract and shows up under the messaging channels.

## What this is

A standalone PersonalClaw app bundle (part of the core/app workspace split). It ships
as a self-contained directory:

- `app.json` — the manifest (provider type + `implementation`; Tier-2 apps carry no `native` flag — that's Tier-1-only).
- `provider.py` — the implementation, exposed via `create_provider`.
- `test_provider.py` — the app's own tests.

It imports only the PersonalClaw **SDK** (never core internals), so core can evolve
without breaking it:

- `personalclaw.sdk.channel`
- `(vendored Slack client — app-local)`

## Automations from inbound traffic

This bundle also registers a **`trigger_source`** provider
(`slack_runtime.trigger_source:create_provider`), so a message that arrives here can fire a
`kind: event` automation. Event names are namespaced by core from the app name:

- `app:slack-channel:direct_message` — a message in a DM with the bot.
- `app:slack-channel:channel_message` — a message in a tracked channel, or an @mention in one.

Author a trigger with pattern `AppEvent` and an `event_glob` matching one of those (or
`app:slack-channel:*` for any of them). The message body arrives as the payload, **fenced at
origin** by core; `meta` carries identifiers only (`channel_id`, `sender`, `thread_id`,
`is_dm`).

Three things this deliberately does *not* do:

- **It observes nothing your trust gate refused.** The publish happens at the one point
  where a message has cleared this app's allowlist / open-channel / tracked-channel gate,
  its channel activation mode and its dedup cache. (This app predates core's guarded door —
  see `tests/test_conformance.py`'s strict xfail — so the gate here is its own.)
  A denied sender gets no session and arms no automation.
- **The event name never comes from the message.** It is chosen in code from the frozen
  list above by a structural fact, so a sender cannot pick which of your automations runs.
- **Prose never lands in `meta`.** `meta` is matched, not narrated, and core does not fence
  it — so a sender's Slack profile name is not there.

## Messages in your Inbox

The bundle's third provider is an **inbox source** (`slack_runtime.inbox_source:create_provider`):
PersonalClaw's Inbox polls it while the app is enabled, for the channels you list in
**Settings → Inbox → Channels to read**, each by its id (like `C0123456789`, at the bottom of
the channel's About tab; the bot has to be in the channel). Each new message in one of them
becomes a row you can draft and send a reply to, posted in its thread. A channel's first poll
starts after its newest message, so its history is not surfaced and fires none of your inbox
automations; what is posted after it is. Nothing posted after it is skipped: a poll pages
through everything new, up to 1,000 messages a channel, and a busier channel's older messages
arrive over the next polls. When no watched channel can be read (a revoked token, the bot not in
the channel), the Inbox says so under Slack's name, with Slack's own reason for each channel.

## Approvals

When an agent asks to run a tool, the prompt says what will run, as PersonalClaw's own
approval card does: where the call came from (*[loop “Fix the README”]*, *[workflow
“deep-research” · step “sweep”]*), shown as written, the tool, its arguments, why the agent is
calling it, what the call can touch with its risk (*Can: runs a command · Risk: Destructive*)
and, for a command that reaches a host off your allowed hosts, a line saying so. Keys and
exfiltration links in the arguments are masked by PersonalClaw before they reach Slack. Long
arguments are split over as many code blocks, and messages, as they take, the buttons on the
last.

**The prompt shows how its approval ended.** An approval PersonalClaw asks here ends when you
press one of its buttons, when you answer it in PersonalClaw, when nobody answers within
PersonalClaw's approval wait (*Settings → Agent defaults → Approval wait*, up to a week), or
when the work that asked for it stops first. The prompt then loses its buttons and says which:
*✅ Approved*, *🚫 Rejected*, *⌛ Nobody answered in time, so it did not run*, or *⏹️ Cancelled:
the work that asked for it stopped first, so it did not run*. A press after that is answered, in
a message only you see, with how it ended; on a prompt left from before a restart, it says the
approval is no longer waiting, and the buttons come off. PersonalClaw decides how long its
approvals wait; this app keeps no clock of its own for them.

A prompt offers the answers PersonalClaw's approval card offers for that call: **Allow once**
and **Deny**, and **Allow for this chat** on a prompt in the chat that is asking (never for a
call that may destroy something, or for a command that reaches a host off your allowed hosts,
which is asked about every time), which the prompt explains first, in the card's words. That one
trusts the chat in PersonalClaw, as the card's *This chat* does: its Permission mode shows Trust
and you switch it off there.

That holds for a thread this app runs itself too, in a DM. Allow for this chat there is the Trust
of the chat PersonalClaw lists for the thread (under Slack in your chat history): open it and its
Permission mode shows **Trust**. The rest of the thread's tool calls run without asking while it
is on, and once you switch it back to Normal there, the thread's next call asks again. The
security log records the grant, and the switch-off, as for any chat. This app keeps no trust of
its own, so nothing survives a switch-off, and a restart of PersonalClaw ends the Trust as it
does any chat's. Allow for this chat is not offered in a channel thread (the thread is in front
of everyone in it), nor when PersonalClaw has no dashboard to show the chat in, or the thread is
Incognito or Temporary, whose chats are never listed. Such a prompt waits as long as
PersonalClaw's approval wait, and goes once it has ended. If nobody answers in that time, the call
does not run and the thread says *⌛ Nobody answered in time, so it did not run*: that is not a
Reject, and the security log records it as decided by nobody. A press after the approval has
ended, on a client still showing the prompt, changes nothing, and you are told how it ended.
The chat PersonalClaw opens for the thread is given each turn the app writes, and the title it
gives the thread, so it shows the thread as it is.

The answers come from PersonalClaw, so the app declares the core features that hand them over
and hold the chat's Trust (`requiresCoreFeatures`: `approval-answers`, `chat-trust`), and a
PersonalClaw without them will not install or update the app. A PersonalClaw from before that
check sends no answers. A prompt it asks for then has no buttons and says so, telling you to
answer it in PersonalClaw and to update PersonalClaw; a call in a thread this app runs itself,
which nothing else can ask you about, does not run, and the thread says why. The gateway log says
it once.

## Compaction

`!compact` compacts the thread's conversation and says how much that freed (*✅ Compacted: freed
42% of the conversation (12,000 → 6,960 characters)*), or that there was nothing to compact. When
the agent compacts the conversation on its own in the middle of a reply, the thread says so in the
same words, in a message after the reply, and the reply keeps everything it said before.

## Results from your schedules

A schedule can send its results here too. In the schedule's Advanced → Notify channel, pick
Slack, then **You, in a direct message** or **A chat or channel** with the channel's id, like
`C0123456789` (at the bottom of the channel's About tab). Your DMs need Slack to know who you
are, its owner. Slack checks the id when you save and says what's wrong if it can't send
there.

## Who a message notifies

Only the people this app means to. Text a model wrote, or a sender, goes to Slack as the
characters it is: `&`, `<` and `>` are sent as the entities Slack's escaping rules give them, in
code as well, so a reply, a scheduled result, a notification, an approval prompt or an Inbox reply
that contains `<!channel>`, `<!here>` or `<@U…>` shows those characters and notifies no one. Text
streamed while a reply is being written is sent the same way. Every mrkdwn text in a block this
app sends is verbatim, so Slack does not read a plain `@here` or `#name` as the mention or the
channel either. A markdown link, a link in Slack's syntax and a bare address become links when
they point at a web or mail address; a link to anything else is shown as it was written. A rich
message the agent writes in Block Kit follows the same rules, and a mention element in it shows as
its words.

A rich message the agent writes also has nothing to press. It keeps the blocks that show something
(sections and their fields, headers, dividers, context, images and rich text) and leaves out
every button, menu, date or time picker and input, along with any block this app does not know.
Slack sends a press on one of those to this app as an action, the way it sends a press on this
app's own buttons (an approval's among them), by an id whoever wrote the button chose. So the only
buttons on a message are this app's own.

## A thread linked to the dashboard

`!link-to-dashboard` in a thread, or **Link to dashboard** under an answer, brings the thread
into a new chat in PersonalClaw and links the thread to it. A reply in the thread then continues
that chat, with everything said before, and its answer comes back to the thread, after a
PersonalClaw restart too: the chat is this channel's, as every chat a channel opens is, and
PersonalClaw keeps which chat the thread continues. Asked again in the same thread, it keeps the
chat it made rather than making a second one.

## What the agent reads

A message's text as its sender typed it. Slack sends `&`, `<` and `>` in a message as `&amp;`,
`&lt;` and `&gt;`; this app reads those three back as the characters, and decodes nothing else,
for the agent, the channel history it is given, your automations, the Inbox and its digests, a
thread's first message and a thread you link to the dashboard. A mention, a channel or a link
stays in Slack's own spelling (`<@U…>`, `<#C…|name>`, `<https://…|words>`).

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**Slack Channel** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or [install it from a shell](../docs/third-party-install.md#installing-from-a-shell).)

## Settings

| Key | Label | Notes |
|---|---|---|
| `bot_token` | Bot Token | Slack Bot User OAuth Token (xoxb-...). Outbound only needs this one. |
| `app_token` | App Token | Slack App-Level Token for Socket Mode (xapp-...). **Inbound needs both.** |
| `allowed_users` | Allowed Users | Who may talk to the bot, besides the owner: one row per person, with their Slack member ID and a name to know them by. Empty means owner-only; with no owner set either, nobody is authorized. |

Both tokens are **write-only**: once saved, the form shows `••••••••` and the value never
leaves the gateway. Saving other fields keeps the stored tokens; typing a new value
replaces one.

**Inbound follows the tokens.** The gateway starts the Socket-Mode receiver when it turns
the channel on, and a Configure → Save moves it onto the saved tokens at once; the channel
row reads "starting" until Socket Mode is connected. Whenever outbound works and inbound does
not, the row says so, and what starts it, rather than showing a flat green. That includes a
connection Slack dropped later: the Slack SDK reconnects on its own every 10 seconds, and while
it cannot, the row says what Slack answered (`invalid_auth` means the App Token was revoked or
regenerated; save the new one in Configure).

An allowlisted (non-owner) user is authorized for conversation *and* for the commands in
the "any allowed user" tier — `!stop`, `!title`, `!compact` and `sessions`. Everything else
stays owner-only. Add people deliberately. `!dashboard` and `/personalclaw dashboard` DM a
dashboard link to the owner alone: the link signs in as the owner, so anyone else who asks is
told *Only this channel's owner can get a dashboard link…* and is sent nothing.

Approvals are the owner's alone. Only the owner's press on one of a prompt's buttons answers a
tool approval, whether it is asked in a DM or in a linked channel thread
where everyone in the channel sees the buttons. Anyone else's press, an allowlisted user's
included, answers nothing. They are told "Only the owner can answer this", and the press is
logged to the security event log.

### Settings that currently do nothing

Two keys are visible in the Configure form and have no effect. They are listed here rather
than quietly left in place:

- `open_channels` — "all users authorized in this channel" is not enforced; the predicate
  behind it is a hardcoded `false`.
- `allowed_enterprise_ids` — workspace validation accepts any workspace whose bot token
  authenticates; the list does not restrict it.

Making either live changes *who can reach the agent*, so it is a deliberate decision rather
than a bugfix. Until then, `allowed_users` (above) is the allowlist that is enforced.

## The live-writes kill switch

A `chat.postMessage` is a live, outward write a whole workspace sees — and is
notified about — before any undo could run, so this transport honors the platform's
process-wide `PERSONALCLAW_DISABLE_LIVE_WRITES` switch, the same one core applies to
non-GET egress and local-model deletion.

With the switch set, `send()` transmits nothing and returns a **typed refusal**
(`SendRefused`) instead. It is falsy, so every existing "did it send?" caller keeps
reading "not delivered" unchanged, but a caller that cares can tell a suppressed write
from a failed one with `isinstance(result, SendRefused)` — the two demand opposite
responses, and a bare `False` would conflate them.

Parsing follows the platform's fail-safe rule exactly: an **absent** variable allows
writes (the switch is opt-in), an explicit `0`/`false`/`no`/`off` turns the guard off,
and **any other present value — including a typo — turns it on**.

## Slack app setup

1. Go to <https://api.slack.com/apps> → **Create New App** → **From a manifest**,
   and paste `slack-manifest.yaml` (replace `{{USERNAME}}` with your name).
2. Socket Mode: toggle it OFF then back ON to trigger the token-generation
   dialog → add the `connections:write` scope → **Generate** → copy the
   `xapp-...` App-Level Token.
3. **Install to Workspace** and copy the Bot User OAuth Token (`xoxb-...`).
4. Enter both tokens in the app's Configure form (Settings above), or run
   `personalclaw setup` and paste them when prompted.

The first person to DM the bot is auto-claimed as the owner, and approvals go to them from that
message on, with no restart. Or `personalclaw setup` asks for your Slack member id. Either way it is stored as Slack's own owner, `PERSONALCLAW_OWNER_ID_SLACK`,
so setting up another channel leaves it alone. An install set up by an earlier release kept the
owner under the shared `PERSONALCLAW_OWNER_ID`; the first time Slack starts, it copies that owner
to its own key, so the bot keeps its owner rather than waiting for a first sender to claim it. Use
`/personalclaw @user` to allowlist more users and `/personalclaw #channel` to
track a channel.

## Network

Reaches Slack only: its Web API at `slack.com`, the Socket Mode WebSocket that API opens, and the private links Slack sends for shared files.

## License

MIT — see `LICENSE`.
