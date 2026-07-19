# Alibaba Model Studio

Alibaba Cloud Model Studio (DashScope) provider for PersonalClaw.

## Capabilities

- **Chat** — Qwen family models via OpenAI-compatible endpoint
- **Embedding** — text-embedding-v3, text-embedding-v2
- **Image Generation** — qwen-image-2.0, qwen-image-2.0-pro, wan2.7-image, wan2.7-image-pro

## Configuration

Set your API key via the `ALIBABA_API_KEY` environment variable or in the app settings.

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
