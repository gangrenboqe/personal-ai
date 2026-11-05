"""记忆层 v2 工具调用单元测试（施工单 32 · S1）。

覆盖：工具 schema 规范（create 只留 kind/title/content，path/source 由代码生成）、
slug 清洗、执行器三命令 + 错误回填、tool_calls 响应解析、supports_tools 隔离。

运行：
    python -m unittest tests.test_memory_tools -v
依赖：只用标准库 unittest。不触碰 29/30 号的既有测试。
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

from personal_ai.memory import (
    MEMORY_SOURCE_AUTO,
    MemoryStore,
    MemoryToolExecutor,
    _slugify,
    memory_tools_schema,
)
from personal_ai.providers.base import ChatResponse, ProviderBase
from personal_ai.providers.mock import MockProvider
from personal_ai.providers.openai_compat import (
    OpenAICompatProvider,
    parse_chat_response_body,
)


def _new_executor() -> tuple[MemoryToolExecutor, str]:
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return MemoryToolExecutor(MemoryStore(db)), db


class TempDB(unittest.TestCase):
    def setUp(self) -> None:
        self.exec_, self.db = _new_executor()

    def tearDown(self) -> None:
        gc.collect()
        for _ in range(3):
            try:
                os.unlink(self.db)
                break
            except PermissionError:
                gc.collect()


# ---------------------------------------------------------------- schema

class TestSchema(unittest.TestCase):
    def setUp(self) -> None:
        self.tools = {t["function"]["name"]: t["function"] for t in memory_tools_schema()}

    def test_01_three_tools(self):
        """1 工具清单：create / view / search / history_search（38 号任务 4 新增第四个，断言由三改四）。"""
        self.assertEqual(set(self.tools), {"memory_create", "memory_view", "memory_search", "history_search"})

    def test_02_create_params_exactly_kind_title_content(self):
        """2 create 参数恰好 kind/title/content——path 与 source 必须不在 schema 里（设计者修正）。"""
        props = set(self.tools["memory_create"]["parameters"]["properties"])
        self.assertEqual(props, {"kind", "title", "content"})
        self.assertEqual(set(self.tools["memory_create"]["parameters"]["required"]),
                         {"kind", "title", "content"})

    def test_03_create_kind_enum(self):
        """3 kind 限定四值域。"""
        self.assertEqual(self.tools["memory_create"]["parameters"]["properties"]["kind"]["enum"],
                         ["fact", "preference", "conclusion", "skill"])

    def test_04_openai_format(self):
        """4 schema 是 OpenAI function 格式（type=function，参数为 JSON Schema object）。"""
        for t in memory_tools_schema():
            self.assertEqual(t["type"], "function")
            self.assertEqual(t["function"]["parameters"]["type"], "object")


# ---------------------------------------------------------------- slug

class TestSlug(unittest.TestCase):
    def test_05_slug_keeps_chinese(self):
        """5 中文保留。"""
        self.assertEqual(_slugify("回答风格偏好"), "回答风格偏好")

    def test_06_slug_replaces_illegal(self):
        """6 空白与路径非法字符转 -。"""
        self.assertEqual(_slugify("编程 偏好/代码:格式化"), "编程-偏好-代码-格式化")

    def test_07_slug_empty_fallback(self):
        """7 空 title 兜底 note。"""
        self.assertEqual(_slugify(""), "note")
        self.assertEqual(_slugify("///"), "note")

    def test_08_slug_length_cap(self):
        """8 超长截断到 40。"""
        self.assertLessEqual(len(_slugify("长" * 100)), 40)


# ---------------------------------------------------------------- 执行器

class TestExecutorCreate(TempDB):
    def test_09_create_generates_path_and_fixed_source(self):
        """9 path 由代码生成、source 固定"模型自主写入"（审计依据）。"""
        r = self.exec_.execute("memory_create", {
            "kind": "preference", "title": "回答风格", "content": "用户偏好简洁回答。",
        })
        self.assertTrue(r["ok"])
        self.assertEqual(r["path"], "/memories/preference/回答风格.md")
        rows = self.exec_.store.view(r["path"])
        self.assertEqual(rows[0]["source"], MEMORY_SOURCE_AUTO)
        self.assertEqual(rows[0]["content"], "用户偏好简洁回答。")

    def test_10_create_duplicate_returns_error_not_raise(self):
        """10 重名不抛异常，回填 ok=False（模型自行决定下一步）。"""
        args = {"kind": "fact", "title": "端口", "content": "端口是 20128。"}
        self.assertTrue(self.exec_.execute("memory_create", args)["ok"])
        r2 = self.exec_.execute("memory_create", args)
        self.assertFalse(r2["ok"])
        self.assertIn("已存在", r2["error"])

    def test_11_create_bad_kind(self):
        """11 kind 非法值回填错误。"""
        r = self.exec_.execute("memory_create", {"kind": "note", "title": "x", "content": "y"})
        self.assertFalse(r["ok"])
        self.assertIn("kind", r["error"])

    def test_12_create_missing_content(self):
        """12 缺 content 回填错误。"""
        r = self.exec_.execute("memory_create", {"kind": "fact", "title": "x", "content": ""})
        self.assertFalse(r["ok"])
        self.assertIn("content", r["error"])


class TestExecutorQuery(TempDB):
    def test_13_view_single_and_missing(self):
        """13 view 精确命中；路径不存在回填错误不抛。"""
        r = self.exec_.execute("memory_create",
                               {"kind": "fact", "title": "时区", "content": "用户时区是 UTC+1。"})
        v = self.exec_.execute("memory_view", {"path": r["path"]})
        self.assertTrue(v["ok"]) and self.assertEqual(v["count"], 1)
        miss = self.exec_.execute("memory_view", {"path": "/memories/fact/none.md"})
        self.assertFalse(miss["ok"])

    def test_14_search_hits_created_memory(self):
        """14 search 能检索到 create 写入的条目（写入→检索闭环）。"""
        self.exec_.execute("memory_create",
                           {"kind": "conclusion", "title": "检索方案", "content": "项目检索层定为 FTS5 trigram。"})
        s = self.exec_.execute("memory_search", {"query": "检索层定的什么方案"})
        self.assertTrue(s["ok"])
        self.assertTrue(any("trigram" in it["content"] for it in s["items"]))

    def test_15_unknown_tool(self):
        """15 未知工具名回填错误。"""
        r = self.exec_.execute("memory_delete", {})
        self.assertFalse(r["ok"])
        self.assertIn("未知工具", r["error"])


# ---------------------------------------------------------------- 响应解析

class TestParseResponse(unittest.TestCase):
    def test_16_parse_tool_calls(self):
        """16 OpenAI tool_calls 响应解析：name/arguments 转 dict。"""
        body = {"choices": [{"message": {
            "content": None,
            "tool_calls": [{"id": "call_1", "type": "function", "function": {
                "name": "memory_create",
                "arguments": '{"kind": "preference", "title": "回答风格", "content": "简洁"}',
            }}],
        }}]}
        content, calls = parse_chat_response_body(body)
        self.assertEqual(content, "")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "memory_create")
        self.assertEqual(calls[0]["arguments"]["kind"], "preference")
        self.assertEqual(calls[0]["id"], "call_1")

    def test_17_parse_bad_arguments_falls_back(self):
        """17 arguments 坏 JSON 兜底 {}，不拖垮整轮。"""
        body = {"choices": [{"message": {"content": "x", "tool_calls": [
            {"id": "c", "function": {"name": "memory_view", "arguments": "{broken"}},
        ]}}]}
        _, calls = parse_chat_response_body(body)
        self.assertEqual(calls[0]["arguments"], {})

    def test_18_parse_plain_content(self):
        """18 普通文本响应：tool_calls 为空列表。"""
        content, calls = parse_chat_response_body(
            {"choices": [{"message": {"content": "你好"}}]})
        self.assertEqual(content, "你好")
        self.assertEqual(calls, [])


# ---------------------------------------------------------------- supports_tools 隔离

class TestSupportsTools(unittest.TestCase):
    def test_19_isolation_flags(self):
        """19 能力标记：base False / mock False / openai_compat True（ChatService 据此决定是否传 tools）。"""
        self.assertFalse(ProviderBase.supports_tools)
        self.assertFalse(MockProvider.supports_tools)
        self.assertTrue(OpenAICompatProvider.supports_tools)

    def test_20_chat_response_default_none(self):
        """20 ChatResponse.tool_calls 默认 None——不传 tools 的旧调用零破坏。"""
        r = ChatResponse(content="x", provider="mock", model="m")
        self.assertIsNone(r.tool_calls)


# ---------------------------------------------------------------- agent loop（S2）

from personal_ai.config import Config  # noqa: E402
from personal_ai.core.chat import ChatService  # noqa: E402
from personal_ai.providers.base import ChatMessage  # noqa: E402


class _FakeToolProvider(ProviderBase):
    """脚本化 provider：第一次返回 tool_calls，之后返回固定文本，记录每次收到的消息。"""

    name = "fake_tool"
    supports_tools = True
    default_chain = lambda self: ["fake"]  # noqa: E731

    def __init__(self, config, script):
        super().__init__(config)
        self.script = list(script)   # 依次弹出的 ChatResponse
        self.seen: list[list[ChatMessage]] = []

    def complete(self, messages, tools=None):
        self.seen.append(list(messages))
        return self.script.pop(0)

    def default_model(self):
        return "fake-model"


def _loop_service(script, db):
    config = Config(provider="mock", db_path=db)
    svc = ChatService(config)
    svc.provider = _FakeToolProvider(config, script)
    return svc


class TestAgentLoop(unittest.TestCase):
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

    def test_21_loop_backfills_tool_result(self):
        """21 有 tool_calls：执行 create → 以 assistant(tool_calls)+role=tool 回填 → 第二次调用出回答。"""
        call = {"id": "c1", "type": "function", "function": {
            "name": "memory_create",
            "arguments": '{"kind": "preference", "title": "缩进", "content": "用户用 tab 缩进"}',
        }}
        first = ChatResponse(content="", provider="fake_tool", model="m", ok=True,
                             tool_calls=[{"id": "c1", "name": "memory_create",
                                          "arguments": {"kind": "preference", "title": "缩进",
                                                        "content": "用户用 tab 缩进"},
                                          "raw": call}])
        second = ChatResponse(content="已记下你的偏好。", provider="fake_tool", model="m", ok=True)
        svc = _loop_service([first, second], self.db)
        out = svc.send("", "我习惯用 tab 缩进")
        self.assertEqual(out["content"], "已记下你的偏好。")
        # 第二次调用里应能看到 assistant(tool_calls) + role=tool 回填
        second_msgs = svc.provider.seen[1]
        self.assertEqual(second_msgs[-2].role, "assistant")
        self.assertEqual(second_msgs[-2].tool_calls[0]["function"]["name"], "memory_create")
        self.assertEqual(second_msgs[-1].role, "tool")
        self.assertEqual(second_msgs[-1].tool_call_id, "c1")
        self.assertIn('"ok": true', second_msgs[-1].content)
        # create 真的写进库了，chain 记录了工具痕迹
        hits = svc.memory.search("tab 缩进")
        self.assertTrue(hits)
        self.assertIn("memory_create", out["chain"])

    def test_22_loop_error_backfill_no_raise(self):
        """22 工具返回 {"ok":false} 当正常结果回填，不抛异常，循环继续到出回答。"""
        call = {"id": "c1", "type": "function", "function": {
            "name": "memory_create", "arguments": '{"kind": "bad", "title": "x", "content": "y"}',
        }}
        first = ChatResponse(content="", provider="fake_tool", model="m", ok=True,
                             tool_calls=[{"id": "c1", "name": "memory_create",
                                          "arguments": {"kind": "bad"}, "raw": call}])
        second = ChatResponse(content="好的。", provider="fake_tool", model="m", ok=True)
        svc = _loop_service([first, second], self.db)
        out = svc.send("", "随便说点什么")
        self.assertEqual(out["content"], "好的。")
        self.assertIn("ok=False", out["chain"])

    def test_23_loop_round_cap(self):
        """23 上限 2 轮：模型连环要工具也只回填 2 次，然后停。"""
        def _call(i):
            return {"id": f"c{i}", "type": "function", "function": {
                "name": "memory_search", "arguments": '{"query": "x"}'}}
        script = [
            ChatResponse(content="", provider="fake_tool", model="m", ok=True,
                         tool_calls=[{"id": "c1", "name": "memory_search", "arguments": {}, "raw": _call(1)}]),
            ChatResponse(content="", provider="fake_tool", model="m", ok=True,
                         tool_calls=[{"id": "c2", "name": "memory_search", "arguments": {}, "raw": _call(2)}]),
            ChatResponse(content="", provider="fake_tool", model="m", ok=True,
                         tool_calls=[{"id": "c3", "name": "memory_search", "arguments": {}, "raw": _call(3)}]),
        ]
        svc = _loop_service(script, self.db)
        svc.send("", "一直要工具")
        self.assertEqual(len(svc.provider.seen), 3)  # 初始 + 2 轮回填，不再继续


# ---------------------------------------------------------------- history_search（38 任务 4）

from personal_ai.storage import add_message, create_session, init_db  # noqa: E402


class TestHistorySearch(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        init_db(self.db)  # 建 messages / sessions（ChatService 同款顺序）
        self.exec_ = MemoryToolExecutor(MemoryStore(self.db))

    def _seed(self, title: str, user_text: str, assistant_text: str = "好的。") -> None:
        sid = create_session(self.db, title=title)
        add_message(self.db, sid, "user", user_text)
        add_message(self.db, sid, "assistant", assistant_text)

    def tearDown(self) -> None:
        gc.collect()
        for _ in range(3):
            try:
                os.unlink(self.db)
                break
            except PermissionError:
                gc.collect()

    def test_24_hit_with_provenance(self):
        """24 命中：带会话名 / 角色 / 时间 / 片段原文（出处三件套）。"""
        self._seed("数据库讨论", "我们定了：用 SQLite，不上 Postgres。")
        r = self.exec_.execute("history_search", {"query": "数据库选型 SQLite"})
        self.assertTrue(r["ok"])
        self.assertGreaterEqual(r["count"], 1)
        top = r["items"][0]
        self.assertEqual(top["session"], "数据库讨论")
        self.assertIn(top["role"], ("user", "assistant"))
        self.assertIn("SQLite", top["snippet"])
        self.assertNotEqual(top["time"], "时间未知")

    def test_25_two_char_query_like_fallback(self):
        """25 2 字词走 LIKE 兜底（与记忆层同策略）。"""
        self._seed("数据库讨论", "我们定了：用 SQLite，不上 Postgres。")
        r = self.exec_.execute("history_search", {"query": "定了"})
        self.assertTrue(r["ok"])
        self.assertGreaterEqual(r["count"], 1)

    def test_26_no_hit_returns_empty_ok(self):
        """26 检索不到返回 {"ok": True, "count": 0}——模型据此如实说没有记录。"""
        self._seed("数据库讨论", "我们定了：用 SQLite，不上 Postgres。")
        r = self.exec_.execute("history_search", {"query": "量子迷宫完全无关词"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["count"], 0)
        self.assertEqual(r["items"], [])

    def test_27_backfill_covers_existing_messages(self):
        """27 存量回填：messages 先于 MemoryStore 存在时，ensure_tables 一次性补索引。"""
        self._seed("更早的会话", "历史里提过 trigram 分词方案。")
        r = self.exec_.execute("history_search", {"query": "trigram 分词方案"})
        self.assertTrue(r["ok"])
        self.assertGreaterEqual(r["count"], 1)

    def test_28_role_filter_excludes_tool_noise(self):
        """28 只检索 user/assistant——mock 噪声与工具轮不进结果。"""
        self._seed("正常会话", "聊一下部署端口。")
        r = self.exec_.execute("history_search", {"query": "部署端口"})
        self.assertTrue(all(i["role"] in ("user", "assistant") for i in r["items"]))


class TestSchemaTexts(unittest.TestCase):
    """38 号任务 1/2/3 的提示文本落地检查（判据/排除规则/时机指引）。"""

    def setUp(self) -> None:
        self.tools = {t["function"]["name"]: t["function"] for t in memory_tools_schema()}

    def test_29_kind_description_has_priority_rule(self):
        """29 kind description 含顺序判据与保守兜底（任务 1）。"""
        d = self.tools["memory_create"]["parameters"]["properties"]["kind"]["description"]
        for fragment in ("命中即停", "判不出来就填 fact", "仍然是 fact"):
            self.assertIn(fragment, d)

    def test_30_create_description_has_exclusion_rules(self):
        """30 create description 含短期提醒排除规则（任务 2）。"""
        d = self.tools["memory_create"]["description"]
        for fragment in ("不要记录", "一次性的提醒或待办", "时效性信息"):
            self.assertIn(fragment, d)


if __name__ == "__main__":
    unittest.main(verbosity=2)
