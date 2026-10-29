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
    return {"db": str(path), "memories_rows": n, "columns": sorted(cols)}


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
