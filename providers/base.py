"""Provider 抽象层。

这一层的存在就是整个项目的核心卖点：
模型不是硬编码的，Provider 不是硬编码的，换一家只需要换一个实现类。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ChatMessage:
    role: str  # system / user / assistant
    content: str


@dataclass
class ChatResponse:
    """一次模型调用的结果。

    chain 字段是「来源透明」原则的落地：
    用户看到的应该是 OmniRoute → SiliconFlow → DeepSeek-V3.2，
    而不只是一个 DeepSeek-V3.2。
    """

    content: str
    provider: str
    model: str
    chain: list[str] = field(default_factory=list)
    ok: bool = True
    error: str | None = None
    raw: dict[str, Any] | None = None

    def chain_text(self) -> str:
        return " → ".join(self.chain) if self.chain else self.model


class ProviderBase(ABC):
    """所有 Provider 的统一接口。新增一家供应商 = 新增一个子类。"""

    name = "base"
    display_name = "抽象 Provider"

    def __init__(self, config) -> None:
        self.config = config

    @abstractmethod
    def complete(self, messages: list[ChatMessage]) -> ChatResponse:
        """发送一批消息，返回一条回答。"""

    @abstractmethod
    def default_chain(self) -> list[str]:
        """该 Provider 默认的来源链（不含模型名）。"""

    def stream(self, messages: list[ChatMessage]):
        """流式返回文本增量。

        默认实现：把非流式结果一次性吐出，保证所有 Provider 都支持 stream()。
        真正支持流式的 Provider 覆写本方法。
        """
        resp = self.complete(messages)
        if resp.ok:
            yield resp.content
        else:
            yield f"[stream error] {resp.error}"

    def default_model(self) -> str:
        return self.config.model or "unknown"

    def describe(self) -> dict:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "model": self.default_model(),
            "chain": self.default_chain() + [self.default_model()],
            "configured": self.is_configured(),
        }

    def is_configured(self) -> bool:
        return True
