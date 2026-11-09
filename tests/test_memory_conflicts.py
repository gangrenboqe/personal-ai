"""43 号：冲突检测 + 淡忘不删 + memory_conflicts 工具 的单元测试。

样本全部现造于临时库（不用真实记忆内容）。最重要的分组：
- 三组真冲突（SQLite/Postgres、tab/空格、推荐/不推荐）必须检出；
- 「相似但不冲突」（同主题补充说明、迁移记录、跨 kind）不许误报
  ——乱报一次，人就不再信这个功能了；
- 淡忘：老记忆降权（score 0.7）但仍能搜到、内容一字不动；verify 后权重恢复。
"""

from __future__ import annotations

import gc
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from personal_ai.memory import (  # noqa: E402
    FADE_DAYS,
    FADE_WEIGHT,
    MemoryStore,
    MemoryToolExecutor,
    detect_conflicts,
)


def _new() -> tuple[MemoryToolExecutor, MemoryStore, str]:
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = MemoryStore(db)
    return MemoryToolExecutor(store), store, db


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.exec_, self.store, self.db = _new()

    def tearDown(self) -> None:
        # Windows 上 sqlite 连接惰性释放，显式回收避免清理失败盖住断言
        gc.collect()
        for _ in range(3):
            try:
                os.unlink(self.db)
                break
            except PermissionError:
                gc.collect()

    def _create(self, kind: str, title: str, content: str) -> dict:
        return self.exec_.execute(
            "memory_create", {"kind": kind, "title": title, "content": content}
        )

    def _set_times(self, path: str, *, written_days_ago: int,
                   verified_days_ago: int | None) -> None:
        """把某条的时间拨回指定天数前（人工构造老记忆；verified=None 表示从未核过）。"""
        written = (datetime.now() - timedelta(days=written_days_ago)).isoformat(timespec="seconds")
        verified = (
            (datetime.now() - timedelta(days=verified_days_ago)).isoformat(timespec="seconds")
            if verified_days_ago is not None else None
        )
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "UPDATE memories SET written_at = ?, verified_at = ? WHERE path = ?",
                (written, verified, path),
            )


class TestConflictDetection(Base):
    # ---------------- 三组真冲突：必须检出 ----------------

    def test_01_sqlite_vs_postgres_detected(self):
        """工单样本①：数据库选型互斥。**只提示、不标取代**，两条都保留且都能搜到。"""
        self._create("conclusion", "数据库选型", "本项目数据库统一用 SQLite。")
        r2 = self._create("conclusion", "数据库选型变更", "数据库改用 Postgres。")
        self.assertTrue(r2["ok"], r2)
        self.assertIn("conflicts", r2)
        self.assertIn("两条都保留", r2.get("message", ""))
        old = self.store.view("/memories/conclusion/数据库选型.md")[0]
        # 疑似冲突不标取代：标记会让 search 把旧条排除，等于没人裁决就藏掉一种说法
        self.assertIsNone(old["superseded_by"])
        both = self.store.view("/memories/conclusion/")
        self.assertEqual(len(both), 2)  # 两条都还在
        # 关键：检索里两条都要出现，否则调用方不知道存在另一种说法
        hits = self.store.search("数据库", limit=10)
        self.assertEqual(len(hits), 2, [h["path"] for h in hits])
        reasons = " ".join(r2["conflicts"][0]["reasons"])
        self.assertIn("取值相反", reasons)

    def test_02_tab_vs_space_detected(self):
        """工单样本②：缩进风格互斥。"""
        self._create("preference", "缩进偏好", "代码缩进用 tab。")
        r2 = self._create("preference", "缩进调整", "缩进统一用 4 个空格。")
        self.assertIn("conflicts", r2)
        reasons = " ".join(r2["conflicts"][0]["reasons"])
        self.assertIn("取值相反", reasons)
        self.assertIn("缩进风格", reasons)

    def test_03_recommend_vs_not_recommend_detected(self):
        """工单样本③：推荐/不推荐同一实体。"""
        self._create("conclusion", "公司推荐", "推荐 A 公司：离家近。")
        r2 = self._create("conclusion", "公司推荐复核", "不推荐 A 公司：岗位不匹配。")
        self.assertIn("conflicts", r2)
        reasons = " ".join(r2["conflicts"][0]["reasons"])
        self.assertIn("推荐冲突", reasons)

    # ---------------- 相似但不冲突：不许误报（最重要的一组） ----------------

    def test_04_supplement_same_value_no_conflict(self):
        """同主题的补充说明（同取值、给理由）不是冲突。"""
        self._create("conclusion", "数据库选型", "本项目数据库统一用 SQLite。")
        r2 = self._create(
            "conclusion", "数据库选型补充理由",
            "选 SQLite 的补充理由：零配置、单文件，备份就是复制一份文件。")
        self.assertNotIn("conflicts", r2)
        old = self.store.view("/memories/conclusion/数据库选型.md")[0]
        self.assertIsNone(old["superseded_by"])  # 没被误标

    def test_05_supplement_recommend_no_conflict(self):
        """同实体的两次正向推荐（补充理由）不是冲突。"""
        self._create("conclusion", "公司推荐", "推荐 A 公司：离家近。")
        r2 = self._create(
            "conclusion", "公司推荐补充",
            "推荐 A 公司的补充理由：岗位方向与他的 AI 主线一致。")
        self.assertNotIn("conflicts", r2)

    def test_06_migration_mention_no_conflict(self):
        """迁移/评估记录同时提到两个值，但没有「改用」的断言——不许误报。"""
        self._create("conclusion", "数据库选型", "本项目数据库统一用 SQLite。")
        r2 = self._create(
            "conclusion", "迁移评估",
            "数据库从 SQLite 迁移到 Postgres 的成本评估过，暂不执行。")
        self.assertNotIn("conflicts", r2)

    def test_07_cross_kind_no_conflict(self):
        """冲突只判同 kind：不同 kind 的相同表述不判。"""
        self._create("conclusion", "数据库选型", "本项目数据库统一用 SQLite。")
        r2 = self._create("preference", "数据库偏好", "数据库改用 Postgres。")
        self.assertNotIn("conflicts", r2)

    def test_08_two_negatives_no_conflict(self):
        """两条都是「不推荐」——不构成推荐/不推荐对立。"""
        self._create("conclusion", "公司推荐", "不推荐 A 公司：岗位不匹配。")
        r2 = self._create("conclusion", "公司推荐复核", "别推荐 A 公司：待遇也一般。")
        self.assertNotIn("conflicts", r2)

    def test_09_detector_pure_function_direction_free(self):
        """detect_conflicts 纯函数：方向无关；无关正文返回空。"""
        a = "本项目数据库统一用 SQLite。"
        b = "数据库改用 Postgres。"
        self.assertTrue(detect_conflicts(a, b))
        self.assertTrue(detect_conflicts(b, a))
        self.assertEqual(detect_conflicts(a, "今天天气不错。"), [])
        self.assertEqual(detect_conflicts("", ""), [])


class TestConflictsTool(Base):
    def test_10_conflicts_tool_lists_full_text_and_no_adjudication(self):
        """memory_conflicts：返回双方完整原文 + 时间 + 理由；列出前后库没被动过。"""
        self._create("conclusion", "数据库选型", "本项目数据库统一用 SQLite。")
        self._create("conclusion", "数据库选型变更", "数据库改用 Postgres。")
        before = self.store.view("/memories/conclusion/")
        r = self.exec_.execute("memory_conflicts", {})
        self.assertTrue(r["ok"])
        self.assertEqual(r["count"], 1)
        pair = r["items"][0]
        self.assertEqual(pair["a"]["content"], "本项目数据库统一用 SQLite。")  # 原文全文
        self.assertEqual(pair["b"]["content"], "数据库改用 Postgres。")
        self.assertTrue(pair["a"]["written_at"] and pair["b"]["written_at"])
        self.assertTrue(any("取值相反" in x for x in pair["reasons"]))
        self.assertIn("不替你作废", pair["note"])
        after = self.store.view("/memories/conclusion/")
        self.assertEqual(
            [(x["path"], x["content"], x["superseded_by"]) for x in before],
            [(x["path"], x["content"], x["superseded_by"]) for x in after],
        )  # 只列不裁：列出这个动作本身没有改任何一条

    def test_11_conflicts_tool_empty_when_none(self):
        self._create("preference", "缩进偏好", "代码缩进用 tab。")
        r = self.exec_.execute("memory_conflicts", {})
        self.assertTrue(r["ok"])
        self.assertEqual(r["count"], 0)


class TestFading(Base):
    def _seed_pair(self) -> tuple[str, str]:
        """一新一旧：旧条 100 天前核过一次后再没核过（淡忘），新条 90 天内写入。"""
        old_r = self._create("fact", "默认方式", "本机部署是默认方式。")
        new_r = self._create("fact", "上云备选", "云部署是备选方案。")
        self._set_times("/memories/fact/默认方式.md",
                        written_days_ago=FADE_DAYS + 11, verified_days_ago=FADE_DAYS + 10)
        self._set_times("/memories/fact/上云备选.md",
                        written_days_ago=FADE_DAYS - 10, verified_days_ago=None)
        return old_r["path"], new_r["path"]

    def test_12_fade_demotes_but_still_found(self):
        """旧条命中分乘 0.7 往后放，但仍能搜到、内容一字不动。"""
        old_path, new_path = self._seed_pair()
        old_content = self.store.view(old_path)[0]["content"]
        hits = self.store.search("部署", limit=10)
        paths = [h["path"] for h in hits]
        self.assertIn(old_path, paths)   # 绝不隐藏：仍然搜得到
        self.assertIn(new_path, paths)
        by = {h["path"]: h for h in hits}
        self.assertTrue(by[old_path]["faded"])
        self.assertFalse(by[new_path]["faded"])
        # LIKE 路径分数可见：两边各命中 1 次，旧条 1×0.7=0.7 < 新条 1.0
        self.assertAlmostEqual(by[old_path]["score"], FADE_WEIGHT, places=3)
        self.assertAlmostEqual(by[new_path]["score"], 1.0, places=3)
        self.assertGreater(paths.index(old_path), paths.index(new_path))  # 往后放
        self.assertEqual(self.store.view(old_path)[0]["content"], old_content)  # 内容一字未动

    def test_13_verify_restores_weight(self):
        """「这条我确认过了」：verified_at 刷新 → 降权解除（score 回 1.0），仍不删不改。"""
        old_path, _new_path = self._seed_pair()
        self.exec_.execute("memory_verify", {"path": old_path})
        hits = self.store.search("部署", limit=10)
        by = {h["path"]: h for h in hits}
        self.assertIn(old_path, by)                      # 还是搜得到
        self.assertFalse(by[old_path]["faded"])          # 降权解除
        self.assertAlmostEqual(by[old_path]["score"], 1.0, places=3)
        row = self.store.view(old_path)[0]
        self.assertIsNotNone(row["verified_at"])
        self.assertEqual(row["content"], "本机部署是默认方式。")  # 内容没动

    def test_14_fade_on_fts_path_flags(self):
        """3 字以上查询走 FTS5 路径：淡忘条照样返回且带 faded 标记。"""
        self._create("fact", "部署方式", "部署方式用的是本机部署。")
        self._create("fact", "部署方式补充", "部署方式的入口在 scripts 目录。")
        self._set_times("/memories/fact/部署方式.md",
                        written_days_ago=FADE_DAYS + 11, verified_days_ago=None)
        hits = self.store.search("部署方式", limit=10)
        by = {h["path"]: h for h in hits}
        self.assertIn("/memories/fact/部署方式.md", by)
        self.assertTrue(by["/memories/fact/部署方式.md"]["faded"])
        self.assertFalse(by["/memories/fact/部署方式补充.md"]["faded"])

    def test_15_context_block_mentions_fade(self):
        """注入块对淡忘条目给出「仅供参考」提示，不替模型裁决。"""
        self._create("fact", "部署方式", "部署方式用的是本机部署。")
        self._set_times("/memories/fact/部署方式.md",
                        written_days_ago=FADE_DAYS + 11, verified_days_ago=None)
        block = self.store.get_context_block("部署方式", limit=5)
        self.assertIn("淡忘", block)
        self.assertIn("未删除未隐藏", block)

    def test_16_verify_unknown_path_errors(self):
        r = self.exec_.execute("memory_verify", {"path": "/memories/fact/不存在.md"})
        self.assertFalse(r["ok"])
        self.assertIn("不存在", r["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
