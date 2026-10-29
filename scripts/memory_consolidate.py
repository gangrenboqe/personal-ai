"""记忆层后台巩固脚本（施工单 29 · 3.6，第一版手动跑，不接定时任务）。

做两件事，都只出清单，不自动裁决：
1. 可能过期：verified_at 为空且 written_at 距今超过 N 天（默认 30）的存活条目。
   --mark 时往 expires_hint 追加标记（幂等，已有标记不重复）。
2. 疑似冲突：同 kind + 同目录下存活且未被取代的条目多于一条——
   可能是新旧版本并存没收口。只列清单，由人判断。

不删除、不改 content、不碰 messages 表。

用法：
    python scripts/memory_consolidate.py                # 默认库，30 天
    python scripts/memory_consolidate.py --days 14
    python scripts/memory_consolidate.py --mark         # 给过期条目打标记
    python scripts/memory_consolidate.py --db 路径
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from personal_ai.config import Config  # noqa: E402
from personal_ai.memory import MemoryStore, ensure_tables  # noqa: E402

STALE_MARK = "[可能过期"


def _parse_dt(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def find_stale(store: MemoryStore, days: int) -> list[dict]:
    """verified_at 为空且超过 N 天未写入的存活条目。"""
    cutoff = datetime.now() - timedelta(days=days)
    out = []
    for row in store.never_verified():
        written = _parse_dt(row.get("written_at") or "")
        if written is not None and written < cutoff:
            out.append(row)
    return out


def find_conflicts(store: MemoryStore) -> list[tuple[str, str, list[dict]]]:
    """同 kind + 同目录下存活且未被取代的条目多于一条 → 疑似新旧并存。

    这是启发式：只负责把可疑组列出来，是否真冲突由人判断（不自动裁决）。
    返回 [(kind, 目录, 组内条目)]。
    """
    groups: dict[tuple[str, str], list[dict]] = {}
    with_store = store.view("/memories/")
    for row in with_store:
        if row.get("superseded_by") is not None:
            continue
        parent = row["path"].rsplit("/", 1)[0]
        groups.setdefault((row["kind"], parent), []).append(row)
    return sorted(
        (kind, parent, rows)
        for (kind, parent), rows in groups.items()
        if len(rows) > 1
    )


def mark_stale(store: MemoryStore, stale: list[dict], today: str) -> int:
    """给过期条目的 expires_hint 追加标记（幂等）。返回实际打标数。"""
    marked = 0
    for row in stale:
        hint = row.get("expires_hint") or ""
        if STALE_MARK in hint:
            continue  # 已打过，不重复
        new_hint = f"{hint}｜{STALE_MARK}：{today} 扫描]" if hint else f"{STALE_MARK}：{today} 扫描]"
        with sqlite3.connect(store.db_path) as conn:
            conn.execute(
                "UPDATE memories SET expires_hint = ? WHERE id = ?",
                (new_hint, row["id"]),
            )
        marked += 1
    return marked


def main() -> int:
    parser = argparse.ArgumentParser(description="记忆层巩固（只列清单，不自动裁决）")
    parser.add_argument("--db", default="", help="SQLite 路径，默认取 PAI_DB_PATH 或 data/personal_ai.db")
    parser.add_argument("--days", type=int, default=30, help="未核过且超过 N 天视为可能过期（默认 30）")
    parser.add_argument("--mark", action="store_true", help="给可能过期的条目在 expires_hint 上打标记")
    args = parser.parse_args()

    db = args.db or Config.from_env().db_path
    ensure_tables(db)
    store = MemoryStore(db)
    today = datetime.now().strftime("%Y-%m-%d")

    stale = find_stale(store, args.days)
    conflicts = find_conflicts(store)

    print("=" * 64)
    print(f"记忆层巩固扫描 · {today}（只列清单，不自动裁决）")
    print("=" * 64)

    print(f"\n【可能过期】verified_at 为空且超过 {args.days} 天：{len(stale)} 条")
    for row in stale:
        print(f"  - {row['path']}  写入: {row['written_at']}  来源: {row['source']}")

    print(f"\n【疑似冲突】同 kind + 同目录多条并存（未被 superseded）：{len(conflicts)} 组")
    for kind, parent, rows in conflicts:
        print(f"  - {parent}（kind: {kind}）")
        for row in rows:
            print(f"      {row['path']}  写入: {row['written_at']}")

    if args.mark and stale:
        marked = mark_stale(store, stale, today)
        print(f"\n已给 {marked} 条在 expires_hint 上追加标记。")

    print("\n说明：本脚本不删除、不改内容、不判定谁对谁错；处理由人决定。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
