"""Personal AI 三分钟验证 demo —— 不需要配置任何 API key。

跑什么：
  1. 一次问答（离线 mock Provider，回答以 [mock] 开头，诚实标注"未调用真实模型"）
  2. 写一条记忆（长期记忆层：带来源与时间，不物理删除）
  3. 问一个相关的问题 → 看记忆被检索出来、带着"来源/写入/核验"三元组注入进模型上下文

诚实的边界：第 3 步的 mock 回答是固定占位话术，不会引用记忆内容——
mock 只证明"检索与注入管道在工作"；真实模型会基于注入块回答（live 实测 19~20/20，见 README）。

运行：python demo.py    （仓库根目录，Python 3.9+，零第三方依赖）
"""

from __future__ import annotations

import os
import tempfile

from personal_ai.config import Config
from personal_ai.core.chat import ChatService


def _sep(title: str) -> None:
    print("\n" + "=" * 64 + f"\n[{title}]\n" + "=" * 64)


def main() -> None:
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    svc = ChatService(Config(provider="mock", db_path=db))
    store = svc.memory

    _sep("第 1 步 · 一次问答（离线 mock，不调真实模型）")
    out = svc.send("", "你好，你是谁？")
    print("用户   : 你好，你是谁？")
    print(f"回答   : {out['content'][:120]}")
    print(f"调用链 : {out['chain']}   ← 来源透明：每条回答都带真实链路")

    _sep("第 2 步 · 写一条记忆（长期记忆层）")
    path = "/memories/preferences/回答风格.md"
    store.create(path, "用户偏好简洁的回答，不用列表。", kind="preference", source="用户说的")
    print(f"已写入 : {path}")
    print("内容   : 用户偏好简洁的回答，不用列表。")
    print(f"元数据 : 来源=用户说的 · written_at 自动记录 · 从未核过条目 {len(store.never_verified())} 条（待人工核验）")

    _sep("第 3 步 · 再问相关的问题 → 记忆被检索并注入")
    out2 = svc.send("", "我喜欢什么样的回答风格？")
    ctx = svc.context_for(out2["session_id"])
    blocks = [m.content for m in ctx if m.role == "system" and "记忆层" in m.content]
    print("用户   : 我喜欢什么样的回答风格？")
    if blocks:
        print("注入进模型上下文的记忆块（检索命中，三元组原样给出）：")
        for line in blocks[0].split("\n"):
            print("    " + line)
    else:
        print("（未命中注入——这与预期不符，请提 issue）")
    print(f"\n模型回答（mock 占位，不引用记忆内容）: {out2['content'][:100]}")
    print("诚实说明：mock 只证明『检索与注入管道在工作』；")
    print("真实模型会基于注入块回答——live 实测检索路径 19~20/20（README「记忆层」一节）。")

    _sep("验证完毕")
    print("想跑真实模型？配 PAI_BASE_URL / PAI_API_KEY / PAI_MODEL 三个环境变量即可（README「接一个真实模型」）。")
    print("demo 用的是临时库，运行结束后自动删除，不在仓库里留任何文件。")
    try:
        os.unlink(db)
    except PermissionError:
        pass


if __name__ == "__main__":
    main()
