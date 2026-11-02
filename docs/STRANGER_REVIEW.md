# 陌生人复验记录（35 号工单 · P1 personal-ai）

> 复验日期：2026-11-02 · 复验人：技术线（Trae）
> 方法：空目录 `git clone`，严格照 README 三步走，不加任何额外步骤。

## 结果

**卡在 clone 后的第一步，修 1 处后走通。**

## 过程

| 步骤 | 结果 |
|---|---|
| `git clone` 到空目录 | ✅ |
| 照 README 跑 `python demo.py` | ❌ **卡住**：HEAD（74af944）里没有 `demo.py`，也没有 `personal_ai/memory.py`——29~32 号的改动全在工作区，**尚未提交** |
| 同步工作区文件（模拟设计者 commit 后的仓）：demo.py、README.md、docs/、personal_ai/{memory.py, core/chat.py, providers/base.py, providers/openai_compat.py} | — |
| 再照 README 跑 `python demo.py` | ✅ **一次走通**：一次问答（mock 诚实标注）→ 一条记忆写入 → 记忆检索并注入的全过程，零 key、零额外步骤、临时库不残留 |

## 结论与待办

1. **README 的三步本身没有问题**（同步文件后一次走通）。
2. **真正的缺口是 git HEAD 落后**：29~32 号的个人仓改动（memory.py、chat.py、providers、demo.py、docs/、tests/）全部未提交。
   设计者 commit 之后，陌生人 clone 即可直接 `python demo.py`，无需任何同步。
3. 复验用的同步文件清单见上表——这就是设计者 commit 时应包含的文件清单。

## 复验环境

- Windows 11，Python 3.11，空目录 clone 自本地仓（E:\DSHPersonalAI）
- 未配置任何 PAI_* 环境变量，未联网
