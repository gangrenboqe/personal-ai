# Personal AI — English

> **This file has been superseded.** The full English documentation now lives in
> **[README.md](README.md)**, which is the primary README of this repository.
>
> 中文完整版: **[README.zh-CN.md](README.zh-CN.md)**

---

## What this project is (short version)

A personal AI entry point that runs on your own machine:

- **Zero third-party dependencies** — Python standard library only (`http.server`, `sqlite3`, `urllib`)
- **Pluggable model providers** — adding a vendor means adding one subclass; business code unchanged
- **Your data stays yours** — sessions, messages and tasks in a single portable SQLite file
- **Source transparency** — every reply stores and displays its real call chain, not just a model name
- **Streaming (SSE)** — replies render chunk by chunk
- **Two test layers in CI** — 31 unit tests + 32 smoke tests, across Ubuntu/Windows × Python 3.11/3.13

See **[README.md](README.md)** for architecture, API reference, measured evidence and known gaps.
