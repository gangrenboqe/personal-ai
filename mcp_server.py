"""personal-ai 记忆层 MCP Server（39 号创建，40 号完整化）。

把现有记忆层以 MCP（Model Context Protocol，2024-11-05 版）暴露出去：
JSON-RPC 2.0 over stdio，零第三方依赖（标准库手写，不用官方 SDK）。

协议规矩（最容易踩的）：
- 每条消息一行 JSON，以 \\n 结尾；stdout 只准出 JSON-RPC 消息，调试一律走 stderr。
- 每条响应写完必须 flush，否则客户端收不到。

能力：
- tools：12 个（memory_create/search/list/view/update/delete/rename/insert/stats/export/import + history_search）
- resources：memories://<path> —— 客户端可像浏览文件树一样直接翻记忆库，不用先搜
- prompts：3 个预设模板（回忆上周 / 我的偏好 / 决定溯源）

工具 description 原样复用 memory_tools_schema()（含 kind 判据与排除规则）；
新增工具的说明为本文件新写。记忆层存储/检索逻辑零改动，全部组合 MemoryStore 现有方法。

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
RESOURCE_SCHEME = "memories://"

# ---- 工具定义：有 schema 的复用原文，新增的在这里补齐（inputSchema = JSON Schema） ----

_SCHEMA_TOOLS = {t["function"]["name"]: t["function"] for t in memory_tools_schema()}


def _from_schema(name: str) -> dict:
    return {
        "name": name,
        "description": _SCHEMA_TOOLS[name]["description"],
        "inputSchema": _SCHEMA_TOOLS[name]["parameters"],
    }


_LIST_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "description": "按类型过滤：fact/preference/conclusion/skill，可选"},
        "since": {"type": "string", "description": "只返回此时间之后写入的（ISO8601，如 2026-11-01），可选"},
        "until": {"type": "string", "description": "只返回此时间之前写入的（ISO8601），可选"},
        "limit": {"type": "integer", "description": "最多返回几条，默认 20"},
    },
}

_NEW_TOOLS = {
    "memory_view": {
        "name": "memory_view",
        "description": "按 path 查看单条记忆的完整内容（含来源、写入时间、是否核过）。",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "记忆路径，如 /memories/fact/xxx.md"}},
            "required": ["path"],
        },
    },
    "memory_update": {
        "name": "memory_update",
        "description": "精确编辑某条记忆：把 old 文本替换为 new 文本（全文检索匹配，FTS 同步更新）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "记忆路径"},
                "old": {"type": "string", "description": "要替换的原文（必须唯一匹配）"},
                "new": {"type": "string", "description": "替换后的文本"},
            },
            "required": ["path", "old", "new"],
        },
    },
    "memory_delete": {
        "name": "memory_delete",
        "description": "软删一条记忆：写 tombstone 标记，不物理删除（可追溯）。删除后 search/list 不再返回它。",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "记忆路径"}},
            "required": ["path"],
        },
    },
    "memory_rename": {
        "name": "memory_rename",
        "description": "重命名（移动）一条记忆的路径。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "old": {"type": "string", "description": "旧路径"},
                "new": {"type": "string", "description": "新路径"},
            },
            "required": ["old", "new"],
        },
    },
    "memory_insert": {
        "name": "memory_insert",
        "description": "往某条记忆的指定行之后插入一段文本（after 省略则追加到末尾）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "记忆路径"},
                "text": {"type": "string", "description": "要插入的文本"},
                "after": {"type": "integer", "description": "插在第几行之后（0 表示最前），省略则追加末尾"},
            },
            "required": ["path", "text"],
        },
    },
    "memory_stats": {
        "name": "memory_stats",
        "description": "按 kind 统计记忆条数（fact/preference/conclusion/skill 各多少条、最新一条何时写入）。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    "memory_export": {
        "name": "memory_export",
        "description": "导出全部活记忆为 JSON（path/kind/content/source/written_at/verified_at），用于备份或迁移。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    "memory_import": {
        "name": "memory_import",
        "description": "批量导入记忆，items 为对象数组，每项含 kind/title/content；来源标记为「批量导入」。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": "记忆对象数组",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string"},
                            "title": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["kind", "title", "content"],
                    },
                }
            },
            "required": ["items"],
        },
    },
    "memory_list": {
        "name": "memory_list",
        "description": "列出记忆条目，可按 kind 过滤、按写入时间范围过滤（since/until），默认最新在前。",
        "inputSchema": _LIST_SCHEMA,
    },
}

MCP_TOOLS = (
    [_from_schema("memory_create"), _from_schema("memory_search"), _NEW_TOOLS["memory_list"],
     _NEW_TOOLS["memory_view"], _NEW_TOOLS["memory_update"], _NEW_TOOLS["memory_delete"],
     _NEW_TOOLS["memory_rename"], _NEW_TOOLS["memory_insert"], _NEW_TOOLS["memory_stats"],
     _NEW_TOOLS["memory_export"], _NEW_TOOLS["memory_import"], _from_schema("history_search")]
)
_TOOL_NAMES = {t["name"] for t in MCP_TOOLS}

# ---- prompts：三个预设模板（MCP prompts/get 返回 messages 给客户端展开成对话） ----

PROMPTS = [
    {
        "name": "recall_last_week",
        "description": "回忆最近一周做了什么：列出并按天归拢这段时间的记忆与对话。",
        "arguments": [{"name": "days", "description": "回看几天，默认 7", "required": False}],
        "_build": lambda a: (
            f"请帮我回忆最近 {int(a.get('days') or 7)} 天做了什么。"
            f"先调用 memory_list 工具（参数 since 用 {int(a.get('days') or 7)} 天前的日期，ISO8601 格式）"
            "拿这段时间的记忆条目，必要时用 history_search 搜具体话题的对话；"
            "然后按天归拢给我一个时间线，每条带来源（记忆里写的 source 和时间）。查不到就直说这段时间没有记录。"
        ),
    },
    {
        "name": "my_preferences",
        "description": "我有什么偏好：汇总记忆库里所有 preference 类的条目。",
        "arguments": [],
        "_build": lambda a: (
            "请汇总我的所有偏好。先调用 memory_list（参数 kind=preference）拿全部偏好类记忆，"
            "按主题归组（代码风格 / 沟通方式 / 工作习惯等）简洁列给我，每条注明写入时间。"
            "如果 preference 一条都没有，直说记忆库里还没有偏好记录。"
        ),
    },
    {
        "name": "decision_trace",
        "description": "这个决定我之前怎么定的：在 conclusion 类记忆与历史对话里溯源某个决定。",
        "arguments": [{"name": "topic", "description": "决定的主题关键词，如「数据库选型」", "required": True}],
        "_build": lambda a: (
            f"请帮我溯源「{a.get('topic') or '（主题待填）'}」这个决定当时是怎么定的。"
            "先调用 memory_search（query 用主题关键词）找 conclusion 类记忆；"
            "再用 history_search 搜当时的讨论原文；"
            "然后告诉我：决定是什么、当时的理由、有没有相关的其他决定，每条带出处和时间。两处都查不到就直说没有记录。"
        ),
    },
]

PROMPT_NAMES = {p["name"] for p in PROMPTS}


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
    store = executor.store

    def _resource_uri(path: str) -> str:
        return RESOURCE_SCHEME + path.lstrip("/")

    def _read_resource(uri: str) -> dict | None:
        if not uri.startswith(RESOURCE_SCHEME):
            return None
        path = "/" + uri[len(RESOURCE_SCHEME):]
        try:
            rows = store.view(path)  # 精确路径 → 单条全文；路径不存在时 store 抛 MemoryCommandError
        except Exception:
            return None
        if not rows:
            return None
        row = rows[0]
        header = f"# {row.get('path', path)}\n\nkind: {row.get('kind')} · source: {row.get('source')} · written_at: {row.get('written_at')}\n\n"
        return {
            "uri": uri,
            "mimeType": "text/markdown",
            "text": header + (row.get("content") or ""),
        }

    def dispatch(req: dict):  # noqa: ANN202
        method = str(req.get("method") or "")
        req_id = req.get("id")
        params = req.get("params") if isinstance(req.get("params"), dict) else {}

        if method.startswith("notifications/"):
            return None  # 通知类：不回（如 notifications/initialized）

        if method == "initialize":
            return _rpc_result(req_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                "serverInfo": {"name": "personal-ai-memory", "version": "2.0.0"},
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
            if name not in _TOOL_NAMES:
                return _rpc_error(req_id, -32602, f"未知工具: {name}")
            result = executor.execute(name, arguments)  # 记忆层约定：错误回 {"ok": False}，不抛
            return _rpc_result(req_id, {
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                "isError": not bool(result.get("ok", True)),
            })

        if method == "resources/list":
            with _connect_rows(store) as rows:
                resources = [
                    {
                        "uri": _resource_uri(r["path"]),
                        "name": r["path"].rsplit("/", 1)[-1],
                        "mimeType": "text/markdown",
                        "description": f"[{r['kind']}] 写入 {r['written_at']}",
                    }
                    for r in rows
                ]
            return _rpc_result(req_id, {"resources": resources})

        if method == "resources/read":
            uri = str(params.get("uri") or "")
            content = _read_resource(uri)
            if content is None:
                return _rpc_error(req_id, -32602, f"找不到资源: {uri or '(空)'}")
            return _rpc_result(req_id, {"contents": [content]})

        if method == "prompts/list":
            return _rpc_result(req_id, {"prompts": [
                {k: p[k] for k in ("name", "description", "arguments")} for p in PROMPTS
            ]})

        if method == "prompts/get":
            name = str(params.get("name") or "")
            if name not in PROMPT_NAMES:
                return _rpc_error(req_id, -32602, f"未知 prompt: {name or '(空)'}")
            p = next(p for p in PROMPTS if p["name"] == name)
            args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            text = p["_build"](args)
            return _rpc_result(req_id, {
                "description": p["description"],
                "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
            })

        return _rpc_error(req_id, -32601, f"Method not found: {method}")

    return dispatch


def _connect_rows(store: MemoryStore):
    """resources/list 用：返回全部活记忆的 (path, kind, written_at)。"""
    from personal_ai.memory import _connect

    class _Rows:
        def __enter__(self):
            self.conn = _connect(store.db_path)
            return [
                dict(r) for r in self.conn.execute(
                    "SELECT path, kind, written_at FROM memories"
                    " WHERE deleted_at IS NULL ORDER BY kind, path"
                ).fetchall()
            ]

        def __exit__(self, *exc):
            self.conn.close()
            return False

    return _Rows()


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
