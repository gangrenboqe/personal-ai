"""记忆层单元测试（施工单 29 验收 5.1）。

覆盖：六命令正常/冲突路径、delete 软删、FTS5 中文检索（专测）、
空记忆不污染注入、supersede 链、迁移幂等。

运行：
    python -m unittest tests.test_memory -v
    python tests/test_memory.py          # 等价

依赖：只用标准库 unittest（与"零第三方依赖"一致）。
"""

from __future__ import annotations

import gc
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from personal_ai.config import Config
from personal_ai.core.chat import ChatService
from personal_ai.memory import (
    MemoryCommandError,
    MemoryStore,
    build_context_block,
    ensure_tables,
)
from scripts.migrate_memory import main as migrate_main


def _new_store() -> tuple[MemoryStore, str]:
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return MemoryStore(db), db


class TempDB(unittest.TestCase):
    """每个用例一份独立临时库，互不干扰。"""

    def setUp(self) -> None:
        self.store, self.db = _new_store()

    def tearDown(self) -> None:
        # Windows 上 sqlite 连接惰性释放，显式回收避免清理失败盖住断言
        gc.collect()
        for _ in range(3):
            try:
                os.unlink(self.db)
                break
            except PermissionError:
                gc.collect()


# ---------------------------------------------------------------- create

class TestCreate(TempDB):
    def test_01_create_and_view_roundtrip(self):
        """1 create 后 view 读回全部字段。"""
        rid = self.store.create(
            "/memories/preferences/回答风格.md",
            "用户喜欢简洁的回答。",
            kind="preference",
            source="用户说的",
        )
        self.assertIsInstance(rid, int)
        rows = self.store.view("/memories/preferences/回答风格.md")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["path"], "/memories/preferences/回答风格.md")
        self.assertEqual(r["content"], "用户喜欢简洁的回答。")
        self.assertEqual(r["kind"], "preference")
        self.assertEqual(r["source"], "用户说的")
        self.assertIsNone(r["verified_at"])  # 从未核过
        self.assertIn("T", r["written_at"])  # ISO8601

    def test_02_create_duplicate_path_raises(self):
        """2 重名报错，不静默覆盖。"""
        self.store.create("/memories/fact/a.md", "A", kind="fact", source="用户说的")
        with self.assertRaises(MemoryCommandError):
            self.store.create("/memories/fact/a.md", "B", kind="fact", source="用户说的")

    def test_03_create_empty_source_raises(self):
        """3 source 必填（硬规矩 3）。"""
        with self.assertRaises(MemoryCommandError):
            self.store.create("/memories/fact/b.md", "x", kind="fact", source="")

    def test_04_create_bad_kind_raises(self):
        """4 kind 必须在 fact/preference/conclusion/skill 内。"""
        with self.assertRaises(MemoryCommandError):
            self.store.create("/memories/fact/c.md", "x", kind="note", source="用户说的")

    def test_05_create_bad_path_raises(self):
        """5 path 必须以 / 开头（对齐 Anthropic 文件路径语义）。"""
        with self.assertRaises(MemoryCommandError):
            self.store.create("memories/fact/d.md", "x", kind="fact", source="用户说的")


# ---------------------------------------------------------------- view

class TestView(TempDB):
    def setUp(self) -> None:
        super().setUp()
        self.store.create("/memories/preferences/a.md", "甲", kind="preference", source="用户说的")
        self.store.create("/memories/preferences/b.md", "乙", kind="preference", source="从对话抽取")
        self.store.create("/memories/fact/c.md", "丙", kind="fact", source="从文件导入")

    def test_06_view_exact_single(self):
        """6 精确路径返回单条。"""
        rows = self.store.view("/memories/fact/c.md")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["content"], "丙")

    def test_07_view_prefix_lists_directory(self):
        """7 前缀列目录返回多条。"""
        rows = self.store.view("/memories/preferences/")
        self.assertEqual({r["path"] for r in rows},
                         {"/memories/preferences/a.md", "/memories/preferences/b.md"})

    def test_08_view_missing_raises(self):
        """8 完全无匹配报错（路径不存在）。"""
        with self.assertRaises(MemoryCommandError):
            self.store.view("/memories/nothing/")


# ---------------------------------------------------------------- str_replace

class TestStrReplace(TempDB):
    def setUp(self) -> None:
        super().setUp()
        self.store.create(
            "/memories/fact/stack.md",
            "检索用 FTS5，存储用 SQLite，缓存用 FTS5。",
            kind="fact",
            source="用户说的",
        )

    def test_09_replace_updates_and_fts_follows(self):
        """9 替换后：view 拿到新内容，FTS 搜得到新词、搜不到旧词。"""
        self.store.str_replace("/memories/fact/stack.md", "缓存用 FTS5", "缓存无")
        content = self.store.view("/memories/fact/stack.md")[0]["content"]
        self.assertEqual(content, "检索用 FTS5，存储用 SQLite，缓存无。")
        hits_new = self.store.search("存储用")
        self.assertTrue(any(h["path"] == "/memories/fact/stack.md" for h in hits_new))

    def test_10_replace_ambiguous_raises(self):
        """10 old 不唯一报错（出现两次的 FTS5）。"""
        with self.assertRaises(MemoryCommandError):
            self.store.str_replace("/memories/fact/stack.md", "FTS5", "X")

    def test_11_replace_missing_old_raises(self):
        """11 old 不存在报错。"""
        with self.assertRaises(MemoryCommandError):
            self.store.str_replace("/memories/fact/stack.md", "不存在的词", "X")

    def test_12_replace_missing_path_raises(self):
        """12 路径不存在报错。"""
        with self.assertRaises(MemoryCommandError):
            self.store.str_replace("/memories/fact/none.md", "a", "b")


# ---------------------------------------------------------------- insert

class TestInsert(TempDB):
    def setUp(self) -> None:
        super().setUp()
        self.store.create("/memories/skill/steps.md", "第一行\n第二行", kind="skill", source="用户说的")

    def test_13_insert_after_line(self):
        """13 after=1 插在第 1 行后。"""
        self.store.insert("/memories/skill/steps.md", "插队", after=1)
        self.assertEqual(
            self.store.view("/memories/skill/steps.md")[0]["content"],
            "第一行\n插队\n第二行",
        )

    def test_14_insert_at_front(self):
        """14 after=None 插在最前。"""
        self.store.insert("/memories/skill/steps.md", "开头")
        self.assertEqual(
            self.store.view("/memories/skill/steps.md")[0]["content"],
            "开头\n第一行\n第二行",
        )

    def test_15_insert_out_of_range_raises(self):
        """15 after 超行数报错。"""
        with self.assertRaises(MemoryCommandError):
            self.store.insert("/memories/skill/steps.md", "x", after=99)

    def test_16_insert_missing_path_raises(self):
        """16 路径不存在报错。"""
        with self.assertRaises(MemoryCommandError):
            self.store.insert("/memories/skill/none.md", "x", after=1)


# ---------------------------------------------------------------- delete（软删）

class TestSoftDelete(TempDB):
    def setUp(self) -> None:
        super().setUp()
        self.store.create("/memories/fact/old.md", "旧事实：端口是 20128", kind="fact", source="用户说的")

    def test_17_deleted_invisible_in_view(self):
        """17 删完 view 查不到（验收 5.1）。"""
        self.store.delete("/memories/fact/old.md")
        with self.assertRaises(MemoryCommandError):
            self.store.view("/memories/fact/old.md")

    def test_18_deleted_visible_in_tombstones(self):
        """18 tombstone 能查到（验收 5.1：不物理删）。"""
        self.store.delete("/memories/fact/old.md")
        tombs = self.store.tombstones()
        self.assertEqual(len(tombs), 1)
        self.assertEqual(tombs[0]["path"], "/memories/fact/old.md")
        self.assertIsNotNone(tombs[0]["deleted_at"])
        self.assertIn("端口是 20128", tombs[0]["content"])  # 内容原样保留

    def test_19_deleted_not_in_search(self):
        """19 删完 search 不再命中。"""
        self.store.delete("/memories/fact/old.md")
        self.assertEqual(self.store.search("端口"), [])

    def test_20_deleted_not_in_directory(self):
        """20 删完目录列表不含。"""
        self.store.delete("/memories/fact/old.md")
        with self.assertRaises(MemoryCommandError):
            self.store.view("/memories/fact/")

    def test_21_double_delete_raises(self):
        """21 已删再删报错。"""
        self.store.delete("/memories/fact/old.md")
        with self.assertRaises(MemoryCommandError):
            self.store.delete("/memories/fact/old.md")


# ---------------------------------------------------------------- rename

class TestRename(TempDB):
    def setUp(self) -> None:
        super().setUp()
        self.store.create("/memories/fact/old_name.md", "记忆迁移测试内容", kind="fact", source="用户说的")

    def test_22_rename_moves_content(self):
        """22 改路径后旧查不到、新可读、search 仍命中内容。"""
        self.store.rename("/memories/fact/old_name.md", "/memories/fact/new_name.md")
        with self.assertRaises(MemoryCommandError):
            self.store.view("/memories/fact/old_name.md")
        self.assertEqual(
            self.store.view("/memories/fact/new_name.md")[0]["content"],
            "记忆迁移测试内容",
        )
        self.assertTrue(any(h["path"] == "/memories/fact/new_name.md" for h in self.store.search("记忆迁移")))

    def test_23_rename_to_existing_raises(self):
        """23 新路径已被占用报错。"""
        self.store.create("/memories/fact/taken.md", "占位", kind="fact", source="用户说的")
        with self.assertRaises(MemoryCommandError):
            self.store.rename("/memories/fact/old_name.md", "/memories/fact/taken.md")

    def test_24_rename_missing_old_raises(self):
        """24 旧路径不存在报错。"""
        with self.assertRaises(MemoryCommandError):
            self.store.rename("/memories/fact/none.md", "/memories/fact/x.md")


# ---------------------------------------------------------------- search（中文专测）

class TestSearch(TempDB):
    def setUp(self) -> None:
        super().setUp()
        self.store.create(
            "/memories/preferences/回答风格.md",
            "用户偏好详细的回答，要带代码示例。",
            kind="preference",
            source="用户说的",
        )
        self.store.create(
            "/memories/fact/project.md",
            "项目用 FTS5 做全文检索，零第三方依赖。",
            kind="fact",
            source="从对话抽取",
        )

    def test_25_chinese_match_3plus_chars(self):
        """25 中文 ≥3 字命中（施工单要求专测中文）。"""
        hits = self.store.search("回答要带代码示例")
        self.assertTrue(any(h["path"] == "/memories/preferences/回答风格.md" for h in hits))

    def test_26_chinese_2char_falls_back_to_like(self):
        """26 中文 2 字走 LIKE 兜底也能命中。"""
        hits = self.store.search("偏好")
        self.assertTrue(any(h["path"] == "/memories/preferences/回答风格.md" for h in hits))

    def test_27_english_match(self):
        """27 英文命中。"""
        hits = self.store.search("FTS5")
        self.assertTrue(any(h["path"] == "/memories/fact/project.md" for h in hits))

    def test_28_search_limit(self):
        """28 limit 生效。"""
        for i in range(4):
            self.store.create(
                f"/memories/fact/many{i}.md",
                f"部署相关条目 {i}",
                kind="fact",
                source="系统推断",
            )
        hits = self.store.search("部署相关条目", limit=2)
        self.assertEqual(len(hits), 2)

    def test_29_search_no_result_returns_empty(self):
        """29 无结果返回空列表（不报错、不注入）。"""
        self.assertEqual(self.store.search("完全无关的查询词"), [])

    def test_30_search_returns_metadata_and_snippet(self):
        """30 返回带命中片段与元数据。"""
        hits = self.store.search("全文检索")
        self.assertEqual(len(hits), 1)
        h = hits[0]
        self.assertIn("snippet", h)
        self.assertTrue(h["snippet"])
        for field in ("path", "kind", "source", "written_at", "verified_at"):
            self.assertIn(field, h)


# ---------------------------------------------------------------- supersede / never_verified

class TestSupersede(TempDB):
    def test_31_superseded_old_not_in_search(self):
        """31 旧条被取代后 search 只返回新条（两段评测的存储判据）。"""
        old_id = self.store.create(
            "/memories/fact/timezone.md", "用户时区在东八区。", kind="fact", source="用户说的"
        )
        new_id = self.store.create(
            "/memories/fact/timezone_v2.md", "用户时区是东一区。", kind="fact", source="用户说的"
        )
        self.store.mark_superseded("/memories/fact/timezone.md", new_id)
        hits = self.store.search("时区")
        self.assertEqual([h["id"] for h in hits], [new_id])
        # 旧条还在库里（不物理删），superseded_by 指向新条
        with_temp = self.store.tombstones()  # tombstones 只含软删；旧条未删
        self.assertEqual(with_temp, [])
        rows = self.store.view("/memories/fact/")
        by_path = {r["path"]: r for r in rows}
        self.assertEqual(by_path["/memories/fact/timezone.md"]["superseded_by"], new_id)
        self.assertIsNone(by_path["/memories/fact/timezone_v2.md"]["superseded_by"])
        self.assertEqual(old_id, 1)  # 自增 id 从 1 起（可复算）

    def test_32_never_verified_lists_unverified(self):
        """32 "从未核过"必须能查出来（硬规矩 3）。"""
        self.store.create("/memories/fact/v.md", "核过的", kind="fact", source="用户说的",
                          verified_at="2026-10-29T10:00:00")
        self.store.create("/memories/fact/u.md", "没核过", kind="fact", source="用户说的")
        unverified = self.store.never_verified()
        self.assertEqual([r["path"] for r in unverified], ["/memories/fact/u.md"])


# ---------------------------------------------------------------- 迁移

class TestMigration(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self) -> None:
        gc.collect()
        for _ in range(3):
            try:
                os.unlink(self.db)
                break
            except PermissionError:
                gc.collect()

    def test_33_migrate_idempotent(self):
        """33 重复执行迁移不炸（交付物 2 的"可重复执行"）。"""
        r1 = ensure_tables(self.db)
        r2 = ensure_tables(self.db)
        self.assertEqual(r1["columns"], r2["columns"])
        self.assertEqual(r1["memories_rows"], r2["memories_rows"])

    def test_34_migrate_columns_complete(self):
        """34 列齐全（含软删落点 deleted_at）。"""
        report = ensure_tables(self.db)
        self.assertEqual(
            set(report["columns"]),
            {"id", "path", "content", "kind", "source", "written_at",
             "verified_at", "expires_hint", "superseded_by", "deleted_at"},
        )


# ---------------------------------------------------------------- 注入

def _chat_service(db: str) -> ChatService:
    config = Config(provider="mock", db_path=db)
    return ChatService(config)


class TestInjection(unittest.TestCase):
    """注入走真实 ChatService._build_messages（send 与流式共用该路径）。"""

    def setUp(self) -> None:
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.svc = _chat_service(self.db)
        self.store = MemoryStore(self.db)

    def tearDown(self) -> None:
        gc.collect()
        for _ in range(3):
            try:
                os.unlink(self.db)
                break
            except PermissionError:
                gc.collect()

    def _build(self, text: str):
        sid = self.svc.new_session()
        self.svc.prepare(sid, text)
        return self.svc.context_for(sid)

    def test_35_empty_memory_no_injection(self):
        """35 空记忆时注入为空，不污染上下文（验收 5.1）。"""
        msgs = self._build("用户喜欢什么回答风格？")
        system_msgs = [m for m in msgs if m.role == "system"]
        self.assertEqual(len(system_msgs), 1)
        self.assertNotIn("记忆层", system_msgs[0].content)

    def test_36_hit_injects_with_metadata(self):
        """36 命中时注入记忆块，先验三元组原样在场。"""
        self.store.create(
            "/memories/preferences/回答风格.md",
            "用户偏好简洁回答。",
            kind="preference",
            source="用户说的",
        )  # verified_at 留空
        msgs = self._build("用户偏好什么样的回答风格？")
        system_msgs = [m for m in msgs if m.role == "system"]
        self.assertEqual(len(system_msgs), 2)
        block = system_msgs[1].content
        for fragment in ("[记忆层命中]", "/memories/preferences/回答风格.md",
                         "来源: 用户说的", "核过: 从未核过"):
            self.assertIn(fragment, block)

    def test_37_unrelated_query_no_injection(self):
        """37 不命中就不注入（不塞空内容占上下文）。"""
        self.store.create(
            "/memories/preferences/回答风格.md", "用户偏好简洁回答。",
            kind="preference", source="用户说的",
        )
        msgs = self._build("量子力学的哥本哈根诠释是什么？")
        system_msgs = [m for m in msgs if m.role == "system"]
        self.assertEqual(len(system_msgs), 1)

    def test_38_deleted_memory_not_injected(self):
        """38 软删后不再注入。"""
        self.store.create(
            "/memories/fact/过期事实.md", "部署端口是 20128。",
            kind="fact", source="用户说的",
        )
        self.store.delete("/memories/fact/过期事实.md")
        msgs = self._build("部署端口是多少？")
        system_msgs = [m for m in msgs if m.role == "system"]
        self.assertEqual(len(system_msgs), 1)


# ---------------------------------------------------------------- 注入块纯函数

class TestContextBlock(unittest.TestCase):
    def test_39_empty_hits_return_empty_string(self):
        """39 build_context_block 空命中返回空串。"""
        self.assertEqual(build_context_block([]), "")

    def test_40_block_carries_triple_prior(self):
        """40 注入块带 source/written_at/verified_at 三元组，无裁决话术。"""
        block = build_context_block([
            {"path": "/memories/fact/a.md", "kind": "fact", "source": "系统推断",
             "written_at": "2026-10-01T08:00:00", "verified_at": None,
             "snippet": "旧结论。", "expires_hint": "30 天后需复核"},
        ])
        for fragment in ("来源: 系统推断", "写入: 2026-10-01", "核过: 从未核过",
                         "过期提示: 30 天后需复核"):
            self.assertIn(fragment, block)


# ---------------------------------------------------------------- 长内容注入（返工单 30）

class TestLongContentInjection(TempDB):
    """返工单 30 的核心回归：注入用完整 content，限长按字符，答案词不被截断。"""

    def test_42_long_content_answer_at_end_injected(self):
        """42 内容 >36 字符、答案词在最末——必须完整进入注入块（老 bug 回归）。"""
        long_content = (
            "项目检索层经过两轮评审，最终确定采用 SQLite 全文索引方案，"
            "实现上选择 FTS5 内置的 trigram 分词模式，检索关键词：trigram"
        )
        self.assertGreater(len(long_content), 36)  # 场景本身必须长于老截断线
        self.store.create("/memories/conclusions/检索.md", long_content,
                          kind="conclusion", source="用户说的")
        block = self.store.get_context_block("项目检索层最后定了什么方案？")
        self.assertIn("trigram", block)  # 答案词在内容最末，截断 bug 下会被切成 "tri"

    def test_43_entry_clipped_at_200_chars(self):
        """43 每条限长 200 字符：超长内容被截断且带省略号。"""
        long_content = "这是一条很长的中文记忆。" * 25  # 300 字符
        self.store.create("/memories/fact/长文.md", long_content,
                          kind="fact", source="用户说的")
        block = self.store.get_context_block("很长的中文记忆")
        self.assertIn("…", block)
        content_line = next(l for l in block.split("\n") if l.startswith("   内容: "))
        body = content_line[len("   内容: "):]
        self.assertLessEqual(len(body), 201)  # 200 + 省略号
        self.assertGreater(len(body), 150)   # 不是被 snippet 截成十几字

    def test_44_clip_does_not_break_english_words(self):
        """44 英文按词边界回退：截断处不落在单词中间。"""
        text = "x " * 95 + "extraordinary terminology"  # 200 边界落在长词中间
        block = build_context_block([
            {"path": "/memories/fact/e.md", "kind": "fact", "source": "用户说的",
             "written_at": "2026-10-29T00:00:00", "verified_at": None, "content": text},
        ])
        body = next(l for l in block.split("\n") if l.startswith("   内容: "))
        body = body[len("   内容: "):].rstrip("…")
        self.assertNotIn("extraordina", body)  # 半个单词没进注入块

    def test_45_block_capped_at_1500(self):
        """45 整块兜底限长 1500 字符。"""
        from personal_ai.memory import BLOCK_MAX_CHARS
        hits = [
            {"path": f"/memories/fact/m{i}.md", "kind": "fact", "source": "用户说的",
             "written_at": "2026-10-29T00:00:00", "verified_at": None,
             "content": "很长的记忆内容。" * 30}
            for i in range(6)
        ]
        block = build_context_block(hits)
        self.assertLessEqual(len(block), BLOCK_MAX_CHARS + 1)  # +1 是省略号


# ---------------------------------------------------------------- CLI 壳可导入可运行

class TestMigrateCLI(unittest.TestCase):
    def test_41_migrate_cli_runs(self):
        """41 python scripts/migrate_memory.py --db 临时库 正常退出。"""
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        sys.argv = ["migrate_memory.py", "--db", self.db]
        try:
            self.assertEqual(migrate_main(), 0)
        finally:
            sys.argv = []
            gc.collect()
            for _ in range(3):
                try:
                    os.unlink(self.db)
                    break
                except PermissionError:
                    gc.collect()


if __name__ == "__main__":
    unittest.main(verbosity=2)
