"""存储层：History 属于用户，不属于任何模型。

SQLite 的意义：零依赖、单文件、可以整块拷走。
换机器 = 拷一个 .db 文件，历史就跟着人走。
"""

from .sqlite import (
    add_message,
    create_session,
    create_task,
    delete_session,
    delete_task,
    init_db,
    list_messages,
    list_sessions,
    list_tasks,
    update_task,
)

__all__ = [
    "init_db",
    "create_session",
    "add_message",
    "list_messages",
    "list_sessions",
    "delete_session",
    "create_task",
    "list_tasks",
    "update_task",
    "delete_task",
]
