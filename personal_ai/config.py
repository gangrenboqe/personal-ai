"""配置层。

所有敏感值一律从环境变量读取，不写入代码，不进 git。

环境变量：
    PAI_PROVIDER     provider 名称，默认 mock（离线可跑）
    PAI_BASE_URL     OpenAI 兼容端点，如 https://api.siliconflow.cn/v1
    PAI_API_KEY      API Key（绝不落盘、绝不进 git）
    PAI_MODEL        模型名，如 deepseek-ai/DeepSeek-V3.2
    PAI_DB_PATH      SQLite 路径
    PAI_HOST / PAI_PORT
    PAI_SYSTEM_PROMPT
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# 项目根目录：本仓库根（代码自动定位，不依赖具体存放路径）
ROOT = Path(__file__).resolve().parents[1]


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


@dataclass
class Config:
    provider: str = "mock"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    db_path: str = ""
    host: str = "127.0.0.1"
    port: int = 8787
    system_prompt: str = "你是 Personal AI 的助手。回答简洁、准确、不编造事实。"
    # 上游供应商冷启动可能超过 60 秒（实测 2026-10-06 有 114 秒才返回的案例），
    # 因此默认给足超时，并允许重试。
    timeout: int = 180
    retries: int = 2

    def __post_init__(self) -> None:
        if not self.db_path:
            self.db_path = str(ROOT / "data" / "personal_ai.db")

    @classmethod
    def from_env(cls) -> "Config":
        port_raw = _env("PAI_PORT", "8787")
        try:
            port = int(port_raw)
        except ValueError:
            port = 8787
        try:
            timeout = int(_env("PAI_TIMEOUT", "180"))
        except ValueError:
            timeout = 180
        try:
            retries = int(_env("PAI_RETRIES", "2"))
        except ValueError:
            retries = 2
        return cls(
            provider=_env("PAI_PROVIDER", "mock"),
            base_url=_env("PAI_BASE_URL"),
            api_key=_env("PAI_API_KEY"),
            model=_env("PAI_MODEL"),
            db_path=_env("PAI_DB_PATH"),
            host=_env("PAI_HOST", "127.0.0.1"),
            port=port,
            system_prompt=_env("PAI_SYSTEM_PROMPT") or cls.system_prompt,
            timeout=timeout,
            retries=max(0, retries),
        )

    def describe(self) -> dict:
        """对外暴露的配置快照。api_key 永远只返回是否存在，不返回内容。"""
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "api_key_set": bool(self.api_key),
            "db_path": self.db_path,
            "host": self.host,
            "port": self.port,
            "timeout": self.timeout,
            "retries": self.retries,
        }
