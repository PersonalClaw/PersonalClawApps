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
- `test_embedding_models.py` — proves every embedding model offered embeds, each with the request and
  answer AWS documents for it, against a loopback Bedrock that holds each request to its model's schema.

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
| `region` | AWS Region | The AWS region every call this instance makes goes to: chat, embeddings, images, video, speech-to-text and the model list. Empty uses us-east-1, whatever your AWS profile or environment names. Credentials come from your AWS environment / profile — no key is stored here. |
| `profile` | AWS Profile | Optional named profile from ~/.aws. Empty uses the default credential chain (env / SSO / instance role). Add a second instance with a different profile to use another AWS account. |
| `default_model` | Default Chat Model | A Bedrock chat model id (full versioned id). The model this instance answers with when nothing in Settings → Models names one. Leave it empty to choose its models in Settings → Models. |
| `system_prompt` | System Prompt | Optional system prompt prepended to every chat turn. |
| `max_tokens` | Max Output Tokens | The most tokens Bedrock may generate in one turn. 0 sends the budget PersonalClaw sets for that call, else 8192. A cap is always sent, because Bedrock's own default can be low enough to cut long answers and tool calls short. |
| `video_s3_bucket` | S3 Bucket | S3 bucket Bedrock's media features stage files in: Nova Reel writes each generated video there, and speech-to-text uploads each recording there for Amazon Transcribe, deleting it afterwards. Required for video generation and speech-to-text; nothing else uses it. Falls back to the BEDROCK_VIDEO_S3_BUCKET env var. |

## The model list

Settings → Models lists the models Bedrock's control plane says the instance's region serves:
each foundation model callable by its own id, and each inference profile (`us.…`, `global.…`).
Each is offered for what Bedrock's record of the model says it does. A model that reads and writes
text and streams its answer is a chat model, and an image or audio one too when it reads images or
speech; a model that writes embeddings is an embedding model when this app knows how to call it
([Embeddings](#embeddings)). A profile is offered for what the model it routes to does. Rerank, image-editing and video-analysis models are not listed, nor is
Nova Sonic, which takes only a two-way audio stream: nothing here can use them. Image and video
generation list their own models (Nova Canvas, Nova Reel).

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

## Embeddings

Each embedding model is called its own way, with the request and answer AWS documents for it:

| Model | Id | Width |
|---|---|---|
| Titan Embeddings G1 - Text | `amazon.titan-embed-text-v1` | 1,536 |
| Titan Text Embeddings V2 | `amazon.titan-embed-text-v2:0` | 1,024 |
| Titan Multimodal Embeddings G1 (text) | `amazon.titan-embed-image-v1` | 1,024 |
| Cohere Embed English v3, Embed Multilingual v3 | `cohere.embed-english-v3`, `cohere.embed-multilingual-v3` | 1,024 |
| Cohere Embed v4 | through an inference profile: `us.cohere.embed-v4:0`, `global.cohere.embed-v4:0`, … | 1,536 |
| Nova Multimodal Embeddings (text) | `amazon.nova-2-multimodal-embeddings-v1:0` | 1,024 |

Settings → Models offers only these for Embedding, those of them the instance's region lists. Any other
model, such as `amazon.titan-embed-g1-text-02`, which Bedrock lists without a documented request, is
not offered, and a binding to one is refused with a sentence before anything is sent. Cohere Embed v3
takes at most 2,048 characters and Nova 8,192, so a longer text is embedded from its start, as each
model does with a text past its token limit. When AWS names two models alike, each is shown with its
id after its name.

## Authentication

Uses your ambient AWS credential chain (environment variables, `~/.aws` profile, SSO). PersonalClaw stores no AWS key for this provider — configure the region/profile in the provider settings.

When that chain can't sign in — no credentials found, a profile that isn't in your AWS config, a `credential_process` command that fails, an SSO sign-in that has expired, a region name that isn't one, no connection to AWS — the chat, the connection test and the Models page say which it is and what to do (sign in with your AWS tool, fix the profile or region, or choose another profile), with the AWS SDK's own message after that.

Speech-to-text, embeddings, images and video say the same, and name what else stops them: no S3 Bucket (speech-to-text and video need one), a bucket that doesn't exist, an IAM action the identity lacks, or a Transcribe or Nova Reel job that failed, with its reason and the next step. A Nova Reel job that runs past ten minutes may still finish, so the message names the S3 folder its video will land in. When speech-to-text is unavailable, the composer's microphone says why; when an embedding model can't embed, a re-index refused on it does.

## Network

Reaches AWS in the region you set in **AWS Region**: Amazon Bedrock, plus Amazon S3 and Amazon Transcribe for the features that use them.

## License

MIT — see the apps repo [LICENSE](../LICENSE).
