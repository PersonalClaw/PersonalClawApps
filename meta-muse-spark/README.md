# Meta Muse Spark

Chat models via the [Meta AI API](https://dev.meta.ai/docs/getting-started/overview/).

## Setup

1. Get an API key from the Meta AI developer portal
2. Install this app from the Store
3. Go to Settings → Providers → Add instance → select "Meta Muse Spark"
4. Enter your API key
5. Bind `muse-spark-1.1` to the Chat use case in Settings → Models

## Environment variable

Set `META_MODEL_API_KEY` in your `.env` to auto-configure.

## Models

| Model | Context | Capabilities |
|-------|---------|-------------|
| muse-spark-1.1 | 1,048,576 tokens | Chat, Vision (image/pdf/video input), Streaming |

## API compatibility

The Meta AI API is OpenAI-compatible. This provider uses the OpenAI SDK client
with `base_url=https://api.meta.ai/v1`.
