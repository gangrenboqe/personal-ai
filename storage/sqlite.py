"""SQLite 实现。标准库，无第三方依赖。"""

from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    provider    TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    provider    TEXT NOT NULL DEFAULT '',
    model       TEXT NOT NULL DEFAULT '',
    chain       TEXT NOT NULL DEFAULT '',
    ok          INTEGER NOT NULL DEFAULT 1,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'todo',
    note        TEXT NOT NULL DEFAULT '',
    due_at      REAL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session
    ON messages(session_id, id);

CREATE INDEX IF NOT EXISTS idx_tasks_status
    ON tasks(status, created_at);
"""


def _connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str) -> str:
    with _connect(db_path) as conn:
        conn.executescript(SCHEMA)
    return db_path


def create_session(db_path: str, title: str = "", provider: str = "") -> str:
    sid = uuid.uuid4().hex[:16]
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO sessions (id, title, provider, created_at) VALUES (?, ?, ?, ?)",
            (sid, title or "新会话", provider, time.time()),
        )
    return sid


def add_message(
    db_path: str,
    session_id: str,
    role: str,
    content: str,
    provider: str = "",
    model: str = "",
    chain: str = "",
    ok: bool = True,
) -> int:
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO messages"
            " (session_id, role, content, provider, model, chain, ok, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, role, content, provider, model, chain, int(ok), time.time()),
        )
        return int(cur.lastrowid or 0)


def list_messages(db_path: str, session_id: str, limit: int = 200) -> list[dict]:
    """取该会话的消息，**按时间正序**返回。

    注意：`limit` 取的是**最近** limit 条（不是最早 limit 条）。
    拼模型上下文时必须用最近的 —— 否则对话变长后会拿着开头的老消息，
    而用户刚说的那句话根本不在里面。这个 bug 是被单元测试抓出来的。
    """
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, role, content, provider, model, chain, ok, created_at"
            " FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "role": r["role"],
            "content": r["content"],
            "provider": r["provider"],
            "model": r["model"],
            "chain": r["chain"],
            "ok": bool(r["ok"]),
            "created_at": r["created_at"],
        }
        for r in reversed(rows)
    ]


def list_sessions(db_path: str, limit: int = 50) -> list[dict]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT s.id, s.title, s.provider, s.created_at,"
            " COUNT(m.id) AS msg_count"
            " FROM sessions s LEFT JOIN messages m ON m.session_id = s.id"
            " GROUP BY s.id ORDER BY s.created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "provider": r["provider"],
            "created_at": r["created_at"],
            "msg_count": r["msg_count"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------
# 未来区：tasks
# ---------------------------------------------------------------

VALID_STATUS = ("todo", "doing", "done")


def create_task(
    db_path: str,
    title: str,
    note: str = "",
    status: str = "todo",
    due_at: float | None = None,
) -> str:
    if status not in VALID_STATUS:
        status = "todo"
    tid = uuid.uuid4().hex[:16]
    now = time.time()
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, status, note, due_at, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (tid, title, status, note, due_at, now, now),
        )
    return tid


def list_tasks(db_path: str, status: str | None = None, limit: int = 200) -> list[dict]:
    sql = "SELECT id, title, status, note, due_at, created_at, updated_at FROM tasks"
    args: list = []
    if status in VALID_STATUS:
        sql += " WHERE status = ?"
        args.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    with _connect(db_path) as conn:
        rows = conn.execute(sql, args).fetchall()
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "status": r["status"],
            "note": r["note"],
            "due_at": r["due_at"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


def update_task(db_path: str, task_id: str, **fields) -> bool:
    allowed = {"title", "status", "note", "due_at"}
    patch = {k: v for k, v in fields.items() if k in allowed}
    if not patch:
        return False
    if "status" in patch and patch["status"] not in VALID_STATUS:
        del patch["status"]
    if not patch:
        return False
    patch["updated_at"] = time.time()
    cols = ", ".join(f"{k} = ?" for k in patch)
    args = list(patch.values()) + [task_id]
    with _connect(db_path) as conn:
        cur = conn.execute(f"UPDATE tasks SET {cols} WHERE id = ?", args)
        return cur.rowcount > 0


def delete_task(db_path: str, task_id: str) -> bool:
    with _connect(db_path) as conn:
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return cur.rowcount > 0


def delete_session(db_path: str, session_id: str) -> bool:
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        cur = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        return cur.rowcount > 0
