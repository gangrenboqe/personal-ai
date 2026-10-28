"""Provider 注册表。

新增一家供应商只需要：
1. 写一个 ProviderBase 子类
2. 在下面 register 一次

上层代码完全不需要改动。这就是「Provider 可替换」的落地点。
"""

from __future__ import annotations

from .base import ChatMessage, ChatResponse, ProviderBase
from .mock import MockProvider
from .openai_compat import OpenAICompatProvider

_REGISTRY: dict[str, type[ProviderBase]] = {}


def register(cls: type[ProviderBase]) -> type[ProviderBase]:
    _REGISTRY[cls.name] = cls
    return cls


register(MockProvider)
register(OpenAICompatProvider)


def available() -> list[str]:
    return sorted(_REGISTRY.keys())


def create(name: str, config) -> ProviderBase:
    if name not in _REGISTRY:
        raise KeyError(f"未知 provider: {name}。可选：{available()}")
    return _REGISTRY[name](config)


def describe_all(config) -> list[dict]:
    return [cls(config).describe() for cls in _REGISTRY.values()]


__all__ = [
    "ChatMessage",
    "ChatResponse",
    "ProviderBase",
    "MockProvider",
    "OpenAICompatProvider",
    "register",
    "available",
    "create",
    "describe_all",
]
