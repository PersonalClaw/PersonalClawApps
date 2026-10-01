# Alibaba Model Studio

Alibaba Cloud Model Studio (DashScope) provider for PersonalClaw.

## Capabilities

- **Chat** — Qwen family models via OpenAI-compatible endpoint
- **Image input** — the Qwen-VL, QVQ and Qwen-Omni models are sent an attached image as the
  image itself (`takes_images` in `provider.py` names the ones whose ids don't say so); every
  other model gets the image's text instead
- **Embedding** — text-embedding-v3, text-embedding-v2
- **Image Generation** — qwen-image-2.0, qwen-image-2.0-pro, wan2.7-image, wan2.7-image-pro

Model Studio's model list names each model and says nothing about what it does, so Settings →
Models offers each for what its id says. Its speech models (ASR such as `qwen3-asr-flash`,
speech synthesis such as `qwen-tts` and `cosyvoice-v2`), its realtime models, which take a live
session, and its rerankers (`gte-rerank-v2`) are offered for nothing: this app drives none of
them.

## Configuration

Set your API key via the `ALIBABA_API_KEY` environment variable or in the app settings.

| Key | Label | Notes |
|---|---|---|
| `api_key` | API Key | Your Alibaba Model Studio API key. Falls back to ALIBABA_API_KEY env var. |
| `default_model` | Default Model | A Model Studio model id, such as qwen-plus. The model this instance answers with when nothing in Settings → Models names one. Leave it empty to choose its models in Settings → Models. |
| `endpoint` | Endpoint | Regional API endpoint (below). |

### Regional Endpoints

Select your endpoint in the app settings dropdown:

| Endpoint | URL |
|----------|-----|
| Singapore (Token Plan) | `https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1` |
| Legacy International | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` |
| Legacy China | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| Virginia | `https://dashscope-us.aliyuncs.com/compatible-mode/v1` |

For workspace-based access, enter your workspace URL manually:
- Beijing: `https://{workspace_id}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`
- Singapore: `https://{workspace_id}.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1`
- Tokyo: `https://{workspace_id}.ap-northeast-1.maas.aliyuncs.com/compatible-mode/v1`

## Network

Reaches the Model Studio endpoint you set in **Endpoint** (by default `dashscope-intl.aliyuncs.com`).
