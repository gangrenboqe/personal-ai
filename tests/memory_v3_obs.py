"""记忆层 v3 观察实验（施工单 38）：四项验收的 live 取数脚本。

任务 1/2：重跑 v2 的 5 个对话（kind 判对数 ≥3/3；入库条数 =3；不该记 0 误写）
任务 3：5 个必须查记忆才能答的问题（≥4 处理正确；查不到的那个必须承认"没有记录"）
任务 4：3 个 history_search 场景（能查到 / 查不到 / 模糊匹配）

前置：网关 127.0.0.1:20128 健康；密钥环境变量或网关存储只读副本。
运行：python tests/memory_v3_obs.py
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from personal_ai.config import Config
from personal_ai.core.chat import ChatService
from personal_ai.providers.base import ChatMessage  # noqa: F401

BASE_URL = (os.environ.get("AGENT_BASE_URL") or "http://127.0.0.1:20128/v1").strip()
MODEL = (os.environ.get("AGENT_MODEL") or "deepseek-ai/DeepSeek-V3.2").strip()


def _api_key() -> str:
    key = (os.environ.get("AGENT_API_KEY") or os.environ.get("PAI_OBS_API_KEY") or "").strip()
    if key:
        return key
    omni = Path.home() / ".omniroute" / "storage.sqlite"
    tmp = Path(tempfile.gettempdir()) / "omni_key_v3obs.db"
    try:
        shutil.copy2(omni, tmp)
        for extra in ("-wal", "-shm"):
            src = Path(str(omni) + extra)
            if src.exists():
                shutil.copy2(src, str(tmp) + extra)
        con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT key FROM api_keys WHERE revoked_at IS NULL LIMIT 1").fetchone()
        finally:
            con.close()
        return (row[0] if row else "").strip()
    except OSError:
        return ""


# v2 同款 5 对话（任务 1/2 验收口径与 v2 对齐：该记 3、不该记 2）
CONVERSATIONS = [
    {"no": 1, "name": "偏好", "expect": True, "kind": "preference", "turns": [
        ("user", "最近在忙什么项目？"),
        ("assistant", "在整理个人 AI 项目的收尾工作。"),
        ("user", "进展还行，测试都补上了。对了，我习惯用 tab 缩进，别用空格。"),
        ("assistant", "了解，代码风格的事我记下了。"),
        ("user", "嗯，后面写代码都按这个来就行。"),
    ]},
    {"no": 2, "name": "长对话捞事实", "expect": True, "kind": "fact", "turns": [
        ("user", "帮我看看这个报错：KeyError: 'session_id'"),
        ("assistant", "这是字典取值没找到键，检查一下消息处理的入参。"),
        ("user", "修好了，是路由参数没接住。顺便说下，我的项目根目录是 E:\\DSHPersonalAI"),
        ("assistant", "好的，根目录确认了。"),
        ("user", "还有个依赖冲突的问题，改天再弄。对了今晚记得提醒我复查日志。"),
    ]},
    {"no": 3, "name": "决策类结论", "expect": True, "kind": "conclusion", "turns": [
        ("user", "数据库选型讨论得怎么样了？"),
        ("assistant", "还在权衡 SQLite 和 Postgres。"),
        ("user", "我们定了：用 SQLite，不上 Postgres。"),
        ("assistant", "明确了，零依赖路线。"),
        ("user", "好，就按这个执行。"),
    ]},
    {"no": 4, "name": "闲聊不该记", "expect": False, "kind": None, "turns": [
        ("user", "今天天气不错，你觉得呢"),
        ("assistant", "是啊，适合出去走走。"),
        ("user", "周末准备去公园转转。"),
        ("assistant", "不错的计划。"),
        ("user", "行了不聊了，干活去了。"),
    ]},
    {"no": 5, "name": "未定论不该记", "expect": False, "kind": None, "turns": [
        ("user", "我可能要考虑换个方案，但还没定"),
        ("assistant", "可以先列一下两个方案的优劣。"),
        ("user", "嗯，回头再说吧。"),
        ("assistant", "好，需要时再聊。"),
        ("user", "今天先到这里。"),
    ]},
]

# 任务 3：5 个必查问题。in_memory=True 表示 v2 记忆里有；key=答对判据；admit=查不到时应承认
RECALL_QUESTIONS = [
    {"q": "我之前说我的项目在哪个目录？", "in_memory": True, "key": "DSHPersonalAI"},
    {"q": "我偏好什么缩进风格？", "in_memory": True, "key": "tab"},
    {"q": "我有没有跟你提过数据库选型？", "in_memory": True, "key": "SQLite"},
    {"q": "我上次说的那个模型叫什么？", "in_memory": False, "key": None},  # 必须承认没有记录
    {"q": "我平时喜欢代码怎么排版？", "in_memory": "fuzzy", "key": "tab"},  # 模糊问法，命中缩进偏好
]

ADMIT_MARKS = ("没有相关记录", "没有记录", "没有找到", "不记得", "没有印象", "未记录", "找不到")

# 任务 4：3 个 history_search 场景（先造 3 段历史对话进 messages 表）
HISTORY_SEEDS = [
    ("数据库选型讨论", "我们定了：用 SQLite，不上 Postgres，走零依赖路线。"),
    ("分词方案讨论", "检索的分词方案最后选了 trigram，不用自写二元分词了。"),
    ("部署排障", "昨晚部署卡在端口占用，最后发现是旧实例没退干净。"),
]
HISTORY_SCENARIOS = [
    {"no": 1, "name": "能查到", "q": "我之前聊过 SQLite 的选型吗？", "hit": True, "key": "SQLite"},
    {"no": 2, "name": "查不到", "q": "我有没有聊过量子迷宫的剧情？", "hit": False, "key": None},
    {"no": 3, "name": "模糊匹配", "q": "那个分词方案的事你还记得吗？", "hit": True, "key": "trigram"},
]


def main() -> int:
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    config = Config(provider="openai_compat", base_url=BASE_URL,
                    api_key=_api_key(), model=MODEL, db_path=db)
    svc = ChatService(config)
    total_calls = 0

    # ---- 任务 1/2：重跑 v2 的 5 对话 ----
    creates: dict[int, list[dict]] = {}
    for conv in CONVERSATIONS:
        messages = [ChatMessage(role="system", content=Config.system_prompt)]
        for role, text in conv["turns"]:
            messages.append(ChatMessage(role=role, content=text))
        resp, tool_log = svc._complete_with_tools(messages)
        total_calls += 1 + (1 if tool_log else 0)
        cs = [json.loads(e[len("memory_create("):].split(") -> ")[0])
              for e in tool_log if e.startswith("memory_create(")]
        creates[conv["no"]] = cs
        print(f"对话{conv['no']}「{conv['name']}」期望={conv['expect']}"
              f" create={json.dumps(cs, ensure_ascii=False)}")

    should = [c for c in CONVERSATIONS if c["expect"]]
    kind_ok = sum(1 for c in should
                  if creates[c["no"]] and all(a.get("kind") == c["kind"] for a in creates[c["no"]]))
    total_in库 = sum(len(creates[c["no"]]) for c in CONVERSATIONS)
    miswrite = sum(len(creates[c["no"]]) for c in CONVERSATIONS if not c["expect"])
    print("=" * 64)
    print(f"任务1/2 结果：kind 判对 {kind_ok}/3 · 入库共 {total_in库} 条（应 3）· 不该记误写 {miswrite}/2")

    # ---- 任务 3：5 个必查问题 ----
    # 🔴 2026-11-05 修：调用失败（429/超时）和"模型答错"是两件事。
    # 前者是环境问题，不能算到模型头上，所以单独计数、不进正确率分母。
    recall_ok, admit_ok = 0, None
    call_fail = 0
    answered = 0
    for i, item in enumerate(RECALL_QUESTIONS, 1):
        resp, tool_log = svc._complete_with_tools([ChatMessage(role="user", content=item["q"])])
        total_calls += 1 + (1 if tool_log else 0)
        if not resp.ok:
            call_fail += 1
            print(f"问题{i}：⛔ 调用失败（{str(resp.error)[:50]}）—— 不计入")
            continue
        answered += 1
        searched = any(e.startswith("memory_search(") for e in tool_log)
        reply = resp.content or ""
        if item["in_memory"] is True:
            ok = item["key"].lower() in reply.lower()
            recall_ok += ok
        else:
            ok = any(m in reply for m in ADMIT_MARKS) and not resp.tool_calls
            admit_ok = ok
            recall_ok += ok
        print(f"问题{i}（记忆{'有' if item['in_memory'] is True else '无' if item['in_memory'] is False else '模糊'}"
              f"·应查=True·主动查={searched}）：{'PASS' if ok else 'FAIL'}  回答: {reply[:90]!r}")
    print("=" * 64)
    print(f"任务3 结果：{recall_ok}/{answered} 处理正确（另有 {call_fail} 次调用失败未计入）"
          f" · 查不到时承认没有记录：{admit_ok}")

    # ---- 任务 4：造 3 段历史 → 3 场景 ----
    from personal_ai.storage import add_message, create_session
    for title, text in HISTORY_SEEDS:
        sid = svc.new_session(title=title)
        add_message(db, sid, "user", text)
    hist_ok = 0
    hist_fail = 0
    hist_answered = 0
    for sc in HISTORY_SCENARIOS:
        resp, tool_log = svc._complete_with_tools([ChatMessage(role="user", content=sc["q"])])
        total_calls += 1 + (1 if tool_log else 0)
        if not resp.ok:
            hist_fail += 1
            print(f"history 场景{sc['no']}「{sc['name']}」：⛔ 调用失败（{str(resp.error)[:45]}）—— 不计入")
            continue
        hist_answered += 1
        called = any(e.startswith("history_search(") for e in tool_log)
        reply = resp.content or ""
        if sc["hit"]:
            ok = sc["key"].lower() in reply.lower()
        else:
            ok = any(m in reply for m in ADMIT_MARKS)
        hist_ok += ok
        print(f"history 场景{sc['no']}「{sc['name']}」主动查={called}：{'PASS' if ok else 'FAIL'}"
              f"  回答: {reply[:80]!r}")
    print("=" * 64)
    print(f"任务4 结果：{hist_ok}/{hist_answered} 场景正确（另有 {hist_fail} 次调用失败未计入）")
    print(f"总模型调用：{total_calls}")
    try:
        os.unlink(db)
    except PermissionError:
        print(f"[临时库未删：{db}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
