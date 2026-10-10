"""单元测试（纯函数层，不起服务、不联网）。

为什么补这一层：
- `scripts/smoke_test.py` 是**接口级**冒烟：把整个服务拉起来打 32 个请求，验"整体能跑"。
- 它验不了"某个函数算得对不对"，也因为要起服务，改一个小函数反馈很慢。
- 这一层直接测函数，**毫秒级**，改一行逻辑就能立刻知道有没有弄坏东西。

运行：
    python -m unittest discover -s tests -v
    python tests/test_units.py          # 等价

依赖：只用标准库unittest，**不装任何东西**（与本项目"零第三方依赖"一致）。
"""

from __future__ import annotations

import os
import sys
import gc
import time
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from personal_ai.config import Config
from personal_ai.core.chat import ChatService
from personal_ai.providers import ChatMessage, ChatResponse, create
from personal_ai.providers.mock import MockProvider
from personal_ai.providers.openai_compat import OpenAICompatProvider
from personal_ai.storage import (
    add_message,
    create_session,
    create_task,
    delete_session,
    delete_task,
    init_db,
    list_messages,
    list_sessions,
    list_tasks,
    update_task,
)


class TempDB(unittest.TestCase):
    """给每个用例一份独立的临时数据库，互不干扰。"""

    def setUp(self) -> None:
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        # 建表。应用启动时 ChatService 会做同一件事；
        # 这里必须自己调一次，否则直接用存储函数会撞 "no such table"。
        init_db(self.db)

    def tearDown(self) -> None:
        # Windows 上 sqlite 连接是惰性释放的：连接对象被回收后文件句柄才松开。
        # 这里显式回收并重试几次，避免测试本身的清理失败盖住真正的断言结果。
        gc.collect()
        for p in (self.db, self.db + "-wal", self.db + "-shm"):
            for _ in range(5):
                if not os.path.exists(p):
                    break
                try:
                    os.remove(p)
                except PermissionError:
                    time.sleep(0.05)


# ============================ Provider 抽象层 ============================

class TestProviderBase(unittest.TestCase):
    def test_chain_text_joins_chain(self):
        """有来源链时，把每一跳用箭头连起来 —— 这是"来源透明"的落地。"""
        r = ChatResponse(content="x", provider="p", model="m",
                         chain=["openai-compat", "api.example.com", "m"])
        self.assertEqual(r.chain_text(), "openai-compat → api.example.com → m")

    def test_chain_text_falls_back_to_model(self):
        """来源链为空时不能返回空字符串，至少退回模型名。"""
        r = ChatResponse(content="x", provider="p", model="m", chain=[])
        self.assertEqual(r.chain_text(), "m")

    def test_default_stream_yields_whole_content(self):
        """基类的默认 stream() 把非流式结果一次吐出，保证所有 Provider 都有 stream()。"""
        cfg = Config(provider="mock", model="m")
        p = MockProvider(cfg)
        got = list(p.stream([ChatMessage(role="user", content="hi")]))
        self.assertEqual(len(got), 1)
        self.assertIn("[mock]", got[0])

    def test_describe_shape(self):
        d = MockProvider(Config(provider="mock")).describe()
        self.assertEqual(d["name"], "mock")
        self.assertEqual(d["model"], "mock-offline")
        self.assertIn("configured", d)


class TestMockProvider(unittest.TestCase):
    def setUp(self):
        self.p = MockProvider(Config(provider="mock"))

    def test_marks_itself_as_mock(self):
        """离线兜底必须自我标注，不能冒充真回答。"""
        r = self.p.complete([ChatMessage(role="user", content="你是谁")])
        self.assertTrue(r.ok)
        self.assertTrue(r.content.startswith("[mock]"))
        self.assertEqual(r.provider, "mock")

    def test_echoes_last_user_message(self):
        """应回显最后一条 user 消息（倒序找，不是第一条）。"""
        r = self.p.complete([
            ChatMessage(role="user", content="第一句"),
            ChatMessage(role="assistant", content="回答"),
            ChatMessage(role="user", content="最后一句"),
        ])
        self.assertIn("最后一句", r.content)
        self.assertNotIn("你刚才说的是：第一句", r.content)

    def test_no_user_message_still_works(self):
        """没有 user 消息时不能崩。"""
        r = self.p.complete([ChatMessage(role="system", content="sys")])
        self.assertTrue(r.ok)


class TestOpenAICompatPureFunctions(unittest.TestCase):
    """只测不发请求的纯函数：URL拼接、host 标签、失败响应构造。"""

    def _p(self, base_url="", api_key="k"):
        return OpenAICompatProvider(Config(provider="openai_compat",
                                            base_url=base_url, api_key=api_key))

    def test_endpoint_appends_v1_and_path(self):
        self.assertEqual(self._p("http://127.0.0.1:20128")._endpoint(),
                         "http://127.0.0.1:20128/v1/chat/completions")

    def test_endpoint_idempotent(self):
        """已经是完整地址时不要重复拼接。"""
        full = "http://127.0.0.1:20128/v1/chat/completions"
        self.assertEqual(self._p(full)._endpoint(), full)

    def test_endpoint_strips_trailing_slash(self):
        self.assertEqual(self._p("http://h/v1/")._endpoint(),
                         "http://h/v1/chat/completions")

    def test_host_label(self):
        self.assertEqual(self._p("http://127.0.0.1:20128/v1")._host_label(),
                         "127.0.0.1:20128")

    def test_host_label_never_empty(self):
        self.assertEqual(self._p("")._host_label(), "unknown-host")

    def test_blank_response_is_marked_failed(self):
        """失败响应必须 ok=False 且带 error —— 不能静默伪装成功。"""
        r = self._p("http://h")._blank("boom", {"a": 1})
        self.assertFalse(r.ok)
        self.assertEqual(r.error, "boom")
        self.assertEqual(r.content, "")

    def test_is_configured_requires_both(self):
        self.assertFalse(self._p("http://h", "").is_configured())
        self.assertTrue(self._p("http://h", "k").is_configured())


class TestRegistry(unittest.TestCase):
    def test_mock_registered(self):
        self.assertEqual(create("mock", Config(provider="mock")).name, "mock")

    def test_openai_compat_registered(self):
        self.assertEqual(
            create("openai_compat", Config(provider="openai_compat",
                                           base_url="http://h", api_key="k")).name,
            "openai_compat")


# ============================== 配置与密钥 ==============================

class TestConfig(unittest.TestCase):
    def test_describe_never_leaks_api_key(self):
        """安全断言：对外快照只能出现 api_key_set，不能出现密钥内容。"""
        d = Config(api_key="sk-secret-value").describe()
        self.assertTrue(d["api_key_set"])
        self.assertNotIn("api_key", d)
        self.assertNotIn("sk-secret-value", str(d))

    def test_from_env_defaults(self):
        for k in ("PAI_PROVIDER", "PAI_BASE_URL", "PAI_API_KEY", "PAI_MODEL",
                  "PAI_PORT", "PAI_TIMEOUT", "PAI_RETRIES", "PAI_HOST"):
            os.environ.pop(k, None)
        c = Config.from_env()
        self.assertEqual(c.provider, "mock")
        self.assertEqual(c.port, 8787)
        self.assertEqual(c.timeout, 180)
        self.assertEqual(c.retries, 2)
        self.assertEqual(c.host, "127.0.0.1")

    def test_from_env_survives_garbage_numbers(self):
        """环境变量写成乱码时必须回落默认值，而不是崩掉。"""
        os.environ["PAI_PORT"] = "not-a-port"
        os.environ["PAI_TIMEOUT"] = "abc"
        os.environ["PAI_RETRIES"] = "x"
        try:
            c = Config.from_env()
            self.assertEqual(c.port, 8787)
            self.assertEqual(c.timeout, 180)
            self.assertEqual(c.retries, 2)
        finally:
            for k in ("PAI_PORT", "PAI_TIMEOUT", "PAI_RETRIES"):
                os.environ.pop(k, None)

    def test_retries_never_negative(self):
        os.environ["PAI_RETRIES"] = "-5"
        try:
            self.assertEqual(Config.from_env().retries, 0)
        finally:
            os.environ.pop("PAI_RETRIES", None)

    def test_db_path_autofilled(self):
        self.assertTrue(Config().db_path)


# ================================ 存储层 ================================

class TestStorage(TempDB):
    def test_session_and_message_roundtrip(self):
        sid = create_session(self.db, title="第一场")
        add_message(self.db, sid, "user", "你好")
        add_message(self.db, sid, "assistant", "在", provider="mock",
                    model="mock-offline", chain="mock → mock-offline")
        msgs = list_messages(self.db, sid)
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant"])
        self.assertTrue(list_sessions(self.db))

    def test_message_limit_and_order(self):
        """limit 只取最近若干条，且仍按时间正序返回。"""
        sid = create_session(self.db)
        for i in range(5):
            add_message(self.db, sid, "user", f"m{i}")
        got = list_messages(self.db, sid, limit=2)
        self.assertEqual([m["content"] for m in got], ["m3", "m4"])

    def test_ok_is_returned_as_bool(self):
        sid = create_session(self.db)
        add_message(self.db, sid, "assistant", "x", ok=False)
        self.assertIs(list_messages(self.db, sid)[0]["ok"], False)

    def test_create_task_rejects_bad_status(self):
        """非法状态必须回落成 todo，不能写脏数据进库。"""
        tid = create_task(self.db, title="写测试", status="不存在的状态")
        task = list_tasks(self.db)[0]
        self.assertEqual(task["id"], tid)
        self.assertEqual(task["status"], "todo")

    def test_list_tasks_filter_by_status(self):
        a = create_task(self.db, title="A")
        create_task(self.db, title="B", status="done")
        update_task(self.db, a, status="doing")
        self.assertEqual([t["title"] for t in list_tasks(self.db, status="done")], ["B"])
        self.assertEqual([t["title"] for t in list_tasks(self.db, status="doing")], ["A"])

    def test_update_task_returns_bool(self):
        tid = create_task(self.db, title="A")
        self.assertTrue(update_task(self.db, tid, status="done"))
        self.assertFalse(update_task(self.db, "不存在", status="done"))

    def test_delete_task_and_session(self):
        tid = create_task(self.db, title="A")
        self.assertTrue(delete_task(self.db, tid))
        self.assertFalse(delete_task(self.db, tid))
        self.assertEqual(list_tasks(self.db), [])

        sid = create_session(self.db)
        self.assertTrue(delete_session(self.db, sid))
        self.assertEqual(list_sessions(self.db), [])


# =============== 上下文组装（今天修的那条，必须有回归测试） ===============

class TestContextAssembly(TempDB):
    def setUp(self):
        super().setUp()
        self.svc = ChatService(Config(provider="mock", db_path=self.db))

    def test_filters_mock_and_failed_messages(self):
        """
        回归测试：离线兜底的假回答、生成失败的回答，都不该参与后续推理。
        起因是真实发生过一次答非所问 —— 旧的 [mock] 回答被当成"用户说过的话"发给模型。
        它们仍然留在库里可查，只是不进上下文。
        """
        sid = create_session(self.db)
        add_message(self.db, sid, "user", "真问题")
        add_message(self.db, sid, "assistant", "[mock] 假回答", provider="mock")
        add_message(self.db, sid, "assistant", "失败的回答", provider="openai_compat", ok=False)
        add_message(self.db, sid, "assistant", "真回答", provider="openai_compat")

        ctx = self.svc.context_for(sid)
        self.assertEqual([m.content for m in ctx[1:]],
                         ["真问题", "真回答"])
        self.assertEqual(ctx[0].role, "system")

    def test_filtered_messages_still_readable_in_db(self):
        """过滤只影响上下文，不能把历史记录也删掉。"""
        sid = create_session(self.db)
        add_message(self.db, sid, "assistant", "[mock] 假回答", provider="mock")
        self.assertEqual(len(list_messages(self.db, sid)), 1)

    def test_send_appends_user_and_assistant(self):
        sid = self.svc.new_session(title="t")
        out = self.svc.send(sid, "你是谁")
        self.assertTrue(out["is_mock"])
        self.assertEqual([m["role"] for m in list_messages(self.db, sid)],
                         ["user", "assistant"])


if __name__ == "__main__":
    unittest.main(verbosity=2)