"""OpenAI 兼容协议 Provider。

覆盖绝大多数供应商：SiliconFlow、DeepSeek 官方、OpenAI、Moonshot、通义、vLLM、Ollama、
以及 OmniRoute 这类网关——只要它们暴露 /chat/completions。

只用标准库 urllib，不引入 requests，保证零依赖可跑。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from .base import ChatMessage, ChatResponse, ProviderBase


class OpenAICompatProvider(ProviderBase):
    name = "openai_compat"
    display_name = "OpenAI 兼容协议 Provider"

    def is_configured(self) -> bool:
        return bool(self.config.base_url and self.config.api_key)

    def _endpoint(self) -> str:
        base = self.config.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        if not base.endswith("/v1"):
            base = base + "/v1"
        return base + "/chat/completions"

    def _host_label(self) -> str:
        try:
            host = urlparse(self.config.base_url).netloc
        except ValueError:
            host = self.config.base_url
        return host or "unknown-host"

    def _blank(self, error: str, raw: dict | None = None) -> ChatResponse:
        return ChatResponse(
            content="",
            provider=self.name,
            model=self.default_model(),
            chain=self.default_chain() + [self.default_model()],
            ok=False,
            error=error,
            raw=raw,
        )

    def complete(self, messages: list[ChatMessage]) -> ChatResponse:
        """发送一次请求；失败且可重试时自动重试。

        为什么要有重试：上游供应商冷启动实测可能超过 60 秒（2026-10-06 记录到
        114 秒才返回、以及 504 网关超时），单次请求失败不代表链路不可用。
        """
        if not self.is_configured():
            return self._blank("未配置 PAI_BASE_URL / PAI_API_KEY")

        attempts = 1 + max(0, self.config.retries)
        last: ChatResponse | None = None
        for i in range(attempts):
            resp, retryable = self._request(messages)
            if resp.ok:
                return resp
            last = resp
            if not retryable or i == attempts - 1:
                break
            time.sleep(min(8.0, 1.5 * (i + 1)))
        return last  # type: ignore[return-value]

    def _request(self, messages: list[ChatMessage]) -> tuple[ChatResponse, bool]:
        """返回 (响应, 是否值得重试)。"""
        payload = {
            "model": self.config.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._endpoint(),
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:500]
            # 5xx 与 429（限流）值得重试；4xx（如密钥错误）重试也没用
            retryable = e.code >= 500 or e.code == 429
            return self._blank(f"HTTP {e.code}: {detail}"), retryable
        except Exception as e:  # 网络层异常（含超时）统一收敛，不让服务崩
            return self._blank(f"{type(e).__name__}: {e}"), True

        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return (
                self._blank(
                    f"响应格式无法解析: {str(body)[:500]}",
                    body if isinstance(body, dict) else None,
                ),
                False,
            )

        real_model = ""
        if isinstance(body, dict):
            real_model = str(body.get("model") or "")

        return (
            ChatResponse(
                content=content,
                provider=self.name,
                model=real_model or self.default_model(),
                chain=self.default_chain() + [real_model or self.default_model()],
                ok=True,
                raw=body,
            ),
            False,
        )

    def stream(self, messages: list[ChatMessage]):
        """SSE（Server-Sent Events，服务端事件流）流式返回文本增量。

        SSE 是什么：服务端可以一小段一小段地把内容推给浏览器，
        前端边收边显示 —— 这就是 ChatGPT 那种"逐字蹦出来"的效果。
        协议格式就是一堆 `data: 内容` 的行，空行分隔，最后以 `data: [DONE]` 结束。
        """
        if not self.is_configured():
            yield "[stream error] 未配置 PAI_BASE_URL / PAI_API_KEY"
            return

        payload = {
            "model": self.config.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": True,
        }
        req = urllib.request.Request(
            self._endpoint(),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    for choice in obj.get("choices") or []:
                        piece = (choice.get("delta") or {}).get("content")
                        if piece:
                            yield piece
        except Exception as e:  # 流式失败也要让前端知道，而不是静默结束
            yield f"\n[stream error] {type(e).__name__}: {e}"

    def default_chain(self) -> list[str]:
        return ["openai-compat", self._host_label()]
