"""HTTP 服务层。

刻意只使用标准库 http.server：零第三方依赖，任何一台装了 Python 的机器都能直接跑。
Flask 版本可以在需要时再叠加，不影响现有路由与业务逻辑。
"""

from .app import run_server

__all__ = ["run_server"]
