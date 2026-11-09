"""MCP Server 集成测试（39 号）：子进程跑 mcp_server.py，管道模拟客户端握手/列工具/建/查。"""

from __future__ import annotations

import gc
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "mcp_server.py"

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2024-11-05", "capabilities": {},
    "clientInfo": {"name": "test", "version": "1.0"}}}
LIST_TOOLS = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
NOTIFY_DONE = {"jsonrpc": "2.0", "method": "notifications/initialized"}


class TestMcpServer(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.env = dict(os.environ, PAI_DB_PATH=self.db, PYTHONIOENCODING="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env, text=True,
            encoding="utf-8", cwd=str(ROOT))

    def _send(self, req: dict) -> dict | None:
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        if "id" not in req or req.get("id") is None:
            return None  # 通知不回
        line = self.proc.stdout.readline()
        return json.loads(line) if line else None

    def tearDown(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        gc.collect()
        for _ in range(3):
            try:
                os.unlink(self.db)
                break
            except PermissionError:
                gc.collect()

    def test_01_initialize_handshake(self):
        """验收 1：握手返回 protocolVersion + capabilities.tools。"""
        r = self._send(INIT)
        self.assertEqual(r["jsonrpc"], "2.0")
        self.assertEqual(r["result"]["protocolVersion"], "2024-11-05")
        self.assertIn("tools", r["result"]["capabilities"])
        self.assertEqual(r["result"]["serverInfo"]["name"], "personal-ai-memory")

    def test_02_notification_is_silent(self):
        """通知类消息不回（stdout 无输出）；且不阻塞后续请求。"""
        self._send(NOTIFY_DONE)
        r = self._send({"jsonrpc": "2.0", "id": 9, "method": "tools/list"})
        self.assertEqual(r["id"], 9)

    def test_03_tools_list_with_kind_rule(self):
        """验收 2：工具齐全；kind 判据在 create 的 kind 参数 description、排除规则在工具 description。"""
        r = self._send(LIST_TOOLS)
        tools = {t["name"]: t for t in r["result"]["tools"]}
        self.assertIn("命中即停", tools["memory_create"]["inputSchema"]["properties"]["kind"]["description"])
        self.assertIn("一次性的提醒或待办", tools["memory_create"]["description"])

    def test_04_create_then_search_roundtrip(self):
        """验收 3+4：create 返回成功且库里 +1；search 查到刚写的那条。"""
        import sqlite3

        self._send(LIST_TOOLS)  # 先确认子进程 ready（ensure_tables 已完成）
        con = sqlite3.connect(self.db)
        before = con.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        con.close()
        r = self._send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "memory_create", "arguments": {
                "kind": "preference", "title": "缩进偏好", "content": "习惯使用 tab 缩进代码"}}})
        payload = json.loads(r["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"], payload)
        self.assertFalse(r["result"].get("isError", False))
        con = sqlite3.connect(self.db)
        after = con.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        con.close()
        self.assertEqual(after, before + 1)  # 库里真多了一条

        r = self._send({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
            "name": "memory_search", "arguments": {"query": "tab 缩进"}}})
        payload = json.loads(r["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"])
        self.assertGreaterEqual(payload["count"], 1)
        self.assertIn("tab", json.dumps(payload["items"], ensure_ascii=False))

    def test_05_list_and_unknown_tool(self):
        """memory_list 列出刚写的条目；未知工具回 -32602 而不是崩。"""
        self._send({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {
            "name": "memory_create", "arguments": {
                "kind": "fact", "title": "根目录", "content": "项目根目录在 E:\\DSHPersonalAI"}}})
        r = self._send({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {
            "name": "memory_list", "arguments": {"kind": "fact"}}})
        payload = json.loads(r["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"])
        self.assertGreaterEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["kind"], "fact")

        r = self._send({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
            "name": "no_such_tool", "arguments": {}}})
        self.assertEqual(r["error"]["code"], -32602)

    def test_06_unknown_method_32601(self):
        """未知 method 回 -32601。"""
        r = self._send({"jsonrpc": "2.0", "id": 8, "method": "resources/xxx"})
        self.assertEqual(r["error"]["code"], -32601)

    # ------------------------------------------------- 40 号：12 工具 + resources + prompts

    def _seed_one(self, kind="fact", title="根目录", content="项目根目录在 E:\\DSHPersonalAI"):
        return self._send({"jsonrpc": "2.0", "id": 100, "method": "tools/call", "params": {
            "name": "memory_create",
            "arguments": {"kind": kind, "title": title, "content": content}}})

    def test_07_tools_list_has_eighteen(self):
        """工具扩到 18 个：记忆库 14（含 43 号 memory_verify/memory_conflicts）+ 文件层 4。"""
        r = self._send(LIST_TOOLS)
        names = {t["name"] for t in r["result"]["tools"]}
        self.assertEqual(len(names), 18)
        self.assertEqual(names, {
            "memory_create", "memory_search", "memory_list", "memory_view",
            "memory_update", "memory_delete", "memory_rename", "memory_insert",
            "memory_stats", "memory_export", "memory_import", "history_search",
            "memory_verify", "memory_conflicts",
            "journal_search", "journal_read", "topic_list", "topic_read"})

    def test_08_view_update_rename_insert_chain(self):
        """查详情 → 编辑 → 改名 → 插入：一条记忆的完整生命周期（除删除）。"""
        self._seed_one()
        r = self._send({"jsonrpc": "2.0", "id": 101, "method": "tools/call", "params": {
            "name": "memory_view", "arguments": {"path": "/memories/fact/根目录.md"}}})
        payload = json.loads(r["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"])
        self.assertIn("DSHPersonalAI", payload["items"][0]["content"])

        r = self._send({"jsonrpc": "2.0", "id": 102, "method": "tools/call", "params": {
            "name": "memory_update", "arguments": {
                "path": "/memories/fact/根目录.md", "old": "E:\\DSHPersonalAI", "new": "E:\\DSHPersonalAI\\games"}}})
        payload = json.loads(r["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"])

        r = self._send({"jsonrpc": "2.0", "id": 103, "method": "tools/call", "params": {
            "name": "memory_rename", "arguments": {
                "old": "/memories/fact/根目录.md", "new": "/memories/fact/项目根目录.md"}}})
        payload = json.loads(r["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"])

        r = self._send({"jsonrpc": "2.0", "id": 104, "method": "tools/call", "params": {
            "name": "memory_insert", "arguments": {
                "path": "/memories/fact/项目根目录.md", "text": "games 子目录放游戏项目。", "after": 1}}})
        payload = json.loads(r["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"])

        r = self._send({"jsonrpc": "2.0", "id": 105, "method": "tools/call", "params": {
            "name": "memory_view", "arguments": {"path": "/memories/fact/项目根目录.md"}}})
        payload = json.loads(r["result"]["content"][0]["text"])
        self.assertIn("games 子目录", payload["items"][0]["content"])

    def test_09_delete_soft_hidden_from_list(self):
        """软删后 search/list 不再返回，tombstone 保留（不物理删）。"""
        self._seed_one()
        r = self._send({"jsonrpc": "2.0", "id": 110, "method": "tools/call", "params": {
            "name": "memory_delete", "arguments": {"path": "/memories/fact/根目录.md"}}})
        payload = json.loads(r["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"])
        r = self._send({"jsonrpc": "2.0", "id": 111, "method": "tools/call", "params": {
            "name": "memory_list", "arguments": {}}})
        payload = json.loads(r["result"]["content"][0]["text"])
        self.assertEqual(payload["count"], 0)

    def test_10_stats_export_import(self):
        """批量导入 3 条 → stats 按 kind 计数 → export 导出全部。"""
        r = self._send({"jsonrpc": "2.0", "id": 120, "method": "tools/call", "params": {
            "name": "memory_import", "arguments": {"items": [
                {"kind": "preference", "title": "缩进", "content": "习惯用 tab 缩进"},
                {"kind": "preference", "title": "回答风格", "content": "喜欢简洁直接的回答"},
                {"kind": "fact", "title": "根目录", "content": "项目根目录在 E:\\DSHPersonalAI"},
            ]}}})
        payload = json.loads(r["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["imported"], 3)
        self.assertEqual(payload["failed"], [])

        r = self._send({"jsonrpc": "2.0", "id": 121, "method": "tools/call", "params": {
            "name": "memory_stats", "arguments": {}}})
        payload = json.loads(r["result"]["content"][0]["text"])
        self.assertEqual(payload["total"], 3)
        by_kind = {row["kind"]: row["count"] for row in payload["by_kind"]}
        self.assertEqual(by_kind, {"preference": 2, "fact": 1})

        r = self._send({"jsonrpc": "2.0", "id": 122, "method": "tools/call", "params": {
            "name": "memory_export", "arguments": {}}})
        payload = json.loads(r["result"]["content"][0]["text"])
        self.assertEqual(payload["count"], 3)
        sources = {item["source"] for item in payload["items"]}
        self.assertIn("批量导入", sources)

    def test_11_list_time_filter(self):
        """memory_list 的 since/until 时间范围过滤。"""
        self._seed_one()
        r = self._send({"jsonrpc": "2.0", "id": 130, "method": "tools/call", "params": {
            "name": "memory_list", "arguments": {"since": "2020-01-01"}}})
        payload = json.loads(r["result"]["content"][0]["text"])
        self.assertEqual(payload["count"], 1)
        r = self._send({"jsonrpc": "2.0", "id": 131, "method": "tools/call", "params": {
            "name": "memory_list", "arguments": {"since": "2099-01-01"}}})
        payload = json.loads(r["result"]["content"][0]["text"])
        self.assertEqual(payload["count"], 0)

    def test_12_resources_list_and_read(self):
        """resources：list 把记忆列成可浏览条目，read 按 uri 读全文。"""
        self._seed_one()
        r = self._send({"jsonrpc": "2.0", "id": 140, "method": "resources/list"})
        resources = r["result"]["resources"]
        self.assertEqual(len(resources), 1)
        uri = resources[0]["uri"]
        self.assertTrue(uri.startswith("memories://"))

        r = self._send({"jsonrpc": "2.0", "id": 141, "method": "resources/read", "params": {"uri": uri}})
        content = r["result"]["contents"][0]
        self.assertEqual(content["mimeType"], "text/markdown")
        self.assertIn("DSHPersonalAI", content["text"])

        r = self._send({"jsonrpc": "2.0", "id": 142, "method": "resources/read",
                        "params": {"uri": "memories://memories/fact/不存在.md"}})
        self.assertEqual(r["error"]["code"], -32602)

    def test_13_prompts_list_and_get(self):
        """prompts：3 个预设模板，get 返回可展开的 user 消息。"""
        r = self._send({"jsonrpc": "2.0", "id": 150, "method": "prompts/list"})
        prompts = {p["name"] for p in r["result"]["prompts"]}
        self.assertEqual(prompts, {"recall_last_week", "my_preferences", "decision_trace"})

        r = self._send({"jsonrpc": "2.0", "id": 151, "method": "prompts/get", "params": {
            "name": "decision_trace", "arguments": {"topic": "数据库选型"}}})
        msg = r["result"]["messages"][0]
        self.assertEqual(msg["role"], "user")
        self.assertIn("数据库选型", msg["content"]["text"])
        self.assertIn("memory_search", msg["content"]["text"])

        r = self._send({"jsonrpc": "2.0", "id": 152, "method": "prompts/get", "params": {
            "name": "no_such_prompt"}})
        self.assertEqual(r["error"]["code"], -32602)

    def test_14_initialize_has_resources_prompts(self):
        """initialize 的 capabilities 声明 resources 与 prompts。"""
        r = self._send(INIT)
        caps = r["result"]["capabilities"]
        self.assertIn("resources", caps)
        self.assertIn("prompts", caps)

    def test_28_conflicts_and_verify_pipe(self):
        """43 号管道：create 检出冲突 → memory_conflicts 列原文 → memory_verify 确认。"""
        self._send(LIST_TOOLS)
        r = self._send({"jsonrpc": "2.0", "id": 160, "method": "tools/call", "params": {
            "name": "memory_create", "arguments": {
                "kind": "conclusion", "title": "数据库选型",
                "content": "本项目数据库统一用 SQLite。"}}})
        self.assertTrue(json.loads(r["result"]["content"][0]["text"])["ok"])
        r = self._send({"jsonrpc": "2.0", "id": 161, "method": "tools/call", "params": {
            "name": "memory_create", "arguments": {
                "kind": "conclusion", "title": "数据库选型变更",
                "content": "数据库改用 Postgres。"}}})
        payload = json.loads(r["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"])
        self.assertIn("两条都保留", payload.get("message", ""))
        self.assertIn("未标取代", payload.get("message", ""))

        r = self._send({"jsonrpc": "2.0", "id": 162, "method": "tools/call", "params": {
            "name": "memory_conflicts", "arguments": {}}})
        payload = json.loads(r["result"]["content"][0]["text"])
        self.assertEqual(payload["count"], 1)
        pair = payload["items"][0]
        self.assertIn("统一用 SQLite", pair["a"]["content"])   # 原文全文返回
        self.assertIn("改用 Postgres", pair["b"]["content"])
        self.assertTrue(pair["reasons"])

        r = self._send({"jsonrpc": "2.0", "id": 163, "method": "tools/call", "params": {
            "name": "memory_verify",
            "arguments": {"path": "/memories/conclusion/数据库选型.md"}}})
        payload = json.loads(r["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"])
        self.assertIn("verified_at", payload)


# ------------------------------------- 42 号：文件层四工具（临时目录现造样本，零真实日志内容）

_FIXTURE = {
    "2026-11-01.md": (
        "# 2026-11-01 测试日志\n"
        "\n"
        "## 10:00 曼努斯测试\n"
        "\n"
        "- 曼努斯面试结束（21 分钟），面试官说适配度不够。\n"
        "- 线下会面地点待定。\n"
    ),
    "2026-11-02.md": (
        "# 2026-11-02 测试日志\n"
        "\n"
        "## 09:00 例行盘点\n"
        "\n"
        "- 今天没有特别的事，只有例行盘点一条。\n"
    ),
    "LOOKUP.md": (
        "# LOOKUP（合成测试数据）\n"
        "\n"
        "### 问：曼努斯为什么没过？\n"
        "→ 2026-11-01.md L5『## 10:00 曼努斯测试』\n"
        "→ 一句话：适配度不够。\n"
    ),
    "MEMORY.md": "# 测试记忆\n\n- 一条合成偏好：喜欢简洁回答。\n",
    "index/2026-Q4.md": "- 2026-11-01：曼努斯测试结束\n",
    "topics/求职线.md": "# 求职线（合成测试数据）\n\n## 2026-11-01\n\n- 投了三家合成公司。\n",
    "topics/技术线.md": "# 技术线（合成测试数据）\n\n- 没有内容。\n",
}


class TestMcpFileLayer(unittest.TestCase):
    """文件层工具：PAI_MEMORY_DIR 指向临时目录，样本全部现造（不用真实日志，隐私+体积）。"""

    def setUp(self) -> None:
        self.mem = tempfile.mkdtemp(prefix="pai_mem_")
        self.outside = tempfile.mkdtemp(prefix="pai_out_")  # 记忆目录之外，验证逃逸被拒
        for rel, content in _FIXTURE.items():
            p = Path(self.mem) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        (Path(self.outside) / "outside_secret.md").write_text(
            "这是记忆目录之外的秘密文件", encoding="utf-8")
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.env = dict(os.environ, PAI_DB_PATH=self.db, PAI_MEMORY_DIR=self.mem,
                        PYTHONIOENCODING="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env, text=True,
            encoding="utf-8", cwd=str(ROOT))

    def _send(self, req: dict) -> dict | None:
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        if "id" not in req or req.get("id") is None:
            return None
        line = self.proc.stdout.readline()
        return json.loads(line) if line else None

    def tearDown(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        gc.collect()

    def _call(self, req_id: int, name: str, arguments: dict) -> dict:
        return self._send({"jsonrpc": "2.0", "id": req_id, "method": "tools/call",
                           "params": {"name": name, "arguments": arguments}})

    def test_20_journal_search_chinese_hit(self):
        """中文关键词命中：结果带 相对路径+行号+原文；日志与 LOOKUP 都命中。"""
        self._send(LIST_TOOLS)  # 探活：确认子进程 ready
        r = self._call(200, "journal_search", {"query": "曼努斯 面试"})
        self.assertNotIn("error", r, r)
        payload = json.loads(r["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"])
        self.assertGreaterEqual(payload["total_hits"], 3)
        paths = {item["path"] for item in payload["results"]}
        self.assertIn("2026-11-01.md", paths)
        self.assertIn("LOOKUP.md", paths)
        texts = json.dumps(payload["results"], ensure_ascii=False)
        self.assertIn("面试官说适配度不够", texts)
        for item in payload["results"]:
            self.assertIn("line", item) and self.assertIn("text", item)

    def test_21_journal_search_no_hit_returns_empty(self):
        """查不到返回空列表（ok 仍 True），不编造。"""
        self._send(LIST_TOOLS)
        r = self._call(201, "journal_search", {"query": "紫电青霜剑气"})
        payload = json.loads(r["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["results"], [])
        self.assertEqual(payload["total_hits"], 0)

    def test_22_journal_read_returns_original(self):
        """按指针读原文：返回带 L 行号前缀的原文行，不是摘要。"""
        self._send(LIST_TOOLS)
        r = self._call(202, "journal_read", {"path": "2026-11-01.md", "line": 5, "context": 1})
        payload = json.loads(r["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["total_lines"], 6)
        self.assertIn("L5: - 曼努斯面试结束（21 分钟），面试官说适配度不够。", payload["text"])

    def test_23_journal_read_line_out_of_range(self):
        """行号越界回 -32602 并说明合法范围。"""
        self._send(LIST_TOOLS)
        r = self._call(203, "journal_read", {"path": "2026-11-01.md", "line": 999})
        self.assertEqual(r["error"]["code"], -32602)
        self.assertIn("行号越界", r["error"]["message"])

    def test_24_journal_read_path_escape_rejected(self):
        """../.. 逃逸被拒：-32602「路径越界」，读不到记忆目录之外的文件。"""
        self._send(LIST_TOOLS)
        r = self._call(204, "journal_read",
                       {"path": "../../outside_secret.md", "line": 1})
        self.assertEqual(r["error"]["code"], -32602)
        self.assertIn("路径越界", r["error"]["message"])
        self.assertNotIn("秘密文件", json.dumps(r, ensure_ascii=False))

    def test_25_memory_dir_missing_explicit_error(self):
        """PAI_MEMORY_DIR 指向不存在的目录：明确报「记忆目录不存在」，不静默返回空。"""
        env = dict(self.env, PAI_MEMORY_DIR=str(Path(self.mem) / "不存在的目录"))
        proc = subprocess.Popen(
            [sys.executable, str(SERVER)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True,
            encoding="utf-8", cwd=str(ROOT))
        try:
            proc.stdin.write(json.dumps(LIST_TOOLS) + "\n")
            proc.stdin.flush()
            json.loads(proc.stdout.readline())  # 探活
            proc.stdin.write(json.dumps(
                {"jsonrpc": "2.0", "id": 205, "method": "tools/call",
                 "params": {"name": "journal_search", "arguments": {"query": "曼努斯"}}}) + "\n")
            proc.stdin.flush()
            r = json.loads(proc.stdout.readline())
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass
            proc.wait(timeout=5)
        self.assertEqual(r["error"]["code"], -32602)
        self.assertIn("记忆目录不存在", r["error"]["message"])

    def test_26_topic_list_and_read(self):
        """topic_list 列出主题；topic_read 正常读、不存在报错、越界拒绝。"""
        self._send(LIST_TOOLS)
        r = self._call(206, "topic_list", {})
        payload = json.loads(r["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["count"], 2)
        self.assertEqual({t["name"] for t in payload["topics"]}, {"求职线", "技术线"})

        r = self._call(207, "topic_read", {"name": "求职线"})
        payload = json.loads(r["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"])
        self.assertIn("合成测试数据", payload["text"])

        r = self._call(208, "topic_read", {"name": "topics/技术线.md"})
        payload = json.loads(r["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"])

        r = self._call(209, "topic_read", {"name": "不存在"})
        self.assertEqual(r["error"]["code"], -32602)
        r = self._call(210, "topic_read", {"name": "../../outside_secret.md"})
        self.assertEqual(r["error"]["code"], -32602)
        self.assertIn("路径越界", r["error"]["message"])

    def test_27_journal_read_context_clipping(self):
        """上下文裁剪：目标行贴近文件头/尾时不越界。"""
        self._send(LIST_TOOLS)
        r = self._call(211, "journal_read", {"path": "2026-11-02.md", "line": 1, "context": 10})
        payload = json.loads(r["result"]["content"][0]["text"])
        self.assertEqual(payload["range"], [1, 5])  # 文件只有 5 行，尾部裁掉
        self.assertNotIn("L6:", payload["text"])

        r = self._call(212, "journal_read", {"path": "2026-11-01.md", "line": 5, "context": 1})
        payload = json.loads(r["result"]["content"][0]["text"])
        self.assertEqual(payload["range"], [4, 6])


if __name__ == "__main__":
    unittest.main(verbosity=2)
