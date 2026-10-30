"""记忆层两段评测（施工单 29 验收 5.2）。

🔴 判据来自 arXiv 2610.36308 的教训：「检索」和「答对」混成一个分数，
   就分不清失败是「记忆里没存对」还是「没把记忆用上」。所以两段必须分开报：

    ① 存对了吗   —— 纯逻辑，不需要模型：
                    写入 A，再写入 A'（更新版），检查 view 拿到 A'、
                    A 被标 superseded_by、检索只返回 A'。
    ② 用上了吗   —— 脚本化后端模拟，两小项各自计分：
                    (a) 对照：正确答案直接塞上下文（绕过检索），模拟模型能否答对
                    (b) 正常路径：search → 注入块 → 模拟模型，能否答对

🔴 三个分数永远分开报，绝不合并成一个总分。

离线可跑：不联网、不依赖网关。ScriptedRecallModel 是脚本化后端（上下文里
有正确记忆就"用上"，没有就答"没有找到"），它测的是「记忆有没有被送进
上下文」这条管道与注入块的可读性，不是真实 LLM 的成绩——
真实模型分数要等网关修复后，换后端重跑本脚本才能得出。

运行：
    python tests/memory_eval.py                 # 默认建临时库
    python tests/memory_eval.py --db 路径       # 指定库
退出码：三个分数全满 → 0；否则 1（不放宽判据）。

修订记录：
    2026-10-29 返工单 30：场景 5/11 加严（新事实 >36 字符、答案词移到最末）——
    老 snippet 截断 bug 下这两条会挂（答案词被切成 nan/tri），修复后（注入给完整
    content）任何 SQLite 版本行为下都应通过。判据只加严、未放宽。
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from personal_ai.memory import MemoryStore, build_context_block  # noqa: E402


# ---------------------------------------------------------------- 场景（20 条）

@dataclass
class Scenario:
    topic: str            # 场景名
    kind: str             # fact | preference | conclusion | skill
    old_path: str         # 旧记忆路径
    old_content: str      # 旧事实 A
    new_path: str         # 新记忆路径
    new_content: str      # 更新版 A'
    question: str         # 用户会问的问题（词面与记忆重叠，供检索）
    answer_key: str       # 只出现在 A' 里的关键词（旧 A 里没有）


SCENARIOS: list[Scenario] = [
    Scenario("回答风格偏好", "preference",
             "/memories/preferences/sc01/风格.md", "用户偏好简洁的回答，越短越好。",
             "/memories/preferences/sc01/风格_v2.md", "用户现在偏好详细的回答，要带代码示例。",
             "用户对回答风格有什么要求？", "带代码示例"),
    Scenario("时区", "fact",
             "/memories/fact/sc02/时区.md", "用户所在时区是东八区。",
             "/memories/fact/sc02/时区_v2.md", "用户所在时区是 UTC+1。",
             "用户所在时区是哪个？", "UTC+1"),
    Scenario("服务端口", "fact",
             "/memories/fact/sc03/端口.md", "服务监听端口是 8787。",
             "/memories/fact/sc03/端口_v2.md", "服务监听端口改为 20128。",
             "服务监听端口是多少？", "20128"),
    Scenario("跑测试命令", "skill",
             "/memories/skills/sc04/测试.md", "跑全量测试用 python -m unittest discover。",
             "/memories/skills/sc04/测试_v2.md", "跑全量测试用 python -m unittest tests.test_units。",
             "跑全量测试用什么命令？", "tests.test_units"),
    Scenario("检索方案结论", "conclusion",
             "/memories/conclusions/sc05/检索.md", "项目检索层计划用向量库实现。",
             # 返工单 30 加严：内容 >36 字符、答案词在最末（老 snippet 截断 bug 的回归场景）
             "/memories/conclusions/sc05/检索_v2.md",
             "项目检索层经过两轮评审最终定案：采用 SQLite 全文索引，实现选 FTS5 内置的 trigram 分词，保持零第三方依赖，关键词：trigram",
             "项目检索层最后定了什么方案？", "trigram"),
    Scenario("回复语言", "preference",
             "/memories/preferences/sc06/语言.md", "助手回复使用英文。",
             "/memories/preferences/sc06/语言_v2.md", "助手回复始终使用中文。",
             "助手回复用什么语言？", "中文"),
    Scenario("默认编辑器", "fact",
             "/memories/fact/sc07/编辑器.md", "本机默认编辑器是 vim。",
             "/memories/fact/sc07/编辑器_v2.md", "本机默认编辑器已换成 nano。",
             "本机默认编辑器是什么？", "nano"),
    Scenario("项目代号", "fact",
             "/memories/fact/sc08/代号.md", "项目内部代号是蓝鲸。",
             "/memories/fact/sc08/代号_v2.md", "项目内部代号已改为白鲸。",
             "项目内部代号是什么？", "白鲸"),
    Scenario("变量命名风格", "preference",
             "/memories/preferences/sc09/命名.md", "代码变量名采用驼峰命名。",
             "/memories/preferences/sc09/命名_v2.md", "代码变量名改用下划线命名。",
             "代码变量名用什么命名风格？", "下划线"),
    Scenario("部署方式", "conclusion",
             "/memories/conclusions/sc10/部署.md", "服务部署绑定 127.0.0.1，仅本机可访问。",
             "/memories/conclusions/sc10/部署_v2.md", "服务部署绑定 0.0.0.0，对局域网开放。",
             "服务部署绑定到什么地址？", "0.0.0.0"),
    Scenario("备份方式", "skill",
             "/memories/skills/sc11/备份.md", "数据库备份靠每天手动复制文件。",
             # 返工单 30 加严：内容 >36 字符、答案词在最末
             "/memories/skills/sc11/备份_v2.md",
             "数据库备份流程已从手动复制改为脚本化：每晚运行 scripts/backup.py 生成快照并校验完整性，恢复演练入口命令：backup.py",
             "数据库备份现在怎么做？", "backup.py"),
    Scenario("密钥管理", "fact",
             "/memories/fact/sc12/密钥.md", "密钥保存在配置文件里。",
             "/memories/fact/sc12/密钥_v2.md", "密钥只从环境变量读取，不落盘。",
             "密钥从哪里读取？", "环境变量"),
    Scenario("解释形式偏好", "preference",
             "/memories/preferences/sc13/解释.md", "解释复杂概念时喜欢配示意图。",
             "/memories/preferences/sc13/解释_v2.md", "解释复杂概念时偏好用表格对比。",
             "解释复杂概念时用户偏好什么形式？", "表格对比"),
    Scenario("数据库选型", "fact",
             "/memories/fact/sc14/数据库.md", "项目数据库是 MySQL。",
             "/memories/fact/sc14/数据库_v2.md", "项目数据库已迁移到 SQLite。",
             "项目数据库用的什么？", "SQLite"),
    Scenario("缓存时长", "conclusion",
             "/memories/conclusions/sc15/缓存.md", "缓存有效期定为 60 秒。",
             "/memories/conclusions/sc15/缓存_v2.md", "缓存有效期放宽到 300 秒。",
             "缓存有效期是多少？", "300"),
    Scenario("例会时间", "fact",
             "/memories/fact/sc16/例会.md", "团队例会固定在周一。",
             "/memories/fact/sc16/例会_v2.md", "团队例会改到周三。",
             "团队例会是什么时候？", "周三"),
    Scenario("提交规范", "skill",
             "/memories/skills/sc17/提交.md", "提交信息用英文书写。",
             "/memories/skills/sc17/提交_v2.md", "提交信息改用中文书写。",
             "提交信息用什么语言书写？", "中文"),
    Scenario("请求超时", "fact",
             "/memories/fact/sc18/超时.md", "上游请求超时设为 60 秒。",
             "/memories/fact/sc18/超时_v2.md", "上游请求超时设为 180 秒。",
             "上游请求超时是多少？", "180"),
    Scenario("重试次数", "fact",
             "/memories/fact/sc19/重试.md", "失败后重试 3 次。",
             "/memories/fact/sc19/重试_v2.md", "失败后只重试 2 次。",
             "失败后重试几次？", "2 次"),
    Scenario("误差归因结论", "conclusion",
             "/memories/conclusions/sc20/归因.md", "数值误差的主因判定为网格分辨率不足。",
             "/memories/conclusions/sc20/归因_v2.md", "主因本轮无法分离，嫌疑偏向黏性离散。",
             "数值误差的主因结论是什么？", "黏性离散"),
]

assert len(SCENARIOS) == 20


# ---------------------------------------------------------------- 脚本化后端

class ScriptedRecallModel:
    """脚本化后端：上下文（注入块/直塞文本）里有答案关键词就"用上"。

    行为确定性、可复算。它验证的是注入管道与注入块可读性；
    真实 LLM 的成绩不在本脚本范围内（见文件头说明）。
    """

    def __init__(self, answer_key: str) -> None:
        self.answer_key = answer_key

    def chat(self, context: str, question: str = "") -> str:
        # question 只为与 LiveModel 保持同签名，脚本化后端不需要它
        if self.answer_key in context:
            return f"根据记忆，答案是 {self.answer_key}。"
        return "我没有找到相关记忆，无法回答。"

    def is_correct(self, reply: str) -> bool:
        return self.answer_key in reply



class LiveModel:
    """真实模型后端：注入块当系统提示，场景问题当用户消息。

    🔴 判据仍是「回答里含答案关键词」——所以这个分数测的是**关键词命中率**，
    不是「真模型会不会用记忆」。真模型会换措辞，分数大概率低于脚本化后端
    （同 agent-tool-loop 那次的教训：12 条失败其实是判据太死，不是模型不会用工具）。
    """

    def __init__(self, answer_key: str, base_url: str, api_key: str, model: str) -> None:
        self.answer_key = answer_key
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    def chat(self, context: str, question: str = "") -> str:
        if (context or "").strip():
            sys_prompt = (
                "你可以参考下面这段记忆来回答。\n"
                "如果记忆里没有相关信息，就直接说不知道，不要猜。\n\n" + context
            )
        else:
            sys_prompt = "你没有可用的记忆。如果不知道答案，就直接说不知道，不要猜。"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": question},
            ],
            "max_tokens": 120,
            "temperature": 0,
        }
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.api_key},
            method="POST",
        )
        # 绕开本机代理（Windows 上 urllib 默认读注册表代理）+ 按 AGENTS.md 第 2 节重试
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        retries = int(os.environ.get("AGENT_RETRIES") or 2)
        timeout_s = float(os.environ.get("AGENT_TIMEOUT_MS") or 180000) / 1000.0
        last = None
        for attempt in range(retries + 1):
            try:
                with opener.open(req, timeout=timeout_s) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                return (data["choices"][0]["message"].get("content") or "")
            except urllib.error.HTTPError as exc:
                if (exc.code == 429 or 500 <= exc.code < 600) and attempt < retries:
                    last = exc
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise RuntimeError(f"模型接口 HTTP {exc.code}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < retries:
                    last = exc
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise RuntimeError(f"模型接口失败: {exc}") from exc
        raise RuntimeError(f"重试 {retries} 次仍失败: {last}")

    def is_correct(self, reply: str) -> bool:
        return self.answer_key in reply


def make_model(sc: "Scenario", backend: str):
    """按后端选模型。offline 默认、确定性、可复算；live 需要三个环境变量。"""
    if backend == "live":
        base_url = (os.environ.get("AGENT_BASE_URL") or "").strip()
        api_key = (os.environ.get("AGENT_API_KEY") or "").strip()
        model = (os.environ.get("AGENT_MODEL") or "").strip()
        if not (base_url and api_key and model):
            raise SystemExit(
                "live 后端需要 AGENT_BASE_URL / AGENT_API_KEY / AGENT_MODEL 三个环境变量"
            )
        return LiveModel(sc.answer_key, base_url, api_key, model)
    return ScriptedRecallModel(sc.answer_key)


# ---------------------------------------------------------------- 两段评测

def eval_stage1_store(store: MemoryStore, sc: Scenario) -> tuple[bool, str]:
    """① 存对了吗：写入 A → 写入 A' → 标记取代 → 三项断言。纯逻辑。"""
    store.create(sc.old_path, sc.old_content, sc.kind, "用户说的")
    new_id = store.create(sc.new_path, sc.new_content, sc.kind, "用户说的")
    store.mark_superseded(sc.old_path, new_id)

    got = store.view(sc.new_path)
    if len(got) != 1 or sc.answer_key not in got[0]["content"]:
        return False, f"view 新条未拿到 A'（answer_key={sc.answer_key}）"

    old_rows = store.view(sc.old_path)
    if len(old_rows) != 1 or old_rows[0]["superseded_by"] != new_id:
        return False, "旧条未被正确标记 superseded_by"

    hits = store.search(sc.question)
    ids = [h["id"] for h in hits]
    if new_id not in ids:
        return False, "检索结果里没有新条"
    if old_rows[0]["id"] in ids:
        return False, "被取代的旧条仍出现在检索结果里"
    return True, ""


def eval_stage2_use(store: MemoryStore, sc: Scenario, backend: str = "offline") -> tuple[bool, bool, str]:
    """② 用上了吗。返回 (对照是否对, 检索路径是否对, 失败说明)。

    (a) 对照：把 A' 直接塞进上下文（绕过检索），模拟模型必须答对
        —— 验证模拟后端本身工作正常（它是 (b) 的上界）。
    (b) 正常路径：search → build_context_block → 模拟模型。
    """
    model = make_model(sc, backend)

    direct_reply = model.chat(sc.new_content, sc.question)
    direct_ok = model.is_correct(direct_reply)

    block = store.get_context_block(sc.question)
    recall_reply = model.chat(block, sc.question)
    recall_ok = model.is_correct(recall_reply)

    # 🔴 2026-10-30 补（WorkBuddy）：失败时必须留下「回答原文 + 注入块」。
    #    原实现只写一句"没被送进注入块或块里读不出答案"，导致事后无法判断失败类型
    #    （是措辞不匹配？还是真被别的记忆带偏？还是根本没注入？）→ 只能整轮重跑。
    note = ""
    if not direct_ok:
        note = (f"对照路径失败｜answer_key={sc.answer_key!r}"
                f"｜模型回答={direct_reply[:240]!r}")
    elif not recall_ok:
        note = (f"检索路径失败｜answer_key={sc.answer_key!r}"
                f"｜模型回答={recall_reply[:240]!r}"
                f"｜注入块={block[:240]!r}")
    return direct_ok, recall_ok, note


def run(db_path: str, backend: str = "offline") -> dict:
    store = MemoryStore(db_path)
    s1, direct, recall = [], [], []
    failures: list[str] = []
    for i, sc in enumerate(SCENARIOS, 1):
        ok1, why1 = eval_stage1_store(store, sc)
        s1.append(ok1)
        if not ok1:
            failures.append(f"① 场景{i}「{sc.topic}」: {why1}")
        ok_d, ok_r, why2 = eval_stage2_use(store, sc, backend)
        direct.append(ok_d)
        recall.append(ok_r)
        if not (ok_d and ok_r):
            failures.append(f"② 场景{i}「{sc.topic}」: {why2}")
    return {
        "stage1": sum(s1),
        "direct": sum(direct),
        "recall": sum(recall),
        "total": len(SCENARIOS),
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="记忆层两段评测（分数分开报，绝不合并）")
    parser.add_argument("--db", default="", help="SQLite 路径，默认建临时库")
    parser.add_argument("--backend", default=(os.environ.get("PAI_EVAL_BACKEND") or "offline"),
                        choices=["offline", "live"], help="offline=脚本化后端（默认）；live=真实模型")
    args = parser.parse_args()

    if args.db:
        db = args.db
    else:
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    report = run(db, args.backend)

    print("=" * 64)
    backend_desc = "离线脚本化后端（不代表真实 LLM 成绩）" if args.backend == "offline" \
        else f"真实模型（{os.environ.get('AGENT_MODEL', '?')}）· 判据仍是关键词命中，不是模型能力"
    print(f"记忆层两段评测（20 条场景 · {backend_desc}）")
    print("=" * 64)
    print(f"① 存对了吗   : {report['stage1']}/{report['total']}"
          "   （写入 A → A' → superseded_by 标记 + 检索只见新条）")
    print(f"② 用上了吗   : 检索路径 {report['recall']}/{report['total']}"
          "   （search → 注入块 → 模拟模型）")
    print(f"   对照直塞  : {report['direct']}/{report['total']}"
          "   （正确答案绕过检索直接进上下文，是检索路径的上界）")
    print("-" * 64)
    if report["failures"]:
        print("失败明细：")
        for line in report["failures"]:
            print(f"  - {line}")
    else:
        print("三个分数全部满格（注意口径，见上方后端说明）")
    print("🔴 以上分数分开报，禁止合并成单一总分。")
    print("=" * 64)

    all_full = (
        report["stage1"] == report["total"]
        and report["direct"] == report["total"]
        and report["recall"] == report["total"]
    )
    return 0 if all_full else 1


if __name__ == "__main__":
    raise SystemExit(main())
