"""TeleOps API 路由子包。

按业务域拆分出来的 APIRouter 集合。每个子模块只定义自己的 router，
由 server.py 在应用装配阶段统一 include_router 挂载（避免 server.py 巨型单体）。

路由处理函数内部对全局单例/helper 的引用统一走 `from src.api import server as s`，
server.py 在 import 本包之前已完成全部全局单例与 helper 的初始化，故无循环依赖。
"""

from .system import router as system_router
from .core import router as core_router
from .traces import router as traces_router
from .llm import router as llm_router
from .audit import router as audit_router

__all__ = [
    "system_router",
    "core_router",
    "traces_router",
    "llm_router",
    "audit_router",
]
