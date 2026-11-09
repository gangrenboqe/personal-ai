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
        if h.get("faded"):
            lines.append(
                f"   ⚠️ 淡忘：超过 {FADE_DAYS} 天未确认，仅供参考"
                "（确认仍成立可调 memory_verify 恢复权重；未删除未隐藏）"
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


# ------------------------------------------------------------------
# 43 号：冲突检测 + 淡忘不删
#
# 只用 memories 表现有字段（kind / superseded_by / verified_at / written_at /
# deleted_at），不加列、不改表结构。铁律：不删任何记忆、不自动合并、
# 不改任何一条的 content；判定宁可漏报也不误报（误报一次人就不再信这个功能）。
# ------------------------------------------------------------------

# 淡忘线（天）：距「确认锚点」超过这么多天就在 search 里降权。
# 90 天 ≈ 一个季度的复核周期：太短会频繁降权打扰人，太长起不到提醒复核的作用。
FADE_DAYS = 90

# 淡忘权重：命中相关度乘 0.7 往后放（工单指定值）。只降权——仍然搜得到，
# 绝不隐藏、绝不删除、绝不改内容；人工 mark_verified 确认后恢复满权重。
# 注：表里没有「最近被查过」字段且本单不许改表结构，「没被查过」统计不了，
# 淡忘按「没被核过」口径计算（如实说明，不装能查到）。
FADE_WEIGHT = 0.7

# 断言判定窗口（字符）：取值词前面这么近的距离内出现断言动词，才算「选了它」。
# 窗口太大会把无关句子的动词算进来（误报），太小会漏掉正常语序（漏报可接受）。
_ASSERT_WINDOW = 10

# 主题提示词与取值词的最大间距（字符）：提示词（「缩进」「数据库」）与取值词紧邻
# （如「tab 缩进」「数据库用 SQLite」）即视为断言——主题词+取值词连写本身就是断言。
# 间距放太宽会把别的句子扯进来（误报）。
_HINT_GAP = 6

# 断言动词：只认明确的选择语气。「迁移到 / 评估 / 考虑」不算——迁移记录同时提到
# 新旧两个值，算断言必误报（宁漏勿误）。
_CONFLICT_ASSERT_VERBS = ("用", "使用", "采用", "选定", "统一", "换用", "改用")

# 断言排除窗口：取值词紧前面是这些字样时不算断言
# （否定式「不上 Postgres」、来源式「从 SQLite …」、迁移/评估/考虑语境）。
_CONFLICT_ASSERT_EXCLUDE = re.compile(
    r"(不|别|禁止|拒绝|从|由|迁移|评估|考虑)[^，。；,;]{0,4}$"
)

# 互斥取值家族（43 号）：只登记「同一处选型只能取一」的确定家族，宁漏勿误。
# 每族：name=冲突理由里给人家看的名字；hints=主题提示词（两边正文都要出现才算
# 同主题）；values=取值 → 别名元组。扩家族只改这张表，不改判定逻辑。
_CONFLICT_FAMILIES = (
    {
        "name": "数据库选型",
        "hints": ("数据库", "存储", "选型"),
        "values": {
            "SQLite": ("sqlite",),
            "Postgres": ("postgres", "postgresql", "pgsql"),
            "MySQL": ("mysql",),
        },
    },
    {
        "name": "缩进风格",
        "hints": ("缩进",),
        "values": {
            "tab": ("tab",),
            "空格": ("空格", "space", "spaces"),
        },
    },
)

# 推荐/不推荐：肯定式与否定式的捕获模板。肯定式带 (?<![不别要]) 避免「不推荐」
# 被当成「推荐」。实体核心词去掉通用后缀后必须完全相等——这是「关键实体相同」的
# 可解释判定；不用全文全等（工单禁止），也不用子串包含（「A 公司」和「B 公司」
# 共有「公司」二字，子串包含必误报）。
_RECOMMEND_POS_RE = re.compile(r"(?<![不别要])推荐\s*[:：]?\s*([^\s，。；,.;：！!？?]{1,24})")
_RECOMMEND_NEG_RE = re.compile(
    r"(?:不推荐|别推荐|不建议|不要推荐)\s*[:：]?\s*([^\s，。；,.;：！!？?]{1,24})"
)
_ENTITY_SUFFIX_RE = re.compile(r"(?:公司|项目|方案|工具|库|框架|岗位|路线|软件)$")


def _extract_entities(matches: list[str]) -> set[str]:
    """把捕获到的实体短语清洗成核心词（去通用后缀、去首尾空白、转小写）。"""
    out: set[str] = set()
    for raw in matches:
        core = raw.strip("　.。,，-— ")
        while core:
            stripped = _ENTITY_SUFFIX_RE.sub("", core).strip("　 ")
            if stripped == core or not stripped:
                break
            core = stripped
        if core:
            out.add(core.lower())
    return out


def _recommend_conflicts(a: str, b: str) -> list[str]:
    """推荐/不推荐同一实体：关键实体相同 + 取值相反。"""
    out: list[str] = []
    pos_a = _extract_entities(_RECOMMEND_POS_RE.findall(a))
    neg_a = _extract_entities(_RECOMMEND_NEG_RE.findall(a))
    pos_b = _extract_entities(_RECOMMEND_POS_RE.findall(b))
    neg_b = _extract_entities(_RECOMMEND_NEG_RE.findall(b))
    for e in sorted(pos_a & neg_b):
        out.append(f"推荐冲突：一条「推荐 {e}」，另一条「不推荐 {e}」")
    for e in sorted(pos_b & neg_a):
        out.append(f"推荐冲突：一条「不推荐 {e}」，另一条「推荐 {e}」")
    return out


def _asserted_values(text: str, family: dict) -> set[str]:
    """提取一条正文里「被断言」的取值。只提到、没断言的不算（宁漏勿误）。

    断言 = 满足其一：①取值词前 _ASSERT_WINDOW 内有断言动词；
    ②主题提示词与取值词间距 ≤ _HINT_GAP（「tab 缩进」这类连写）。
    再叠加排除窗口（否定式 / 来源式「从 X …」），双保险压误报。
    """
    out: set[str] = set()
    hints = [h for h in family["hints"] if h in text]
    if not hints:
        return out  # 连主题词都没有，整族不判
    low = text.lower()
    for value, aliases in family["values"].items():
        for alias in aliases:
            pat = (rf"(?<![A-Za-z]){re.escape(alias)}(?![A-Za-z])"
                   if alias.isascii() else re.escape(alias))
            for m in re.finditer(pat, low):
                window = text[max(0, m.start() - _ASSERT_WINDOW): m.start()]
                has_verb = any(v in window for v in _CONFLICT_ASSERT_VERBS)
                near_hint = any(
                    h in text[max(0, m.start() - _HINT_GAP): m.end() + _HINT_GAP]
                    for h in hints
                )
                if not (has_verb or near_hint):
                    continue  # 只是提到，没断言
                if _CONFLICT_ASSERT_EXCLUDE.search(window):
                    continue  # 否定式（不上 Postgres）/ 来源式（从 SQLite …）不算
                out.add(value)
                break
    return out


def _family_conflicts(a: str, b: str) -> list[str]:
    """同主题（两边正文都出现家族主题词）+ 各自断言不同取值。"""
    out: list[str] = []
    for family in _CONFLICT_FAMILIES:
        if not (any(h in a for h in family["hints"]) and any(h in b for h in family["hints"])):
            continue  # 同主题不成立，不判
        for va in sorted(_asserted_values(a, family)):
            for vb in sorted(_asserted_values(b, family)):
                if va != vb:
                    out.append(
                        f"取值相反（{family['name']}）：一条断言「{va}」，另一条断言「{vb}」"
                    )
    return out


def detect_conflicts(content_a: str, content_b: str) -> list[str]:
    """对比两条记忆正文，返回疑似冲突理由列表（空 = 未发现）。只判不记，方向无关。

    可解释规则只有两类（工单口径：同主题 + 关键实体相同 + 取值相反）：
    ① 推荐/不推荐同一实体；② 同一互斥家族里断言了不同取值。
    全是保守判定：宁可漏报；不拿全文全等当判据、不用 embedding、不引第三方包。
    """
    a, b = content_a or "", content_b or ""
    return _recommend_conflicts(a, b) + _family_conflicts(a, b)


def _is_faded(verified_at: str | None, written_at: str, now: datetime | None = None) -> bool:
    """淡忘判定：距「确认锚点」超过 FADE_DAYS 天。

    锚点 = verified_at（人工确认过就以最近一次确认为准），没核过落回 written_at。
    「没被查过」统计不了（无字段且不许改表结构），按「没核过」口径——如实。
    时间格式异常时宁可当没淡忘（不降权，不误伤）。
    """
    anchor = verified_at or written_at
    if not anchor:
        return False
    try:
        t = datetime.fromisoformat(str(anchor))
    except ValueError:
        return False
    now = now or datetime.now()
    return (now - t).days > FADE_DAYS


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
                    " ORDER BY rank * (CASE"
                    "   WHEN julianday('now') - julianday(COALESCE(m.verified_at, m.written_at)) > ?"
                    "   THEN ? ELSE 1.0 END)"
                    " LIMIT ?",
                    # 43 号淡忘：bm25 的 rank 是「越小（越负）越好」，相关度乘 0.7
                    # 等价于 rank 乘 0.7（往 0 靠 = 变差）。只降权，不隐藏。
                    (FADE_DAYS, FADE_WEIGHT, " OR ".join(phrases), limit),
                ).fetchall()
                for r in rows:
                    item = dict(r)
                    item["faded"] = _is_faded(r["verified_at"], r["written_at"])
                    found[r["id"]] = item
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
                    base = sum(1 for t in like_terms if t in hay)
                    if base > 0:
                        # 43 号淡忘：相关度乘 0.7 往后放（同分稳定排序保住 written_at 原序）
                        faded = _is_faded(r["verified_at"], r["written_at"])
                        scored.append((base * (FADE_WEIGHT if faded else 1.0), faded, r))
                scored.sort(key=lambda x: -x[0])  # 同分保持 written_at DESC 原序
                for score, faded, r in scored:
                    if r["id"] not in found:
                        item = dict(r)
                        item["faded"] = faded
                        item["score"] = round(score, 3)
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

    # ---------- 43 号：冲突检测 + 淡忘 ----------

    def find_conflicts_for(
        self, kind: str, content: str, exclude_id: int | None = None
    ) -> list[dict]:
        """给定新记忆的 kind+content，在同 kind 存活旧记忆里找疑似冲突。

        只查不写（标不标 superseded_by 由调用方决定）；判定宁可漏报不误报，
        命中时带可解释的 reasons，供调用方与人工核对。
        """
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT id, path, content, source, written_at, verified_at"
                " FROM memories WHERE kind = ? AND deleted_at IS NULL"
                " AND superseded_by IS NULL AND id != ? ORDER BY id",
                (kind, exclude_id if exclude_id is not None else -1),
            ).fetchall()
        hits: list[dict] = []
        for r in rows:
            reasons = detect_conflicts(content, r["content"])
            if reasons:
                hits.append({**dict(r), "reasons": reasons})
        return hits

    def mark_verified(self, path: str) -> str:
        """43 号：「这条我确认过了」——刷新 verified_at，解除淡忘降权。

        只动 verified_at，绝不改 content，绝不删除。返回刷新时间。
        """
        with _connect(self.db_path) as conn:
            row = self._fetch_live(conn, path)
            ts = _now_iso()
            conn.execute(
                "UPDATE memories SET verified_at = ? WHERE id = ?", (ts, row["id"])
            )
        return ts

    def list_conflicts(self) -> list[dict]:
        """43 号：两两比对全部存活记忆，列出疑似冲突对。只读不写，不替人裁决。

        每对带双方 id / path / content 原文 / 各自时间 / superseded_by 与冲突理由。
        冲突只判同 kind（工单口径）；软删（tombstone）条目不参与。
        """
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT id, path, content, kind, source, written_at, verified_at,"
                " superseded_by FROM memories WHERE deleted_at IS NULL ORDER BY id"
            ).fetchall()
        items = [dict(r) for r in rows]
        keys = ("id", "path", "content", "written_at", "verified_at", "superseded_by")
        pairs: list[dict] = []
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                a, b = items[i], items[j]
                if a["kind"] != b["kind"]:
                    continue
                reasons = detect_conflicts(a["content"], b["content"])
                if reasons:
                    pairs.append({
                        "a": {k: a[k] for k in keys},
                        "b": {k: b[k] for k in keys},
                        "reasons": reasons,
                        "note": "疑似冲突，仅供人工裁决；两条都保留，本工具不替你作废任何一条",
                    })
        return pairs

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
            "name": "memory_verify",
            "description": (
                f"「这条我确认过了」：把某条记忆的 verified_at 刷新为现在，解除淡忘降权"
                f"（超过 {FADE_DAYS} 天没确认的记忆在检索里会往后排，但不隐藏不删除）。"
                "只在你确实核对了内容仍然成立时调用；本工具不改变记忆内容本身。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "/memories/ 开头的存储路径"}},
                "required": ["path"],
            },
        }},
        {"type": "function", "function": {
            "name": "memory_conflicts",
            "description": (
                "列出记忆层里所有「疑似自相矛盾」的记忆对：双方的路径、原文全文、各自时间、"
                "冲突理由。只列不裁——工具不会替你作废或合并任何一条，裁决由人做："
                "确认旧的不对就 memory_delete 软删或 memory_update 改写；确认没问题就 memory_verify。"
                "没有疑似冲突时返回空列表，不要凭空编造冲突。"
            ),
            "parameters": {"type": "object", "properties": {}},
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
            if name == "memory_list":
                """MCP 暴露用（39 号）：按 kind 过滤列出记忆，无 kind 则全部。"""
                raw_limit = arguments.get("limit") or 20
                try:
                    limit = max(1, min(50, int(raw_limit)))
                except (TypeError, ValueError):
                    limit = 20
                kind = str(arguments.get("kind") or "").strip()
                sql = (
                    "SELECT path, kind, source, written_at, verified_at,"
                    " substr(content, 1, 160) AS snippet"
                    " FROM memories WHERE deleted_at IS NULL"
                )
                params: list = []
                if kind:
                    sql += " AND kind = ?"
                    params.append(kind)
                # 时间范围过滤（written_at 是 ISO8601 文本，可直接字符串比较）
                since = str(arguments.get("since") or "").strip()
                until = str(arguments.get("until") or "").strip()
                if since:
                    sql += " AND written_at >= ?"
                    params.append(since)
                if until:
                    sql += " AND written_at <= ?"
                    params.append(until)
                sql += " ORDER BY written_at DESC LIMIT ?"
                params.append(limit)
                with _connect(self.store.db_path) as conn:
                    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
                return {"ok": True, "count": len(rows), "items": rows}
            if name == "memory_update":
                """精确编辑：old→new（复用 str_replace，FTS 同步更新）。"""
                path = str(arguments.get("path") or "")
                self.store.str_replace(path, str(arguments.get("old") or ""), str(arguments.get("new") or ""))
                return {"ok": True, "path": path, "message": "已更新"}
            if name == "memory_delete":
                """软删：写 deleted_at，不物理删除（29 号语义）。"""
                path = str(arguments.get("path") or "")
                self.store.delete(path)
                return {"ok": True, "path": path, "message": "已软删（tombstone 保留）"}
            if name == "memory_rename":
                old = str(arguments.get("old") or "")
                new = str(arguments.get("new") or "")
                self.store.rename(old, new)
                return {"ok": True, "path": new, "message": f"已从 {old} 重命名"}
            if name == "memory_insert":
                path = str(arguments.get("path") or "")
                self.store.insert(path, str(arguments.get("text") or ""), arguments.get("after"))
                return {"ok": True, "path": path, "message": "已插入"}
            if name == "memory_stats":
                """按 kind 统计（只读，39/40 号先例：executor 直连 SQL 不算改核心）。"""
                with _connect(self.store.db_path) as conn:
                    rows = [dict(r) for r in conn.execute(
                        "SELECT kind, COUNT(*) AS count, MAX(written_at) AS latest"
                        " FROM memories WHERE deleted_at IS NULL GROUP BY kind ORDER BY count DESC"
                    ).fetchall()]
                total = sum(r["count"] for r in rows)
                return {"ok": True, "total": total, "by_kind": rows}
            if name == "memory_export":
                with _connect(self.store.db_path) as conn:
                    rows = [dict(r) for r in conn.execute(
                        "SELECT path, kind, content, source, written_at, verified_at"
                        " FROM memories WHERE deleted_at IS NULL ORDER BY path"
                    ).fetchall()]
                return {"ok": True, "count": len(rows), "items": rows}
            if name == "memory_import":
                """批量导入：items=[{kind,title,content},...]，source 标「批量导入」。"""
                items = arguments.get("items")
                if not isinstance(items, list) or not items:
                    return {"ok": False, "error": "items 必须是非空数组，元素含 kind/title/content"}
                imported, failed = 0, []
                for i, item in enumerate(items):
                    if not isinstance(item, dict):
                        failed.append({"index": i, "error": "元素不是对象"})
                        continue
                    r = self._create(item, source="批量导入")
                    if r.get("ok"):
                        imported += 1
                    else:
                        failed.append({"index": i, "error": r.get("error")})
                return {"ok": True, "imported": imported, "failed": failed}
            if name == "memory_verify":
                """43 号：确认一条记忆仍成立（刷新 verified_at，解除淡忘降权）。"""
                path = str(arguments.get("path") or "")
                ts = self.store.mark_verified(path)
                return {"ok": True, "path": path, "verified_at": ts,
                        "message": "已确认（verified_at 已刷新，淡忘降权解除）"}
            if name == "memory_conflicts":
                """43 号：列出疑似冲突对。只列不裁——裁决由人做。"""
                pairs = self.store.list_conflicts()
                return {"ok": True, "count": len(pairs), "items": pairs}
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

    def _create(self, arguments: dict, source: str = MEMORY_SOURCE_AUTO) -> dict:
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
        new_id = self.store.create(path, content, kind, source)
        # 43 号：写入后立刻在同 kind 旧记忆里找疑似冲突。两条都保留、**都留在检索里**——
        # **不标 superseded_by**。原因：这只是自动检出的「疑似」，还没人工裁决；
        # 一旦标记，search 会把旧条排除（既有语义），等于在没人确认的情况下
        # 让一种说法从检索里消失，调用方甚至不知道存在另一种说法。
        # 明确取代要走 mark_superseded —— 那是人的决定，不是检测器的决定。
        conflicts = self.store.find_conflicts_for(kind, content, exclude_id=new_id)
        marked = [{"id": c["id"], "path": c["path"], "reasons": c["reasons"]} for c in conflicts]
        out = {"ok": True, "id": new_id, "path": path}
        if marked:
            out["conflicts"] = marked
            out["message"] = (
                "发现疑似冲突，两条都保留、都参与检索（未标取代、未删除、未改写）。"
                "要裁决请用 memory_conflicts 看两边全文，再对确定作废的一条显式处理。"
            )
        return out
