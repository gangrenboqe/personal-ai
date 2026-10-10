"""一键启动 Personal AI，并自动接入本机已有的 OmniRoute 网关。

用法：
    python scripts/start_local.py
    # 然后浏览器打开 http://127.0.0.1:8787

它做三件事：
1. 检查本机 OmniRoute 是否在运行（不在就提示你先 `omniroute serve`）
2. 从 OmniRoute 的本地库里**只读**取出已有的网关 Key（不新建、不改动任何配置）
3. 用这个 Key 启动 Personal AI，Key 只存在于进程内，不写入任何文件

如果只想离线看看界面（不调用真实模型）：
    python -m personal_ai --serve
"""

from __future__ import annotations

import socket
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from personal_ai.config import Config  # noqa: E402
from personal_ai.server import run_server  # noqa: E402

OMNI_PORT = 20128
OMNI_DB = Path.home() / ".omniroute" / "storage.sqlite"
MODEL = "deepseek-ai/DeepSeek-V3.2"


def gateway_up(host: str = "127.0.0.1", port: int = OMNI_PORT, timeout: float = 1.5) -> bool:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def gateway_key() -> str | None:
    """只读方式取本机 OmniRoute 已有的、未吊销的网关 Key。"""
    if not OMNI_DB.exists():
        return None
    con = sqlite3.connect(f"file:{OMNI_DB}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT key FROM api_keys WHERE revoked_at IS NULL LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    return row[0] if row else None


def wait_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """真正的「服务就绪」判断：端口能被连上。

    以前的做法是在 .bat 里 `timeout /t 6` 固定等 6 秒再开浏览器——那是猜的。
    这里改成轮询真实端口，快了就早开，慢了也不开个空白页给你。
    """
    return gateway_up(host, port, timeout)


def open_browser_when_ready(port: int = 8787) -> None:
    """后台等到服务真的起来，再自动打开浏览器（只开一次）。

    为什么挪到这里做：以前是 .bat 里起一个最小化 cmd 窗口 `start /min` 去延时开浏览器，
    那个窗口会在几秒后自己消失 —— 看起来就像"程序闪一下就没了"，非常容易误判为崩溃。
    """
    import threading
    import time
    import webbrowser

    def _run() -> None:
        for _ in range(120):  # 最多等 120 秒
            time.sleep(0.5)
            if wait_port_open("127.0.0.1", port, timeout=0.5):
                try:
                    webbrowser.open(f"http://127.0.0.1:{port}")
                    print("[浏览器] 已自动打开 http://127.0.0.1:%d" % port)
                except Exception as e:
                    print("[浏览器] 自动打开失败（%s），请手动访问 http://127.0.0.1:%d" % (e, port))
                return
        print("[浏览器] 等待超时，请手动访问 http://127.0.0.1:%d" % port)

    threading.Thread(target=_run, daemon=True).start()


def warm_up_async(cfg: Config) -> None:
    """后台预热一次真实调用。

    为什么要有这一步：上游模型**冷启动**实测要 10～12 秒才出第一个字，
    预热后稳定在 1.6 秒左右。演示/面试现场让对方等 12 秒，观感很糟。

    做法：起一个后台线程，服务起来 2 秒后静默问一句极短的话。
    失败完全不影响正常使用（最多回到"第一句慢"），所以异常一律吞掉并提示。
    """
    import threading
    import time

    def _run() -> None:
        time.sleep(2)
        try:
            from personal_ai.core import ChatService

            svc = ChatService(cfg)
            sid = svc.new_session(title="预热（可删）")
            t0 = time.time()
            svc.send(sid, "hi")
            print("[预热] 完成，耗时 %.1fs —— 现在提问会快很多" % (time.time() - t0))
        except Exception as e:  # 预热失败无所谓，不影响正常服务
            print("[预热] 跳过（%s）—— 不影响使用，只是第一句仍会慢" % e)

    threading.Thread(target=_run, daemon=True).start()


def main() -> int:
    if not gateway_up():
        print("=" * 60)
        print(f"OmniRoute 网关没有运行（端口 {OMNI_PORT} 未监听）。")
        print("先启动它：")
        print("    omniroute serve")
        print("然后再运行本脚本。")
        print()
        print("如果只想离线看界面，不调用真实模型：")
        print("    python -m personal_ai --serve")
        print("=" * 60)
        return 1

    key = gateway_key()
    if not key:
        print("没找到可用的网关 Key。请先在 OmniRoute 面板里创建一个。")
        return 1

    cfg = Config(
        provider="openai_compat",
        base_url=f"http://127.0.0.1:{OMNI_PORT}/v1",
        api_key=key,
        model=MODEL,
        port=8787,
    )
    open_browser_when_ready(8787)
    if "--no-warmup" in sys.argv:
        print("[预热] 已按 --no-warmup 跳过 —— 第一句提问会慢（约 10~12 秒）")
    else:
        print("[预热] 正在后台预热模型，完成后第一句会快很多…")
        warm_up_async(cfg)
    run_server(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
