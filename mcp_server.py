"""personal-ai 记忆层 MCP Server（39 号工单）。

把现有记忆层以 MCP（Model Context Protocol，2024-11-05 版）暴露出去：
JSON-RPC 2.0 over stdio，零第三方依赖（标准库手写，不用官方 SDK）。

协议规矩（最容易踩的）：
- 每条消息一行 JSON，以 \\n 结尾；stdout 只准出 JSON-RPC 消息，调试一律走 stderr。
- 每条响应写完必须 flush，否则客户端收不到。

暴露的工具（复用 MemoryToolExecutor，不改记忆层存储/检索逻辑）：
    memory_create / memory_search / memory_list / history_search
工具 description 原样复用 memory_tools_schema()（含 kind 判据与排除规则）。

运行：python mcp_server.py        （由 MCP 客户端拉起，不需要人工交互）
配置：PAI_DB_PATH 指定 SQLite 路径，缺省 <仓根>/data/personal_ai.db
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from personal_ai.memory import (  # noqa: E402
    MemoryStore,
    MemoryToolExecutor,
    ensure_tables,
    memory_tools_schema,
)

PROTOCOL_VERSION = "2024-11-05"

# memory_list 的参数说明（schema 里没有它，这里补一句；其余工具 description 全部复用 schema 原文）
MEMORY_LIST_TOOL = {
    "name": "memory_list",
    "description": "列出记忆层里的记忆条目，可按 kind 过滤（fact/preference/conclusion/skill），默认最新在前。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "description": "按类型过滤，可选"},
            "limit": {"type": "integer", "description": "最多返回几条，默认 20"},
        },
    },
}

# 从 OpenAI function 格式转成 MCP 工具格式：description 与 parameters 一个字不改
_SCHEMA_TOOLS = {t["function"]["name"]: t["function"] for t in memory_tools_schema()}
MCP_TOOLS = [
    {
        "name": name,
        "description": _SCHEMA_TOOLS[name]["description"],
        "inputSchema": _SCHEMA_TOOLS[name]["parameters"],
    }
    for name in ("memory_create", "memory_search")
] + [MEMORY_LIST_TOOL] + [
    {
        "name": "history_search",
        "description": _SCHEMA_TOOLS["history_search"]["description"],
        "inputSchema": _SCHEMA_TOOLS["history_search"]["parameters"],
    }
]


def _stderr(msg: str) -> None:
    print(f"[mcp_server] {msg}", file=sys.stderr, flush=True)


def _rpc_result(req_id, result: dict) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}, ensure_ascii=False)


def _rpc_error(req_id, code: int, message: str) -> str:
    return json.dumps(
        {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}},
        ensure_ascii=False,
    )


def make_handler(executor: MemoryToolExecutor):
    """构造 dispatch(req) -> str | None（None 表示通知类消息，不回复）。"""

    def dispatch(req: dict):  # noqa: ANN202
        method = str(req.get("method") or "")
        req_id = req.get("id")
        params = req.get("params") if isinstance(req.get("params"), dict) else {}

        if method.startswith("notifications/"):
            return None  # 通知类：不回（如 notifications/initialized）

        if method == "initialize":
            return _rpc_result(req_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "personal-ai-memory", "version": "1.0.0"},
            })

        if method == "tools/list":
            return _rpc_result(req_id, {"tools": MCP_TOOLS})

        if method == "tools/call":
            name = str(params.get("name") or "")
            arguments = params.get("arguments")
            if not name:
                return _rpc_error(req_id, -32602, "tools/call 缺少 name")
            if not isinstance(arguments, dict):
                arguments = {}
            if name not in {t["name"] for t in MCP_TOOLS}:
                return _rpc_error(req_id, -32602, f"未知工具: {name}")
            result = executor.execute(name, arguments)  # 记忆层约定：错误回 {"ok": False}，不抛
            return _rpc_result(req_id, {
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                "isError": not bool(result.get("ok", True)),
            })

        return _rpc_error(req_id, -32601, f"Method not found: {method}")

    return dispatch


def main() -> int:
    db_path = os.environ.get("PAI_DB_PATH", "").strip() or str(ROOT / "data" / "personal_ai.db")
    report = ensure_tables(db_path)
    _stderr(f"db ready: {report['db']} (memories={report['memories_rows']} rows)")
    executor = MemoryToolExecutor(MemoryStore(db_path))
    dispatch = make_handler(executor)

    while True:
        line = sys.stdin.readline()
        if not line:
            break  # 客户端断开
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            # 无 id 可回的坏帧：JSON-RPC 允许 id=null 的错误响应
            sys.stdout.write(_rpc_error(None, -32700, f"Parse error: {e}") + "\n")
            sys.stdout.flush()
            continue
        try:
            resp = dispatch(req)
        except Exception as e:  # 兜底：任何意外都不崩进程、不污染 stdout
            req_id = req.get("id") if isinstance(req, dict) else None
            resp = _rpc_error(req_id, -32603, f"Internal error: {e}") if req_id is not None else None
            _stderr(f"dispatch error: {e!r}")
        if resp is not None:
            sys.stdout.write(resp + "\n")
            sys.stdout.flush()
    _stderr("stdin closed, exit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
