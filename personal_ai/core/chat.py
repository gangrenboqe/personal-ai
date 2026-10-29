"""ChatService：一次对话的完整流程。

职责边界：
- 它只做编排，不关心 HTTP、不关心 UI。
- Provider 由注册表创建，可替换。
- 每一条消息都落库，并保存真实的 provider / model / chain。
"""

from __future__ import annotations

from ..config import Config
from ..memory import MemoryStore
from ..providers import ChatMessage, ChatResponse, available, create
from ..storage import add_message, create_session, init_db, list_messages, list_sessions


class ChatService:
    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config.from_env()
        init_db(self.config.db_path)
        self.provider = create(self.config.provider, self.config)
        # 记忆层（施工单 29）：回答前检索，命中才注入，见 _build_messages
        self.memory = MemoryStore(self.config.db_path)

    # ---------- 会话 ----------

    def new_session(self, title: str = "") -> str:
        return create_session(self.config.db_path, title=title, provider=self.provider.name)

    def sessions(self) -> list[dict]:
        return list_sessions(self.config.db_path)

    def history(self, session_id: str) -> list[dict]:
        return list_messages(self.config.db_path, session_id)

    # ---------- 对话 ----------

    def send(self, session_id: str, text: str) -> dict:
        """发送一条消息，落库，调用模型，把回答也落库。"""
        if not session_id:
            session_id = self.new_session(title=text[:20])

        add_message(self.config.db_path, session_id, "user", text)

        messages = self._build_messages(session_id)
        resp: ChatResponse = self.provider.complete(messages)

        add_message(
            self.config.db_path,
            session_id,
            "assistant",
            resp.content,
            provider=resp.provider,
            model=resp.model,
            chain=resp.chain_text(),
            ok=resp.ok,
        )

        return {
            "session_id": session_id,
            "content": resp.content,
            "ok": resp.ok,
            "error": resp.error,
            "provider": resp.provider,
            "model": resp.model,
            "chain": resp.chain_text(),
            "is_mock": resp.provider == "mock",
        }

    def prepare(self, session_id: str, text: str) -> str:
        """流式专用：先落库用户消息，返回可用的 session_id。"""
        if not session_id:
            session_id = self.new_session(title=text[:20])
        add_message(self.config.db_path, session_id, "user", text)
        return session_id

    def context_for(self, session_id: str) -> list[ChatMessage]:
        return self._build_messages(session_id)

    def finish(self, session_id: str, content: str, chain: str = "", model: str = "", ok: bool = True) -> None:
        """流式专用：把完整回答一次性落库（流已经结束，不再需要逐块存）。"""
        add_message(
            self.config.db_path,
            session_id,
            "assistant",
            content,
            provider=self.provider.name,
            model=model or self.provider.default_model(),
            chain=chain or (" → ".join(self.provider.default_chain() + [self.provider.default_model()])),
            ok=ok,
        )

    def _build_messages(self, session_id: str) -> list[ChatMessage]:
        """组装发给模型的上下文。

        两条过滤规则（2026-10-10 加，起因是一次真实的答非所问）：
        1) 离线兜底（mock）产生的假回答不进上下文
           —— 它会被模型当成"你自己说过的话"，导致答非所问。
        2) 生成失败（ok=False）的回答也不进上下文
           —— 那是一次失败记录，不是有效对话。
        它们仍然留在数据库里（历史可查），只是不再参与后续推理。

        记忆层注入（施工单 29 · 3.4）：
        取最后一条用户消息做 FTS5 检索（top 5），命中才拼一条记忆 system 消息，
        带 source / written_at / verified_at 三元组原样给模型自行权衡；
        不命中不注入，不塞空内容占上下文。
        """
        out = [ChatMessage(role="system", content=self.config.system_prompt)]
        history = list_messages(self.config.db_path, session_id, limit=50)
        last_user = next(
            (m["content"] for m in reversed(history) if m["role"] == "user"), ""
        )
        if last_user:
            block = self.memory.get_context_block(last_user)
            if block:
                out.append(ChatMessage(role="system", content=block))
        for m in history:
            if m["role"] not in ("user", "assistant"):
                continue
            if m["role"] == "assistant":
                if m.get("provider") == "mock":
                    continue
                if m.get("ok") is False:
                    continue
            out.append(ChatMessage(role=m["role"], content=m["content"]))
        return out

    # ---------- 元信息 ----------

    def status(self) -> dict:
        return {
            "version": "0.1.0-a1",
            "provider": self.provider.describe(),
            "providers_available": available(),
            "config": self.config.describe(),
        }
