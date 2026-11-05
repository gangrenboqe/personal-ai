"""记忆层：Anthropic memory tool 六命令 + FTS5 检索 + 上下文注入块。

施工单 29（2026-10-29）落地。硬规矩：
- 零第三方依赖：只用标准库（sqlite3 自带 FTS5，不算第三方）。
- 不做 embedding / 不做向量库。
- 不物理删除：delete 写 deleted_at（tombstone），旧条永远可查。
- 每条必有 source + written_at；verified_at 允许为空，且能查出"从未核过"。
- 冲突不裁决：build_context_block 把 source / verified_at / written_at 三元组
  原样交给模型自行权衡，代码里不写"哪个赢"的规则
  （arXiv 2610.36169：知识冲突需要先验，不是规则）。

FTS5 分词器说明：Python 标准库 sqlite3 不支持注册自定义分词器；默认 unicode61
把整段无空格中文当成一个 token，实际查询基本命中不了。因此用 sqlite 内置的
trigram 分词器（>=3.34，本机 3.43.1 实测可用）：中文按 3 字子串命中，2 字词
走 LIKE 兜底（trigram 表上 LIKE 同样走索引）。查询侧对长句做 3 字滑窗拆分。
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path

# ------------------------------------------------------------------ 表结构

MEMORIES_DDL = """
CREATE TABLE IF NOT EXISTS memories (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    path          TEXT NOT NULL UNIQUE,   -- 形如 /memories/preferences/回答风格.md
    content       TEXT NOT NULL,
    kind          TEXT NOT NULL,          -- fact | preference | conclusion | skill
    source        TEXT NOT NULL,          -- 用户说的 / 从对话抽取 / 从文件导入 / 系统推断
    written_at    TEXT NOT NULL,          -- ISO8601
    verified_at   TEXT,                   -- NULL = 从未核过
    expires_hint  TEXT,                   -- 可能过期的提示
    superseded_by INTEGER REFERENCES memories(id),  -- 被哪条新记忆取代
    deleted_at    TEXT                    -- 非空 = tombstone（软删标记），不物理删
)
"""

MEMORIES_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content, path UNINDEXED,
    content='memories', content_rowid='id',
    tokenize='trigram'
)
"""

REQUIRED_COLS = {
    "id", "path", "content", "kind", "source",
    "written_at", "verified_at", "expires_hint", "superseded_by", "deleted_at",
}

VALID_KINDS = ("fact", "preference", "conclusion", "skill")

_SEARCH_COLS = (
    "m.id, m.path, m.content, m.kind, m.source, m.written_at,"
    " m.verified_at, m.expires_hint"
)

# 查询分段的分隔符：空白 + 常见中英文标点
_SEG_SPLIT_RE = re.compile(
    r"[\s，。！？；：、（）【】《》“”‘’…—,.!?;:()<>{}\[\]\"'`~@#$%^&*+=|\\/]+"
)


class MemoryCommandError(Exception):
    """六命令的语义错误（重名 / 不唯一 / 路径不存在等）。"""


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_tables(db_path: str) -> dict:
    """建记忆层两张表（若缺）。幂等，可重复执行。返回报告供打印与测试。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(str(path)) as conn:
        conn.execute(MEMORIES_DDL)
        conn.execute(MEMORIES_FTS_DDL)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()}
        missing = REQUIRED_COLS - cols
        if missing:
            raise RuntimeError(
                f"memories 表缺少列 {sorted(missing)}——库可能是旧结构，请人工检查: {path}"
            )
        n = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        _ensure_history_fts(conn)  # 施工单 38 任务 4：对话历史全文检索（只新增，不改旧表结构）
    return {"db": str(path), "memories_rows": n, "columns": sorted(cols)}


MESSAGES_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content, session_id UNINDEXED,
    content='messages', content_rowid='id',
    tokenize='trigram'
)
"""

# 触发器维护增量；存量回填只在触发器首次创建时做一次（幂等）
MESSAGES_FTS_TRIGGERS = {
    "messages_fts_ai": """
        CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, content, session_id)
            VALUES (new.id, new.content, new.session_id);
        END
    """,
    "messages_fts_ad": """
        CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, content, session_id)
            VALUES ('delete', old.id, old.content, old.session_id);
        END
    """,
}


def _ensure_history_fts(conn: sqlite3.Connection) -> None:
    """对话历史 FTS（施工单 38 任务 4）。messages 表结构不动，只新增虚拟表与触发器。"""
    has_messages = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='messages'"
    ).fetchone()[0]
    if not has_messages:
        # messages 表由 ChatService 的 init_db 创建；独立使用 MemoryStore（如单测临时库）时
        # 先跳过，等 messages 存在后的下一次 ensure_tables 再补触发器与回填。
        return
    existed = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name='messages_fts_ai'"
    ).fetchone()[0]
    conn.execute(MESSAGES_FTS_DDL)
    conn.execute(MESSAGES_FTS_TRIGGERS["messages_fts_ai"])
    conn.execute(MESSAGES_FTS_TRIGGERS["messages_fts_ad"])
    if not existed:
        # 存量回填（一次性；external content 表的行内容实时取自 messages，索引条目在此补齐）
        conn.execute(
            "INSERT INTO messages_fts(rowid, content, session_id)"
            " SELECT id, content, session_id FROM messages"
        )


# ------------------------------------------------------------------ 注入块

# 注入限长（返工单 30）：记忆条目是精炼事实，注入给完整内容，但要有上限防失控。
# 每条按字符限长（英文按词边界回退），整块再兜底一刀。
ENTRY_MAX_CHARS = 200
BLOCK_MAX_CHARS = 1500


def _clip_entry(text: str, max_chars: int = ENTRY_MAX_CHARS) -> str:
    """注入用限长：中文直接截，英文按最后一个空格回退（不切断单词）。"""
    if len(text) <= max_chars:
        return text
    clipped = text[:max_chars]
    m = re.search(r"\s\S*$", clipped)
    if m is not None:
        # 尾部是英文单词的碎片：回退到词首（碎片本身很长说明是中文长段，不回退）
        if len(clipped) - m.start() <= 30:
            clipped = clipped[: m.start()]
    return clipped + "…"


def build_context_block(hits: list[dict]) -> str:
    """把检索命中拼成可注入系统提示的文本块。

    规矩（施工单 3.4 / 3.5；返工单 30 修正）：
    - 空列表返回空串（调用方不注入，不塞空内容占上下文）。
    - source / verified_at / written_at 三元组原样给出，不写裁决规则。
    - 内容用**完整 content**（按字符限长），不用 FTS5 snippet——
      snippet 是"命中片段"，token 数上限在不同分词器/版本下会截断答案词
      （1 token=3 字符时 12 token 只剩约 14 字符），也可能多窗口重复拼接；
      注入给模型看的应该是记忆全文，不是检索高亮。
    """
    if not hits:
        return ""
    lines = [
        "[记忆层命中]（供参考的历史记忆。每条附来源与时间，若相互冲突请自行权衡，"
        "没有隐藏的优先级规则）"
    ]
    for i, h in enumerate(hits, 1):
        verified = h.get("verified_at") or "从未核过"
        lines.append(f"{i}. {h['path']}（kind: {h.get('kind', '')}）")
        lines.append(
            f"   来源: {h.get('source', '')} · 写入: {h.get('written_at', '')} · 核过: {verified}"
        )
        if h.get("expires_hint"):
            lines.append(f"   过期提示: {h['expires_hint']}")
        text = (h.get("content") or "").strip()
        if text:
            lines.append(f"   内容: {_clip_entry(text)}")
    block = "\n".join(lines)
    if len(block) > BLOCK_MAX_CHARS:  # 整块兜底（极少触发）
        block = block[: BLOCK_MAX_CHARS - 1] + "…"
    return block


def _plain_snippet(content: str, needle: str, width: int = 30) -> str:
    """LIKE 命中时自制的命中片段（FTS snippet 的等价兜底）。"""
    idx = content.find(needle)
    if idx < 0:
        return content[: width * 2]
    start = max(0, idx - width)
    end = min(len(content), idx + len(needle) + width)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(content) else ""
    return f"{prefix}{content[start:end]}{suffix}"


def _split_query(query: str, max_like: int = 16) -> tuple[list[str], list[str]]:
    """把查询切成 FTS trigram 短语（3 字滑窗）与 LIKE 兜底词（2 字滑窗）。

    2 字滑窗是必需的：中文提问与记忆原文常常只有 2 字词面重叠
    （如"回答风格"对"详细的回答"），3 字窗口够不着。
    """
    segs = [s for s in _SEG_SPLIT_RE.split(query) if s]
    phrases: list[str] = []
    like_terms: list[str] = []
    for seg in segs:
        if len(seg) >= 3:
            n = len(seg) - 2
            step = max(1, (n + 11) // 12)  # 每段最多 12 个滑窗，防爆
            for i in range(0, n, step):
                phrases.append('"' + seg[i : i + 3] + '"')
        for i in range(max(0, len(seg) - 1)):
            bg = seg[i : i + 2]
            if bg not in like_terms:
                like_terms.append(bg)
    return phrases, like_terms[:max_like]


# ------------------------------------------------------------------ 存储

class MemoryStore:
    """记忆层存储：六命令 + search。每次操作独立连接（线程安全）。"""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        ensure_tables(db_path)

    # ---------- FTS 同步（external content 表手动维护） ----------

    def _fts_add(self, conn: sqlite3.Connection, rowid: int, content: str, path: str) -> None:
        conn.execute(
            "INSERT INTO memories_fts(rowid, content, path) VALUES (?, ?, ?)",
            (rowid, content, path),
        )

    def _fts_remove(self, conn: sqlite3.Connection, rowid: int, content: str, path: str) -> None:
        # external content 表的删除必须带上当初写入的原值
        conn.execute(
            "INSERT INTO memories_fts(memories_fts, rowid, content, path)"
            " VALUES ('delete', ?, ?, ?)",
            (rowid, content, path),
        )

    # ---------- 六命令 ----------

    def create(
        self,
        path: str,
        content: str,
        kind: str,
        source: str,
        verified_at: str | None = None,
        expires_hint: str | None = None,
    ) -> int:
        """新建记忆。重名报错（不静默覆盖）；source 必填；kind 必须在值域内。"""
        if not path or not path.startswith("/"):
            raise MemoryCommandError("path 必须以 / 开头，形如 /memories/preferences/x.md")
        if not content or not content.strip():
            raise MemoryCommandError("content 不能为空")
        if kind not in VALID_KINDS:
            raise MemoryCommandError(f"kind 必须是 {'/'.join(VALID_KINDS)} 之一，收到: {kind!r}")
        if not source or not source.strip():
            raise MemoryCommandError("source 必填（用户说的 / 从对话抽取 / 从文件导入 / 系统推断）")
        with _connect(self.db_path) as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO memories"
                    " (path, content, kind, source, written_at, verified_at, expires_hint)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (path, content, kind, source, _now_iso(), verified_at, expires_hint),
                )
            except sqlite3.IntegrityError as exc:
                raise MemoryCommandError(f"path 已存在（重名不覆盖）: {path}") from exc
            rowid = int(cur.lastrowid or 0)
            self._fts_add(conn, rowid, content, path)
        return rowid

    def view(self, path: str) -> list[dict]:
        """精确路径返回单条；前缀匹配列目录；完全无匹配报错。

        已软删（tombstone）的条目不在此出现——查 tombstone 用 tombstones()。
        """
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                f"SELECT {_SEARCH_COLS}, m.superseded_by, m.deleted_at"
                " FROM memories m WHERE m.deleted_at IS NULL ORDER BY m.path"
            ).fetchall()
        exact = [dict(r) for r in rows if r["path"] == path]
        if exact:
            return exact
        prefix = [dict(r) for r in rows if r["path"].startswith(path)]
        if prefix:
            return prefix
        raise MemoryCommandError(f"路径不存在: {path}")

    def str_replace(self, path: str, old: str, new: str) -> None:
        """精确替换。old 不唯一或不存在都报错（不猜）。"""
        if not old:
            raise MemoryCommandError("old 不能为空")
        with _connect(self.db_path) as conn:
            row = self._fetch_live(conn, path)
            content = row["content"]
            count = content.count(old)
            if count == 0:
                raise MemoryCommandError(f"old 在 {path} 中不存在: {old!r}")
            if count > 1:
                raise MemoryCommandError(f"old 在 {path} 中出现 {count} 次，不唯一，拒绝替换")
            updated = content.replace(old, new, 1)
            conn.execute(
                "UPDATE memories SET content = ? WHERE id = ?", (updated, row["id"])
            )
            self._fts_remove(conn, row["id"], content, path)
            self._fts_add(conn, row["id"], updated, path)

    def insert(self, path: str, text: str, after: int | None = None) -> None:
        """插入一行。after=None/0 插在最前；after=k 插在第 k 行后（1-based）。"""
        if not text:
            raise MemoryCommandError("text 不能为空")
        with _connect(self.db_path) as conn:
            row = self._fetch_live(conn, path)
            lines = row["content"].split("\n")
            if after is None or after == 0:
                lines.insert(0, text)
            elif 1 <= after <= len(lines):
                lines.insert(after, text)
            else:
                raise MemoryCommandError(
                    f"after={after} 超界：{path} 共 {len(lines)} 行"
                )
            updated = "\n".join(lines)
            conn.execute(
                "UPDATE memories SET content = ? WHERE id = ?", (updated, row["id"])
            )
            self._fts_remove(conn, row["id"], row["content"], path)
            self._fts_add(conn, row["id"], updated, path)

    def delete(self, path: str) -> None:
        """软删：写 deleted_at（tombstone），不物理删，FTS 同步移除。"""
        with _connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT id, content, path, deleted_at FROM memories WHERE path = ?",
                (path,),
            ).fetchone()
            if row is None:
                raise MemoryCommandError(f"路径不存在: {path}")
            if row["deleted_at"] is not None:
                raise MemoryCommandError(f"该条已删除（tombstone）: {path}")
            conn.execute(
                "UPDATE memories SET deleted_at = ? WHERE id = ?",
                (_now_iso(), row["id"]),
            )
            self._fts_remove(conn, row["id"], row["content"], path)

    def rename(self, old: str, new: str) -> None:
        """改路径。旧路径必须存在，新路径必须空闲。"""
        if not new or not new.startswith("/"):
            raise MemoryCommandError("新 path 必须以 / 开头")
        with _connect(self.db_path) as conn:
            row = self._fetch_live(conn, old)
            try:
                conn.execute(
                    "UPDATE memories SET path = ? WHERE id = ?", (new, row["id"])
                )
            except sqlite3.IntegrityError as exc:
                raise MemoryCommandError(f"新 path 已存在: {new}") from exc
            self._fts_remove(conn, row["id"], row["content"], old)
            self._fts_add(conn, row["id"], row["content"], new)

    def search(self, query: str, limit: int = 5) -> list[dict]:
        """FTS5 检索，返回 path + 命中片段 + 元数据。

        已软删、已被取代（superseded_by 非空）的条目不出现。
        无命中返回空列表（调用方据此不注入）。
        """
        query = (query or "").strip()
        if not query:
            return []
        phrases, like_terms = _split_query(query)
        found: dict[int, dict] = {}

        with _connect(self.db_path) as conn:
            if phrases:
                rows = conn.execute(
                    f"SELECT {_SEARCH_COLS},"
                    " snippet(memories_fts, 0, '【', '】', '…', 12) AS snippet"
                    " FROM memories_fts JOIN memories m ON m.id = memories_fts.rowid"
                    " WHERE memories_fts MATCH ?"
                    " AND m.deleted_at IS NULL AND m.superseded_by IS NULL"
                    " ORDER BY rank LIMIT ?",
                    (" OR ".join(phrases), limit),
                ).fetchall()
                for r in rows:
                    found[r["id"]] = dict(r)
            if like_terms:
                # 2 字词 trigram 匹配不了，走 LIKE 兜底（一次全扫、按命中词数排序）
                cond = " OR ".join(
                    "(m.content LIKE ? OR m.path LIKE ?)" for _ in like_terms
                )
                args: list = []
                for term in like_terms:
                    pat = f"%{term}%"
                    args += [pat, pat]
                rows = conn.execute(
                    f"SELECT {_SEARCH_COLS} FROM memories m"
                    f" WHERE m.deleted_at IS NULL AND m.superseded_by IS NULL AND ({cond})"
                    " ORDER BY m.written_at DESC",
                    args,
                ).fetchall()
                scored = []
                for r in rows:
                    hay = r["content"] + r["path"]
                    score = sum(1 for t in like_terms if t in hay)
                    if score > 0:
                        scored.append((score, r))
                scored.sort(key=lambda x: -x[0])  # 同分保持 written_at DESC 原序
                for _score, r in scored:
                    if r["id"] not in found:
                        item = dict(r)
                        item["snippet"] = next(
                            (
                                _plain_snippet(r["content"], t)
                                for t in like_terms
                                if t in r["content"] or t in r["path"]
                            ),
                            r["content"][:60],
                        )
                        found[r["id"]] = item
        return list(found.values())[:limit]

    # ---------- 查询辅助（加固脚本与人工核对用） ----------

    def never_verified(self) -> list[dict]:
        """verified_at 为空的存活条目——"哪些从没核过"必须能查出来。"""
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                f"SELECT {_SEARCH_COLS}, m.superseded_by, m.deleted_at"
                " FROM memories m"
                " WHERE m.verified_at IS NULL AND m.deleted_at IS NULL"
                " ORDER BY m.written_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def tombstones(self) -> list[dict]:
        """所有软删条目（tombstone），含 deleted_at 与 superseded_by。"""
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                f"SELECT {_SEARCH_COLS}, m.superseded_by, m.deleted_at"
                " FROM memories m WHERE m.deleted_at IS NOT NULL ORDER BY m.path"
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_superseded(self, old_path: str, new_id: int) -> None:
        """标记旧条被新条（id=new_id）取代。不删旧条，只写 superseded_by。"""
        with _connect(self.db_path) as conn:
            if conn.execute(
                "SELECT 1 FROM memories WHERE id = ? AND deleted_at IS NULL", (new_id,)
            ).fetchone() is None:
                raise MemoryCommandError(f"取代目标不存在或已删: id={new_id}")
            cur = conn.execute(
                "UPDATE memories SET superseded_by = ?"
                " WHERE path = ? AND deleted_at IS NULL",
                (new_id, old_path),
            )
            if cur.rowcount == 0:
                raise MemoryCommandError(f"被取代的路径不存在或已删: {old_path}")

    def get_context_block(self, query: str, limit: int = 5) -> str:
        """检索 + 拼注入块。空命中返回空串（调用方不注入）。"""
        return build_context_block(self.search(query, limit=limit))

    # ---------- 内部 ----------

    @staticmethod
    def _fetch_live(conn: sqlite3.Connection, path: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT id, path, content FROM memories WHERE path = ? AND deleted_at IS NULL",
            (path,),
        ).fetchone()
        if row is None:
            raise MemoryCommandError(f"路径不存在（或已删除）: {path}")
        return row


# ------------------------------------------------------------------
# v2：模型工具调用（施工单 32 · S1）
#
# 设计者修正（2026-11-01）：path 与 source 不让模型填——
#   模型会编不规范 path（实测编出"编程偏好/代码格式化"，无 /memories/ 前缀），
#   也会把用户原话当 source 填回来（循环引用，无审计信息量）。
#   → path 由代码生成 /memories/{kind}/{slug}.md，source 固定"模型自主写入"，
#     schema 只留 kind / title / content 三个参数。
# ------------------------------------------------------------------

MEMORY_SOURCE_AUTO = "模型自主写入"


def _slugify(title: str, max_len: int = 40) -> str:
    """把模型的 title 短语清洗成路径安全的 slug：中文保留，空白与非法字符转 -。"""
    s = re.sub(r'[\\/:*?"<>|\s]+', "-", (title or "").strip()).strip("-")
    s = s[:max_len].strip("-")
    return s or "note"


def memory_tools_schema() -> list[dict]:
    """三个记忆工具的 OpenAI JSON Schema（与 MemoryToolExecutor 一一对应）。"""
    return [
        {"type": "function", "function": {
            "name": "memory_create",
            "description": (
                "把一条值得长期保留的记忆写入记忆层（用户明确说出的偏好/事实/结论/技能，"
                "或对话中得出的重要结论）。不确定值不值得记就不要调用；闲聊不要调用。"
                "不要记录以下内容："
                "① 短期、一次性的提醒或待办（「明天提醒我 X」「记得下午做 Y」）；"
                "② 时效性信息（今天的天气、当前时间、正在进行的对话内容）；"
                "③ 你自己的中间推理或过程说明。"
                "只记录会长期有效的事实、偏好、决定。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": list(VALID_KINDS),
                        "description": (
                            "按顺序判断，命中即停："
                            "1. fact —— 客观事实或配置，可用「是什么/在哪里/有多少」提问；"
                            "2. preference —— 用户偏好或习惯，含「喜欢/习惯/偏好/不喜欢/不要」；"
                            "3. conclusion —— 已做出的决定，含「定了/决定了/结论是/选定了」；"
                            "4. skill —— 用户会做但模型不会的事（用于分工）。"
                            "边界规则：事实里带「我」的主语（如「我的项目在 X」）仍然是 fact，不是 preference；"
                            "只有表达「偏好/取舍」的才是 preference；判不出来就填 fact（最保守，不影响偏好类检索）。"
                        ),
                    },
                    "title": {
                        "type": "string",
                        "description": "短语标题（用于生成存储路径），如：回答风格偏好",
                    },
                    "content": {
                        "type": "string",
                        "description": "记忆正文，一两句话说清这件事本身",
                    },
                },
                "required": ["kind", "title", "content"],
            },
        }},
        {"type": "function", "function": {
            "name": "memory_view",
            "description": "查看某条记忆或某目录下的记忆列表。path 来自 memory_create 的返回结果。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "/memories/ 开头的存储路径"},
                },
                "required": ["path"],
            },
        }},
        {"type": "function", "function": {
            "name": "memory_search",
            "description": "按关键词检索记忆层，返回相关记忆及其来源与写入时间。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "关键词或短语"},
                    "limit": {"type": "integer", "description": "最多返回几条，默认 5"},
                },
                "required": ["query"],
            },
        }},
        {"type": "function", "function": {
            "name": "history_search",
            "description": (
                "在历史对话记录里全文检索，返回匹配的对话片段（含所属会话名、时间、片段原文）。"
                "适合回忆「之前聊过什么」。检索不到会返回空列表——此时如实告诉用户没有相关记录，"
                "不要凭印象编造。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "关键词或短语"},
                    "limit": {"type": "integer", "description": "最多返回几条，默认 5"},
                },
                "required": ["query"],
            },
        }},
    ]


class MemoryToolExecutor:
    """把模型发来的工具调用路由到 MemoryStore。所有返回都是可 JSON 序列化的 dict。

    约定：命令语义错误（重名 / kind 非法 / 路径不存在）不抛异常，
    统一回 {"ok": False, "error": ...} 给模型，让它自己决定下一步（29 号单确立的回填风格）。
    """

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def execute(self, name: str, arguments: dict) -> dict:
        arguments = arguments or {}
        try:
            if name == "memory_create":
                return self._create(arguments)
            if name == "memory_view":
                rows = self.store.view(str(arguments.get("path") or ""))
                return {"ok": True, "count": len(rows), "items": rows[:10]}
            if name == "memory_search":
                raw_limit = arguments.get("limit") or 5
                try:
                    limit = max(1, min(20, int(raw_limit)))
                except (TypeError, ValueError):
                    limit = 5
                hits = self.store.search(str(arguments.get("query") or ""), limit=limit)
                return {"ok": True, "count": len(hits), "items": hits}
            if name == "history_search":
                return self._history_search(arguments)
            return {"ok": False, "error": f"未知工具: {name}"}
        except MemoryCommandError as e:
            return {"ok": False, "error": str(e)}

    def _history_search(self, arguments: dict) -> dict:
        """对话历史全文检索（施工单 38 任务 4）。

        - 用 messages_fts（trigram，≥3 字 MATCH 滑窗 + 2 字 LIKE 兜底，与记忆层同策略）
        - 出处带会话名 + 时间 + 片段原文（截断，防长消息撑爆上下文）
        - 检索不到返回空列表（{"ok": True, "count": 0}），让模型如实说"没有记录"
        """
        query = str(arguments.get("query") or "").strip()
        if not query:
            return {"ok": False, "error": "query 必填（关键词或短语）"}
        try:
            limit = max(1, min(20, int(arguments.get("limit") or 5)))
        except (TypeError, ValueError):
            limit = 5

        phrases, like_terms = _split_query(query)
        found: dict[int, dict] = {}

        with _connect(self.store.db_path) as conn:
            if phrases:
                rows = conn.execute(
                    "SELECT m.id, m.role, m.content, m.created_at, s.title AS session_title"
                    " FROM messages_fts f"
                    " JOIN messages m ON m.id = f.rowid"
                    " JOIN sessions s ON s.id = m.session_id"
                    " WHERE messages_fts MATCH ? AND m.role IN ('user','assistant')"
                    " ORDER BY m.created_at DESC LIMIT ?",
                    (" OR ".join(phrases), limit),
                ).fetchall()
                for r in rows:
                    found[r["id"]] = self._history_item(r)
            if like_terms and len(found) < limit:
                cond = " OR ".join("m.content LIKE ?" for _ in like_terms)
                rows = conn.execute(
                    "SELECT m.id, m.role, m.content, m.created_at, s.title AS session_title"
                    " FROM messages m JOIN sessions s ON s.id = m.session_id"
                    f" WHERE m.role IN ('user','assistant') AND ({cond})"
                    " ORDER BY m.created_at DESC LIMIT ?",
                    [f"%{t}%" for t in like_terms] + [limit],
                ).fetchall()
                for r in rows:
                    if r["id"] not in found:
                        found[r["id"]] = self._history_item(r)
        items = sorted(found.values(), key=lambda x: x["time"], reverse=True)[:limit]
        return {"ok": True, "count": len(items), "items": items}

    @staticmethod
    def _history_item(r: sqlite3.Row) -> dict:
        from datetime import datetime as _dt

        try:
            when = _dt.fromtimestamp(r["created_at"]).strftime("%Y-%m-%d %H:%M")
        except (TypeError, OSError, ValueError):
            when = "时间未知"
        return {
            "session": r["session_title"] or "（无标题会话）",
            "role": r["role"],
            "time": when,
            "snippet": (r["content"] or "")[:160],
        }

    def _create(self, arguments: dict) -> dict:
        kind = str(arguments.get("kind") or "").strip()
        title = str(arguments.get("title") or "").strip()
        content = str(arguments.get("content") or "").strip()
        if kind not in VALID_KINDS:
            return {"ok": False, "error": f"kind 必须是 {'/'.join(VALID_KINDS)} 之一，收到: {kind!r}"}
        if not title:
            return {"ok": False, "error": "title 必填（短语标题）"}
        if not content:
            return {"ok": False, "error": "content 必填（记忆正文）"}
        path = f"/memories/{kind}/{_slugify(title)}.md"
        new_id = self.store.create(path, content, kind, MEMORY_SOURCE_AUTO)
        return {"ok": True, "id": new_id, "path": path}
