"""零依赖 HTTP 服务。

路由：
    GET  /                          服务首页（最小占位页，A3 阶段替换为时间轴 UI）
    GET  /api/health                健康检查 + 当前 provider 状态
    GET  /api/providers             所有可替换 provider 列表
    GET  /api/sessions              会话列表（过去）
    POST /api/sessions              新建会话
    GET  /api/sessions/<id>/messages 会话历史（过去）
    POST /api/chat                  发一条消息（现在）
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from ..config import Config
from ..core import ChatService, TaskService

# 前端静态资源目录（A3 接入的「过去 / 现在 / 未来」时间轴 UI）
WEB_DIR = Path(__file__).resolve().parents[1] / "web"

STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}



class Handler(BaseHTTPRequestHandler):
    service: ChatService
    tasks: TaskService
    server_version = "PersonalAI/0.1.0-a1"

    # ---------- 基础 ----------

    def _send(self, code: int, payload: dict | list, cors: bool = True) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,PATCH,DELETE,OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, name: str, content_type: str) -> bool:
        """从 web/ 目录读取静态文件。读不到返回 False，由调用方降级。"""
        path = WEB_DIR / name
        try:
            body = path.read_bytes()
        except OSError:
            return False
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)
        return True

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def log_message(self, fmt: str, *args) -> None:  # 精简日志
        print(f"  {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PATCH,DELETE,OPTIONS")
        self.end_headers()

    # ---------- 路由 ----------

    def do_GET(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        svc = self.service

        if path in STATIC_FILES:
            name, ctype = STATIC_FILES[path]
            if not self._send_static(name, ctype):
                return self._send(503, {"error": f"前端资源缺失: {name}"})
            return None
        if path == "/api/health":
            return self._send(200, svc.status())
        if path == "/api/providers":
            from ..providers import describe_all

            return self._send(200, describe_all(svc.config))
        if path == "/api/sessions":
            return self._send(200, svc.sessions())
        if path == "/api/tasks":
            return self._send(200, self.tasks.list())

        parts = path.split("/")
        if len(parts) == 5 and parts[1] == "api" and parts[2] == "sessions" and parts[4] == "messages":
            return self._send(200, svc.history(parts[3]))

        return self._send(404, {"error": f"not found: {path}"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        svc = self.service
        data = self._body()

        if path == "/api/sessions":
            sid = svc.new_session(title=str(data.get("title", "")))
            return self._send(201, {"session_id": sid})

        if path == "/api/chat":
            text = str(data.get("message", "")).strip()
            if not text:
                return self._send(400, {"error": "message 不能为空"})
            result = svc.send(str(data.get("session_id", "")), text)
            return self._send(200 if result["ok"] else 502, result)

        if path == "/api/chat/stream":
            return self._chat_stream(data)

        if path == "/api/tasks":
            title = str(data.get("title", "")).strip()
            if not title:
                return self._send(400, {"error": "title 不能为空"})
            tid = self.tasks.add(
                title=title,
                note=str(data.get("note", "")),
                status=str(data.get("status", "todo")),
            )
            return self._send(201, {"id": tid, "summary": self.tasks.summary()})

        return self._send(404, {"error": f"not found: {path}"})

    def _chat_stream(self, data: dict) -> None:
        """SSE 流式对话：逐块把回答推给前端，结束后再统一落库。"""
        svc = self.service
        text = str(data.get("message", "")).strip()
        if not text:
            return self._send(400, {"error": "message 不能为空"})

        sid = svc.prepare(str(data.get("session_id", "")), text)
        messages = svc.context_for(sid)

        # 关键：BaseHTTPRequestHandler 默认 HTTP/1.0，若声明 keep-alive 会让客户端一直等
        # EOF。这里显式关闭连接，流结束即返回。
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        parts: list[str] = []
        try:
            for piece in svc.provider.stream(messages):
                if not piece:
                    continue
                parts.append(piece)
                chunk = json.dumps({"delta": piece}, ensure_ascii=False)
                self.wfile.write(f"data: {chunk}\n\n".encode("utf-8"))
                self.wfile.flush()

            full = "".join(parts)
            ok = not full.startswith("[stream error]")
            svc.finish(sid, full, chain=self._chain_text(), model=svc.provider.default_model(), ok=ok)
            done = json.dumps(
                {
                    "session_id": sid,
                    "done": True,
                    "ok": ok,
                    "content": full,
                    "model": svc.provider.default_model(),
                    "chain": self._chain_text(),
                },
                ensure_ascii=False,
            )
            self.wfile.write(f"data: {done}\n\n".encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return  # 前端断了：静默收尾，不写库
        except Exception as e:
            err = json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)
            try:
                self.wfile.write(f"data: {err}\n\n".encode("utf-8"))
                self.wfile.flush()
            except Exception:
                pass

    def _chain_text(self) -> str:
        p = self.service.provider
        return " → ".join(p.default_chain() + [p.default_model()])

    def do_PATCH(self) -> None:
        """更新任务：/api/tasks/<id>"""
        path = urlparse(self.path).path.rstrip("/") or "/"
        data = self._body()
        parts = path.split("/")

        if len(parts) == 4 and parts[1] == "api" and parts[2] == "tasks":
            fields = {
                k: data[k]
                for k in ("title", "status", "note", "due_at")
                if k in data
            }
            if not fields:
                return self._send(400, {"error": "没有可更新字段"})
            ok = self.tasks.update(parts[3], **fields)
            if not ok:
                return self._send(404, {"error": "任务不存在或无字段被更新"})
            return self._send(200, {"ok": True, "summary": self.tasks.summary()})

        return self._send(404, {"error": f"not found: {path}"})

    def do_DELETE(self) -> None:
        """删除任务：/api/tasks/<id>"""
        path = urlparse(self.path).path.rstrip("/") or "/"
        parts = path.split("/")

        if len(parts) == 4 and parts[1] == "api" and parts[2] == "tasks":
            ok = self.tasks.delete(parts[3])
            if not ok:
                return self._send(404, {"error": "任务不存在"})
            return self._send(200, {"ok": True, "summary": self.tasks.summary()})

        return self._send(404, {"error": f"not found: {path}"})


def run_server(config: Config | None = None, port: int | None = None) -> None:
    config = config or Config.from_env()
    if port:
        config.port = port
    service = ChatService(config)

    class BoundHandler(Handler):
        service = ChatService(config)
        tasks = TaskService(config)

    httpd = ThreadingHTTPServer((config.host, config.port), BoundHandler)
    print("=" * 60)
    print("Personal AI · MVP A1 已启动")
    print(f"  地址     : http://{config.host}:{config.port}")
    chain = " → ".join(
        service.provider.default_chain() + [service.provider.default_model()]
    )
    print(f"  Provider : {service.provider.name}")
    print(f"  调用链   : {chain}")
    print(f"  数据库   : {config.db_path}")
    print("  Ctrl+C 停止")
    print("=" * 60)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
