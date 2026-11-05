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
        """验收 2：4 个工具；kind 判据在 create 的 kind 参数 description、排除规则在工具 description。"""
        r = self._send(LIST_TOOLS)
        tools = {t["name"]: t for t in r["result"]["tools"]}
        self.assertEqual(set(tools), {"memory_create", "memory_search", "memory_list", "history_search"})
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
        r = self._send({"jsonrpc": "2.0", "id": 8, "method": "resources/list"})
        self.assertEqual(r["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main(verbosity=2)
