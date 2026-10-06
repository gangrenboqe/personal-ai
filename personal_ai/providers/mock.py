"""离线 Provider。

用途：
1. 没有 Key、没有网络时，系统依然能跑起来、能被演示。
2. 自动化测试不依赖外部服务。
3. 明确区分「真实回答」与「兜底回答」——它会在 chain 里标注自己是 mock。
"""

from __future__ import annotations

from .base import ChatMessage, ChatResponse, ProviderBase


class MockProvider(ProviderBase):
    name = "mock"
    display_name = "离线兜底 Provider（不产生真实模型调用）"

    def complete(self, messages: list[ChatMessage]) -> ChatResponse:
        last_user = ""
        for m in reversed(messages):
            if m.role == "user":
                last_user = m.content
                break
        content = (
            "[mock] 当前没有配置真实模型，这里是离线兜底回答。\n"
            f"你刚才说的是：{last_user}\n"
            "配置 PAI_BASE_URL / PAI_API_KEY / PAI_MODEL 后即可得到真实回答。"
        )
        return ChatResponse(
            content=content,
            provider=self.name,
            model=self.default_model(),
            chain=self.default_chain() + [self.default_model()],
            ok=True,
        )

    def default_chain(self) -> list[str]:
        return ["mock"]

    def default_model(self) -> str:
        return "mock-offline"

    def is_configured(self) -> bool:
        return True
