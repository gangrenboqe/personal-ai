"""Personal AI 冒烟测试。

零第三方依赖。自动拉起服务 → 打全部 API → 退出并汇总。

用法：
    python scripts/smoke_test.py
    python scripts/smoke_test.py --port 8799
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PASS, FAIL = "PASS", "FAIL"


def request(method: str, url: str, payload: dict | None = None, timeout: int = 10):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"raw": body}


def _js(base: str) -> str:
    try:
        with urllib.request.urlopen(f"{base}/app.js", timeout=5) as r:
            return r.read().decode("utf-8")
    except Exception:
        return ""


def _raw_post(base: str, path: str, body: dict, timeout: int = 60) -> tuple[int, str, str]:
    """返回 (状态码, Content-Type, 原始文本)。用于检查 SSE 这类非 JSON 响应。"""
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read().decode("utf-8", errors="replace")


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(f"  [{PASS if cond else FAIL}] {name}" + (f"  {detail}" if detail else ""))
    return cond


def wait_up(base: str, tries: int = 40) -> bool:
    for _ in range(tries):
        try:
            status, _ = request("GET", f"{base}/api/health", timeout=2)
            if status == 200:
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--provider", default="mock")
    args = ap.parse_args()

    base = f"http://127.0.0.1:{args.port}"
    proc = subprocess.Popen(
        [sys.executable, "-m", "personal_ai", "--serve", "--port", str(args.port)],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    results: list[bool] = []
    try:
        if not wait_up(base):
            out = proc.stdout.read() if proc.stdout else ""
            print("服务启动失败：")
            print(out[:2000])
            return 1
        print(f"\n服务已启动 {base}\n")

        print("1. 健康检查")
        code, health = request("GET", f"{base}/api/health")
        results.append(check("GET /api/health 200", code == 200, f"provider={health.get('provider', {}).get('name')}"))
        results.append(check("providers_available 非空", bool(health.get("providers_available"))))
        cfg = health.get("config", {})
        results.append(
            check(
                "密钥未泄露（只暴露布尔位）",
                "api_key" not in cfg and isinstance(cfg.get("api_key_set"), bool),
                f"api_key_set={cfg.get('api_key_set')}",
            )
        )

        print("\n2. Provider 可替换性")
        code, providers = request("GET", f"{base}/api/providers")
        names = [p["name"] for p in providers]
        results.append(check("至少 2 个 provider", len(providers) >= 2, str(names)))
        results.append(check("包含 openai_compat", "openai_compat" in names))

        print("\n3. 会话（过去）")
        code, created = request("POST", f"{base}/api/sessions", {"title": "冒烟测试"})
        sid = created.get("session_id", "")
        results.append(check("POST /api/sessions 201", code == 201, sid))

        print("\n4. 对话（现在）")
        code, chat = request("POST", f"{base}/api/chat", {"session_id": sid, "message": "冒烟测试消息"})
        results.append(check("POST /api/chat 200", code == 200))
        results.append(check("回答非空", bool(chat.get("content"))))
        results.append(check("携带调用链 chain", bool(chat.get("chain")), chat.get("chain", "")))
        results.append(check("标注是否 mock", "is_mock" in chat, f"is_mock={chat.get('is_mock')}"))

        print("\n5. 历史落库（数据属于用户）")
        code, msgs = request("GET", f"{base}/api/sessions/{sid}/messages")
        roles = [m["role"] for m in msgs]
        results.append(check("user + assistant 双双落库", roles == ["user", "assistant"], str(roles)))
        results.append(check("assistant 消息带 provider", any(m.get("provider") for m in msgs if m["role"] == "assistant")))

        print("\n6. 会话列表")
        code, sessions = request("GET", f"{base}/api/sessions")
        results.append(check("GET /api/sessions 200", code == 200, f"{len(sessions)} 个会话"))
        results.append(check("含消息计数", any(s.get("msg_count", 0) > 0 for s in sessions)))

        print("\n7. 错误处理")
        code, err = request("POST", f"{base}/api/chat", {"session_id": sid, "message": "   "})
        results.append(check("空消息返回 400", code == 400))
        code, err = request("GET", f"{base}/api/does-not-exist")
        results.append(check("未知路由返回 404", code == 404))

        print("\n8. 前端（A3 接入的过去/现在/未来 UI）")
        try:
            with urllib.request.urlopen(f"{base}/", timeout=5) as r:
                html = r.read().decode("utf-8")
                ctype = r.headers.get("Content-Type", "")
        except Exception as e:
            html, ctype = "", ""
            results.append(check("GET / 可访问", False, str(e)))
        results.append(check("GET / 返回 HTML", "<html" in html.lower(), ctype))
        results.append(
            check(
                "时间骨架 过去/现在/未来 存在",
                all(k in html for k in ("过去", "现在", "未来")),
            )
        )
        results.append(check("前端调用真实接口", "/api/chat" in html or "/api/chat" in _js(base)))

        print("\n9. 静态资源")
        for path, key in (("/app.js", "fetch"), ("/style.css", ".chat-log")):
            try:
                with urllib.request.urlopen(f"{base}{path}", timeout=5) as r:
                    body = r.read().decode("utf-8")
                results.append(check(f"GET {path} 200 且内容正确", key in body, f"{len(body)} bytes"))
            except Exception as e:
                results.append(check(f"GET {path} 200 且内容正确", False, str(e)))

        print("\n10. 任务（未来区）")
        created = request("POST", f"{base}/api/tasks", {"title": "冒烟任务"})
        tid = created[1].get("id", "")
        results.append(check("POST /api/tasks 201", created[0] == 201, tid))

        _, tasks = request("GET", f"{base}/api/tasks")
        results.append(check("GET /api/tasks 含新任务", any(t["id"] == tid for t in tasks)))

        code, _ = request("PATCH", f"{base}/api/tasks/{tid}", {"status": "doing"})
        _, tasks = request("GET", f"{base}/api/tasks")
        m = next((t for t in tasks if t["id"] == tid), {})
        results.append(check("PATCH 改状态生效", code == 200 and m.get("status") == "doing", str(m.get("status"))))

        code, _ = request("DELETE", f"{base}/api/tasks/{tid}")
        _, tasks = request("GET", f"{base}/api/tasks")
        results.append(check("DELETE 生效", code == 200 and not any(t["id"] == tid for t in tasks)))

        code, err = request("POST", f"{base}/api/tasks", {"title": "   "})
        results.append(check("空标题返回 400", code == 400))

        print("\n11. 流式输出（SSE）")
        code, ctype, raw = _raw_post(f"{base}", "/api/chat/stream", {"message": "流式测试：请分三行回答"})
        results.append(check("POST /api/chat/stream 200", code == 200, str(code)))
        results.append(check("响应类型是 text/event-stream", "text/event-stream" in ctype, ctype))
        data_lines = [l for l in raw.splitlines() if l.startswith("data:")]
        results.append(check("包含 data: 事件行", len(data_lines) >= 2, f"{len(data_lines)} 行"))
        results.append(check("以 [DONE] 收尾", "[DONE]" in raw))
        results.append(check("末事件带 done 与调用链", '"done": true' in raw.replace("True", "true")))

        # 流式回答应同样落库
        try:
            done_payload = json.loads(
                [l for l in data_lines if '"done"' in l][-1][5:].strip()
            )
            sid2 = done_payload.get("session_id", "")
            _, msgs2 = request("GET", f"{base}/api/sessions/{sid2}/messages")
            results.append(
                check("流式结果已落库", [m["role"] for m in msgs2] == ["user", "assistant"],
                      str([m["role"] for m in msgs2]))
            )
        except Exception as e:
            results.append(check("流式结果已落库", False, str(e)))

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    passed = sum(results)
    total = len(results)
    print("\n" + "=" * 56)
    print(f"冒烟测试结果：{passed}/{total} 通过")
    print("=" * 56)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
