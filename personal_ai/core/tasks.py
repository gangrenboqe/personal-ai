"""未来区：任务（Task）。

「未来」是 Personal AI 永久时间骨架的第三段。
它不是一个待办清单插件，而是让「还没发生的事」在这个产品里真正占一个位置。

状态只有三种：
    todo  待开始
    doing 进行中
    done  已完成
"""

from __future__ import annotations

from ..config import Config
from ..storage import create_task, delete_task, init_db, list_tasks, update_task


class TaskService:
    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config.from_env()
        init_db(self.config.db_path)

    def list(self, status: str | None = None) -> list[dict]:
        return list_tasks(self.config.db_path, status=status)

    def add(self, title: str, note: str = "", status: str = "todo", due_at: float | None = None) -> str:
        title = (title or "").strip()
        if not title:
            raise ValueError("任务标题不能为空")
        return create_task(self.config.db_path, title=title, note=note, status=status, due_at=due_at)

    def update(self, task_id: str, **fields) -> bool:
        return update_task(self.config.db_path, task_id, **fields)

    def delete(self, task_id: str) -> bool:
        return delete_task(self.config.db_path, task_id)

    def summary(self) -> dict:
        """给 UI 顶部用的计数。"""
        allt = list_tasks(self.config.db_path)
        return {
            "total": len(allt),
            "todo": sum(1 for t in allt if t["status"] == "todo"),
            "doing": sum(1 for t in allt if t["status"] == "doing"),
            "done": sum(1 for t in allt if t["status"] == "done"),
        }
