# Amazon Bedrock

Amazon Bedrock chat models via the Converse API. Authentication uses your AWS environment / named profile — no key is stored by PersonalClaw.

**Amazon Bedrock** is a **model provider** — it registers Amazon Bedrock chat models (Converse API) under Settings → Models; auth comes from your AWS environment/profile, no key is stored.

## What this is

A standalone PersonalClaw app bundle (part of the core/app workspace split). It ships
as a self-contained directory:

- `app.json` — the manifest (identity, provider/backend/UI declarations, permissions).
- `provider.py` — the implementation, exposed via `create_provider`.
- `test_access_and_credentials.py`, `test_catalog.py`, `test_media_names_its_model.py`,
  `test_media_says_why.py`, `test_provider.py`, `test_stream_timeout.py` — the app's own tests.
- `test_wire.py` — proves a call's per-call temperature and output budget reach the request, against
  a recording endpoint.

It imports only the PersonalClaw **SDK** (never core internals), so core can evolve
without breaking it:

- `personalclaw.sdk.model` — chat, the model catalog, and the media scanners
- `personalclaw.sdk.embedding`, `personalclaw.sdk.image`, `personalclaw.sdk.video`,
  `personalclaw.sdk.stt` — the media adapters
- `personalclaw.sdk.net` — a failure's sentence with the AWS SDK's own words after it

## Install

From the App Store, add the `apps/` directory as a **local source**, then install
**Amazon Bedrock** — the install runs through the security scanner and lifecycle exactly like
any other app. (Or [install it from a shell](../docs/third-party-install.md#installing-from-a-shell).)

## Settings

| Key | Label | Notes |
|---|---|---|
| `region` | AWS Region | Bedrock region (e.g. us-west-2). Credentials come from your AWS environment / profile — no key is stored here. |
| `default_model` | Default Chat Model | A Bedrock chat model id (full versioned id). The model this instance answers with when nothing in Settings → Models names one. Leave it empty to choose its models in Settings → Models. |
| `profile` | AWS Profile | Optional named profile from ~/.aws. Empty uses the default credential chain (env / SSO / instance role). |
| `system_prompt` | System Prompt | Optional system prompt prepended to every turn. |
| `video_s3_bucket` | S3 Bucket | Where Nova Reel writes each generated video, and where speech-to-text uploads each recording for Amazon Transcribe (deleting it afterwards). Required for video generation and speech-to-text; nothing else uses it. Falls back to the `BEDROCK_VIDEO_S3_BUCKET` env var. |

## Prompt caching

Bedrock's Converse API needs an explicit cache **checkpoint** — it does not cache a prompt
prefix on its own — so this app declares the `explicit` posture and does the translation
itself: PersonalClaw marks one message as the end of the stable prompt prefix, and this app
turns that mark into Converse's `cachePoint` content block. PersonalClaw's core never learns
Bedrock's syntax.

What you get from the second turn of a conversation onward: the stable part of the prompt is
read from Bedrock's cache instead of re-processed, which is both cheaper and faster. Bedrock
reports it as `cacheReadInputTokens`, which this app surfaces as the turn's cache-read token
count, so savings show up in the usual usage view rather than needing a Bedrock-specific one.

Turn it off globally with **Settings → Agent → Prompt Caching**; there is nothing to
configure per-model. Caching is best-effort on Bedrock's side and needs a prompt long enough
to be worth caching, so short turns may report no reads at all — that is normal.

## Authentication

Uses your ambient AWS credential chain (environment variables, `~/.aws` profile, SSO). PersonalClaw stores no AWS key for this provider — configure the region/profile in the provider settings.

When that chain can't sign in — no credentials found, a profile that isn't in your AWS config, a `credential_process` command that fails, an SSO sign-in that has expired, a region name that isn't one, no connection to AWS — the chat, the connection test and the Models page say which it is and what to do (sign in with your AWS tool, fix the profile or region, or choose another profile), with the AWS SDK's own message after that.

Speech-to-text, embeddings, images and video say the same, and name what else stops them: no S3 Bucket (speech-to-text and video need one), a bucket that doesn't exist, an IAM action the identity lacks, or a Transcribe or Nova Reel job that failed, with its reason and the next step. A Nova Reel job that runs past ten minutes may still finish, so the message names the S3 folder its video will land in. When speech-to-text is unavailable, the composer's microphone says why; when an embedding model can't embed, a re-index refused on it does.

## License

MIT — see the apps repo [LICENSE](../LICENSE).
