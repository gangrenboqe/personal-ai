"""32 号工单 S2 观察实验：模型会不会自己判断"这件事值得记"。

设计（设计者 2026-11-01 定稿）：
- 5 个对话，每个 3~5 轮合成历史（user 关键句 + 中性 assistant 应答），一次调用看模型是否自主调 create；
  有 tool_calls 则执行回填后再调一次收尾（agent loop，与生产 send() 同一实现 _complete_with_tools）。
- 对话 1~3 该记（preference / fact / conclusion）；对话 4~5 不该记（纯闲聊 / 未定论）——
  对话 4 和 5 是核心：测"不该记的没记"，否则记忆库会被污染。
- 判定线（先说死）：该记 x≥3 且误记 y=0 → 成立；x≥3 但 y≥1 → 有污染；x≤2 → 不成立。
- system prompt 只有弱提示（有工具可用、由你判断），无任何"必须调用"强指令。

预算：前置 smoke 1 次 + 5 对话 × 最多 2 次 = 最多 11 次模型调用，超 15 次说明 loop 空转。
密钥：优先环境变量 AGENT_API_KEY / PAI_OBS_API_KEY；未设则运行时从网关存储只读副本取（不写进代码）。
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
from personal_ai.core.chat import ChatMessage, ChatService
from personal_ai.providers import create as provider_create


def _resolve_api_key() -> str:
    key = (os.environ.get("AGENT_API_KEY") or os.environ.get("PAI_OBS_API_KEY") or "").strip()
    if key:
        return key
    # 运行时只读副本（WAL 库先拷再读），密钥不写进任何代码或文件
    omni = Path.home() / ".omniroute" / "storage.sqlite"
    tmp_db = Path(tempfile.gettempdir()) / "omni_key_v2obs.db"
    shutil.copy2(omni, tmp_db)
    for extra in ("-wal", "-shm"):
        src = Path(str(omni) + extra)
        if src.exists():
            shutil.copy2(src, str(tmp_db) + extra)
    con = sqlite3.connect(f"file:{tmp_db}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT key FROM api_keys WHERE revoked_at IS NULL LIMIT 1").fetchone()
    finally:
        con.close()
    return (row[0] if row else "").strip()


BASE_URL = (os.environ.get("AGENT_BASE_URL") or "http://127.0.0.1:20128/v1").strip()
MODEL = (os.environ.get("AGENT_MODEL") or "deepseek-ai/DeepSeek-V3.2").strip()

# 5 个对话：u=用户话术，a=中性助手应答（合成历史，制造干扰）；期望见 expected
CONVERSATIONS: list[dict] = [
    {
        "no": 1, "name": "基础能力：偏好", "expected_create": True, "expected_kind": "preference",
        "turns": [
            ("user", "最近在忙什么项目？"),
            ("assistant", "在整理个人 AI 项目的收尾工作。"),
            ("user", "进展还行，测试都补上了。对了，我习惯用 tab 缩进，别用空格。"),
            ("assistant", "了解，代码风格的事我记下了。"),
            ("user", "嗯，后面写代码都按这个来就行。"),
        ],
    },
    {
        "no": 2, "name": "长对话里捞事实", "expected_create": True, "expected_kind": "fact",
        "turns": [
            ("user", "帮我看看这个报错：KeyError: 'session_id'"),
            ("assistant", "这是字典取值没找到键，检查一下消息处理的入参。"),
            ("user", "修好了，是路由参数没接住。顺便说下，我的项目根目录是 E:\\DSHPersonalAI"),
            ("assistant", "好的，根目录确认了。"),
            ("user", "还有个依赖冲突的问题，改天再弄。对了今晚记得提醒我复查日志。"),
        ],
    },
    {
        "no": 3, "name": "决策类结论", "expected_create": True, "expected_kind": "conclusion",
        "turns": [
            ("user", "数据库选型讨论得怎么样了？"),
            ("assistant", "还在权衡 SQLite 和 Postgres。"),
            ("user", "我们定了：用 SQLite，不上 Postgres。"),
            ("assistant", "明确了，零依赖路线。"),
            ("user", "好，就按这个执行。"),
        ],
    },
    {
        "no": 4, "name": "闲聊不该记（核心）", "expected_create": False, "expected_kind": None,
        "turns": [
            ("user", "今天天气不错，你觉得呢"),
            ("assistant", "是啊，适合出去走走。"),
            ("user", "周末准备去公园转转。"),
            ("assistant", "不错的计划。"),
            ("user", "行了不聊了，干活去了。"),
        ],
    },
    {
        "no": 5, "name": "未定论不该记（核心）", "expected_create": False, "expected_kind": None,
        "turns": [
            ("user", "我可能要考虑换个方案，但还没定"),
            ("assistant", "可以先列一下两个方案的优劣。"),
            ("user", "嗯，回头再说吧。"),
            ("assistant", "好，需要时再聊。"),
            ("user", "今天先到这里。"),
        ],
    },
]


def _service(db: str) -> ChatService:
    config = Config(provider="openai_compat", base_url=BASE_URL,
                    api_key=_resolve_api_key(), model=MODEL, db_path=db)
    return ChatService(config)


def main() -> int:
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    svc = _service(db)

    total_calls = 0

    # ---- 前置 smoke（1 次）：解析链路端到端确认 ----
    smoke = svc._complete_with_tools([ChatMessage(role="user", content="回复 ok 两个字")])
    total_calls += 1
    print(f"[smoke] ok={smoke[0].ok} content={smoke[0].content[:40]!r} tool_calls={smoke[0].tool_calls}")

    creates_per_conv: dict[int, list[dict]] = {}
    for conv in CONVERSATIONS:
        messages = [ChatMessage(role="system", content=Config.system_prompt)]
        for role, text in conv["turns"]:
            messages.append(ChatMessage(role=role, content=text))
        resp, tool_log = svc._complete_with_tools(messages)
        total_calls += 1 + (1 if tool_log else 0)
        raw_calls = [
            {"name": c["name"], "arguments": c["arguments"]}
            for c in (resp.tool_calls or [])
        ]
        # 完整链路里的 create 次数（含首轮与回填轮——tool_log 有记录）
        creates = []
        for entry in tool_log:
            if entry.startswith("memory_create("):
                arg_str = entry[len("memory_create("):].split(") -> ")[0]
                try:
                    creates.append(json.loads(arg_str))
                except json.JSONDecodeError:
                    creates.append({"raw": arg_str})
        creates_per_conv[conv["no"]] = creates
        print("=" * 70)
        print(f"对话{conv['no']}「{conv['name']}」 期望记忆={conv['expected_create']}"
              + (f" kind={conv['expected_kind']}" if conv["expected_kind"] else ""))
        print(f"  模型回答: {resp.content[:120]!r}")
        print(f"  本轮 tool_calls(原始): {json.dumps(raw_calls, ensure_ascii=False)}")
        print(f"  全链路 create 记录: {json.dumps(creates, ensure_ascii=False)}")

    # ---- 汇总四个数 ----
    should = [c for c in CONVERSATIONS if c["expected_create"]]
    wrote = sum(1 for c in should if creates_per_conv[c["no"]])
    must_not = [c for c in CONVERSATIONS if not c["expected_create"]]
    miswritten = sum(len(creates_per_conv[c["no"]]) for c in must_not)
    kinds = {c["no"]: [a.get("kind") for a in creates_per_conv[c["no"]]] for c in should}
    dup = any(
        len({json.dumps(a, sort_keys=True) for a in creates_per_conv[c["no"]]})
        != len(creates_per_conv[c["no"]])
        or len(creates_per_conv[c["no"]]) > 1
        for c in CONVERSATIONS
    )

    print("=" * 70)
    print(f"四个数：该记 {wrote}/{len(should)} · 误写 {miswritten}/{len(must_not)} · "
          f"kind 实际={json.dumps(kinds, ensure_ascii=False)} · 重复/多次写入={dup}")
    print(f"总模型调用次数：{total_calls}（预算 ≤15）")
    if wrote >= 3 and miswritten == 0:
        verdict = "成立（可写进简历口径：模型能自主判断该记什么）"
    elif wrote >= 3:
        verdict = "能力有，但会污染记忆库，要加约束"
    else:
        verdict = "不成立（如实记录）"
    print(f"判定（先说死的线）：{verdict}")
    print("=" * 70)

    # 留库给人工抽查
    keep = input if False else None  # 不交互，直接清理
    try:
        os.unlink(db)
    except PermissionError:
        print(f"[临时库未删（句柄占用），路径：{db}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
