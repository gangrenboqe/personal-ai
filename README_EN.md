# Personal AI — English Overview

> Short English summary. The full (Chinese) README is [here](README.md).

A personal AI workspace with a time-axis UI: **Past · Now · Future**.
My history, memory, tasks and context belong to me — not to any model vendor.
Model / Agent / Provider are three replaceable layers.

## What it does

- **Chat with a real LLM** through a pluggable provider registry (mock / OpenAI-compatible included)
- **SSE streaming** — replies render chunk by chunk
- **Source transparency** — every reply stores and shows its full call chain
  (e.g. `openai-compat → 127.0.0.1:20128 → deepseek-ai/DeepSeek-V3.2`)
- **Task panel** — a "Future" zone where plans live before they happen
- **Single-file SQLite storage** — sessions / messages / tasks all in one file I own

## Measured, not claimed

| Item | Result |
|---|---|
| Automated smoke tests | **32 / 32 passing** (API + SSE + persistence) |
| Real model call | DeepSeek-V3.2, HTTP 200, **independently logged by the local gateway** |
| Streaming, warm start | first chunk ≈ 1.6 s, 16 chunks |
| Streaming, cold start | first chunk ≈ 11.7 s (upstream cold boot) |
| Timeout / retry | 180 s, 2 retries on timeout / 5xx / 429 |

## Quick start

```bash
python -m personal_ai --serve          # offline mode (mock provider)
# open http://127.0.0.1:8787
```

Real model through a local OpenAI-compatible gateway:

```bash
set PAI_PROVIDER=openai_compat
set PAI_BASE_URL=http://127.0.0.1:20128/v1
set PAI_API_KEY=sk-***
set PAI_MODEL=deepseek-ai/DeepSeek-V3.2
python scripts/start_local.py
```

## Architecture

```
personal_ai/
  server/     stdlib http.server — REST API + SSE endpoint
  core/       chat service, task service
  providers/  provider registry (add one file = add one vendor)
  storage/    SQLite: sessions / messages / tasks
  web/        static frontend — time-axis UI, streaming rendering
scripts/      smoke test (32 checks) + one-click launcher
```

## Design principles

1. **Zero third-party dependencies.** Python stdlib only (`http.server`, `sqlite3`,
   `urllib`). It runs wherever Python runs, and nothing breaks on another machine.
2. **Source transparency.** An answer without its origin is not trustworthy; every
   reply keeps provider / model / chain, and the UI shows the chain per reply.
3. **Replaceable providers.** Registry pattern — swapping vendors is one file.
4. **Honest documentation.** Numbers here are measured; anything unverified is
   marked as such instead of being rounded up into a claim.

## Repo scope

This repository intentionally contains only code and docs:
`README.md` / `README_EN.md` / `personal_ai/` / `scripts/` / `.gitignore`.
No personal data, no private notes.
