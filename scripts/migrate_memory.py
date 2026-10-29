"""记忆层迁移脚本（CLI 壳，可重复执行）。

实际建表逻辑在 personal_ai/memory.py 的 ensure_tables()，
这里只做命令行入口，保证"只新增两张表，不动旧表"。

用法：
    python scripts/migrate_memory.py            # 默认库（PAI_DB_PATH 或 data/personal_ai.db）
    python scripts/migrate_memory.py --db 路径
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from personal_ai.config import Config  # noqa: E402
from personal_ai.memory import ensure_tables  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="记忆层表结构迁移（可重复执行）")
    parser.add_argument(
        "--db", default="", help="SQLite 路径，默认取 PAI_DB_PATH 或 data/personal_ai.db"
    )
    args = parser.parse_args()

    db = args.db or Config.from_env().db_path
    report = ensure_tables(db)
    print("迁移完成（幂等，可重复执行）：")
    print(f"  库          : {report['db']}")
    print(f"  memories 行 : {report['memories_rows']}")
    print(f"  列          : {', '.join(report['columns'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
