"""命令行入口。

    python -m personal_ai --once "你好"        # 单次问答，打印真实调用链
    python -m personal_ai --serve              # 启动 HTTP 服务
    python -m personal_ai --status             # 查看当前配置与 provider
"""

from __future__ import annotations

import argparse
import json

from .config import Config
from .core import ChatService
from .providers import available


def cmd_status(config: Config) -> int:
    svc = ChatService(config)
    print(json.dumps(svc.status(), ensure_ascii=False, indent=2))
    return 0


def cmd_once(config: Config, text: str) -> int:
    svc = ChatService(config)
    sid = svc.new_session(title=text[:20])
    result = svc.send(sid, text)
    print("-" * 60)
    print(f"会话       : {result['session_id']}")
    print(f"调用链     : {result['chain']}")
    print(f"真实模型   : {result['model']}")
    print(f"是否离线兜底: {'是（mock）' if result['is_mock'] else '否'}")
    print("-" * 60)
    print(result["content"] or result["error"])
    print("-" * 60)
    return 0 if result["ok"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="personal_ai", description="Personal AI MVP")
    parser.add_argument("--serve", action="store_true", help="启动 HTTP 服务")
    parser.add_argument("--once", metavar="TEXT", help="发送一条消息并退出")
    parser.add_argument("--status", action="store_true", help="查看当前状态")
    parser.add_argument("--provider", help=f"Provider 名称，可选：{available()}")
    parser.add_argument("--port", type=int, help="HTTP 端口")
    args = parser.parse_args()

    config = Config.from_env()
    if args.provider:
        config.provider = args.provider

    if args.once:
        return cmd_once(config, args.once)
    if args.status:
        return cmd_status(config)

    from .server import run_server

    run_server(config, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
