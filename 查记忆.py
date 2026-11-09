#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""查记忆 — 历史记忆本地查询工具（41 号工单 · 零依赖，仅 Python 标准库）

用法：
  python 查记忆.py "待遇 门槛"              # 关键词搜索（可多个，空格分隔）
  python 查记忆.py --question "Manus 为什么没过"   # 整句问题（自动切词）
  python 查记忆.py --rebuild                # 校准 LOOKUP.md 的行号引用

扫描范围：工作日志（2026-*.md）+ topics/ 五文件 + LOOKUP.md + index/ + MEMORY.md
输出：文件相对路径 : 行号 : 那一句原文（按相关度前 10）

记忆目录：环境变量 PAI_MEMORY_DIR 优先；没设就用本脚本所在目录
（即 .workbuddy/memory）。目录不存在会明确报错，不静默返回空。

铁律：本脚本对日志/topics 只读不改；--rebuild 只更新 LOOKUP.md 里的行号指针，
     日志一个字不动。搜不到就明确输出「没有命中」，不猜不编。
"""
import argparse
import os
import re
import sys
import time
from pathlib import Path


def get_memory_dir() -> Path:
    """记忆目录：环境变量 PAI_MEMORY_DIR 优先；没设就用本脚本所在目录。"""
    env = os.environ.get("PAI_MEMORY_DIR", "").strip()
    return Path(env).expanduser() if env else Path(__file__).resolve().parent


def resolve_memory_dir(base: str | Path | None = None) -> Path:
    """校验记忆目录存在并返回绝对路径；不存在抛 FileNotFoundError（不静默返回空）。"""
    base = Path(base).expanduser() if base else get_memory_dir()
    if not base.is_dir():
        raise FileNotFoundError(f"记忆目录不存在：{base}")
    return base.resolve()

# ---------- 扫描范围 ----------

def collect(
    base: str | Path | None = None,
) -> tuple[list[tuple[str, Path]], dict[str, int]]:
    """返回 ([(显示路径, Path)], 分类计数)。显示路径相对于记忆目录。"""
    base = resolve_memory_dir(base)
    files, counts = [], {}
    def add(p, cat):
        if p.is_file():
            files.append((p.relative_to(base).as_posix(), p))
            counts[cat] = counts.get(cat, 0) + 1
    for p in sorted(base.glob("2026-*.md")):
        add(p, "日志")
    for sub, cat in (("topics", "topics"), ("index", "index")):
        d = base / sub
        if d.is_dir():
            for p in sorted(d.glob("*.md")):
                add(p, cat)
    add(base / "LOOKUP.md", "LOOKUP")
    add(base / "MEMORY.md", "MEMORY")
    return files, counts

# ---------- 切词与打分 ----------

_STOP1 = set("的了呢吗吧啊呀哦么是在和与就也都还把被将已又再于对从让给你我他她它这那有个不没为到说出给看做用要能会可想知上下里外前后很太更最只才一并之以及或但而因此由于虽然然而如果因为所以当请问告诉一下怎样如何")
_BIGRAM_STOP = {
    "什么", "怎么", "哪些", "为何", "为什么", "如何", "多少", "哪里", "哪天",
    "还是", "这个", "那个", "当时", "之后", "以后", "后来", "每次", "一下",
    "可以", "应该", "需要", "现在", "已经", "没有", "我是", "我的", "他的",
    "定的", "怎么定", "是不是", "有没有", "咱们", "你们", "他们", "还是说",
}

def make_terms(q: str) -> list[tuple[str, float]]:
    """把查询串切成 (词, 权重) 列表。ASCII 词原样；中文整段（≤6 字）+ 二元组。"""
    terms = {}
    def put(t, w):
        if t in terms:
            terms[t] = max(terms[t], w)
        else:
            terms[t] = w
    for w in re.findall(r"[A-Za-z0-9_][A-Za-z0-9_\.\-/]*", q):
        if len(w) >= 2:
            put(w.lower(), 2.0 + min(len(w), 10) * 0.3)
    for seg in re.findall(r"[\u4e00-\u9fff]{2,}", q):
        if 2 <= len(seg) <= 6:
            put(seg, 6.0 + len(seg) * 2.0)          # 整段命中=权重高
        for i in range(len(seg) - 1):
            g = seg[i:i + 2]
            if g[0] in _STOP1 or g[1] in _STOP1 or g in _BIGRAM_STOP:
                continue
            put(g, 2.0)
    return list(terms.items())

def score_line(line: str, terms: list[tuple[str, float]]) -> tuple[float, int]:
    """返回 (得分, 命中的不同词数)。多词齐中的行在 search() 里排前面。"""
    ll = line.lower()
    s = 0.0
    n = 0
    for t, w in terms:
        c = ll.count(t)
        if c:
            s += c * w
            n += 1
    return s, n

def search(
    terms: list[tuple[str, float]],
    files: list[tuple[str, Path]],
) -> list[tuple[float, str, int, str]]:
    """检索排序：返回 [(相关度, 显示路径, 行号, 原文行)]，多词齐中优先、分数次之。"""
    hits = []
    for rel, p in files:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            sc, nd = score_line(line, terms)
            if sc > 0:
                hits.append((nd, sc, rel, i, line))
    # 主排序键 = 命中的不同词数（多词齐中优先），次键 = 分数
    hits.sort(key=lambda x: (-x[0], -x[1], x[2], x[3]))
    return [(sc, rel, no, line) for _nd, sc, rel, no, line in hits]

# ---------- --rebuild：校准 LOOKUP.md 行号 ----------

REF = re.compile(r"(?P<path>[^\s『；]+\.md)[ \t]+L(?P<no>\d+)『(?P<snip>[^』]+)』")

def resolve(path_str: str, base: str | Path | None = None) -> Path | None:
    """把 LOOKUP 里的相对路径解析成真实文件；找不到返回 None。"""
    base = resolve_memory_dir(base)
    p = Path(path_str.replace("\\", "/"))
    if p.is_absolute():
        return p if p.exists() else None
    for cand_base in (base, base.parent.parent):
        cand = cand_base / p
        if cand.exists():
            return cand
    return None

def rebuild(base: str | Path | None = None) -> None:
    base = resolve_memory_dir(base)
    lookup = base / "LOOKUP.md"
    text = lookup.read_text(encoding="utf-8")
    stat = {"ok": 0, "fixed": 0, "miss": 0}

    def fix_line(line):
        def rep(m):
            f = resolve(m.group("path"), base)
            if f is None:
                stat["miss"] += 1
                print(f"  ⚠ 文件不存在，保留原值：{m.group('path')}")
                return m.group(0)
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                stat["miss"] += 1
                print(f"  ⚠ 读不了，保留原值：{m.group('path')}")
                return m.group(0)
            snip = m.group("snip")
            for i, ln in enumerate(content.splitlines(), 1):
                if snip in ln:
                    old = int(m.group("no"))
                    if i == old:
                        stat["ok"] += 1
                    else:
                        stat["fixed"] += 1
                        print(f"  校准 {m.group('path')} L{old} → L{i}『{snip[:20]}…』")
                    return f"{m.group('path')} L{i}『{snip}』"
            stat["miss"] += 1
            print(f"  ⚠ 锚文未找到（保留 L{m.group('no')} 不动）："
                  f"{m.group('path')}『{snip[:20]}…』")
            return m.group(0)
        return REF.sub(rep, line)

    new_text = "".join(fix_line(ln) for ln in text.splitlines(keepends=True))
    if new_text != text:
        lookup.write_text(new_text, encoding="utf-8")
    total = stat["ok"] + stat["fixed"] + stat["miss"]
    print(f"--rebuild 完成：核对 {total} 处引用｜已对 {stat['ok']}｜校准 {stat['fixed']}"
          f"｜锚文未找到 {stat['miss']}（保留原值）")
    print("日志与 topics 一字未动，只更新了 LOOKUP.md 的行号指针。")

# ---------- 主入口 ----------

def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(
        description="查记忆：在 32 天日志 + topics + LOOKUP + index 中查关键词")
    ap.add_argument("keywords", nargs="*", help="关键词（可多个，空格分隔）")
    ap.add_argument("--question", "-q", help="整句问题（自动切词）")
    ap.add_argument("--rebuild", action="store_true",
                    help="校准 LOOKUP.md 的行号引用（日志不动）")
    ap.add_argument("--top", type=int, default=10, help="输出条数（默认 10）")
    args = ap.parse_args()

    t0 = time.perf_counter()
    try:
        if args.rebuild:
            rebuild()
            return
    except FileNotFoundError as e:
        print(str(e))
        sys.exit(2)
    q = args.question if args.question else " ".join(args.keywords)
    if not q.strip():
        ap.print_usage()
        print("示例：python 查记忆.py \"待遇 门槛\"  或  python 查记忆.py --question \"Manus 为什么没过\"")
        sys.exit(2)

    try:
        files, counts = collect()
    except FileNotFoundError as e:
        print(str(e))
        sys.exit(2)
    terms = make_terms(q)
    hits = search(terms, files)
    elapsed = time.perf_counter() - t0

    summary = " + ".join(f"{cat}{n}" for cat, n in sorted(counts.items()))
    if not hits:
        print(f"没有命中：关键词「{q}」在 {len(files)} 个文件（{summary}）中都没有出现。不猜不编。")
        sys.exit(1)

    print(f"扫描 {len(files)} 个文件（{summary}）· 用时 {elapsed:.2f}s")
    print(f"命中 {len(hits)} 行，相关度前 {min(args.top, len(hits))}：\n")
    for rank, (sc, rel, no, line) in enumerate(hits[:args.top], 1):
        print(f"{rank:>2}. [{sc:.0f}] {rel}:{no}")
        print(f"    {line}")

if __name__ == "__main__":
    main()
