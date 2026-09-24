# -*- coding: utf-8 -*-
"""进程内滑动窗口限流（零第三方依赖）。

设计要点：
- 分档限额：读 / 写 / 登录注册（登录档最严，防口令爆破）。
- 滑动窗口：**计数存储已抽到 src.core.state_store**，本模块只负责"该按哪档限额、
  用什么 key 计"的策略判定。
  - 默认``TELEOPS_STATE_STORE=local``：进程内 deque，单实例部署零外部依赖；
  - 设``TELEOPS_STATE_STORE=redis``：同一套窗口语义落到 Redis ZSET（Lua 原子执行），
    多 worker / 多副本共享配额 —— 多副本水平扩容时不再出现"N 副本 = N 倍配额"。
- 提供 configure_rate_limit() 支持运行时调整（测试用），无需重启。

用法：
    from src.core import rate_limit
    ok, retry = rate_limit.allow("r:127.0.0.1", 300)
"""
import os

# 默认配置（可用环境变量覆盖；中间件在 import 时读取一次）
WINDOW = 60.0                       # 窗口秒数
ENABLED = os.environ.get("TELEOPS_RATE_LIMIT", "on").strip().lower() in ("1", "on", "true", "yes")
# v0.8.12：调严到推荐档。登录注册最严防爆破，写接口 60/min（北向告警源正常频率），
# 读接口 120/min（前端轮询友好）。如需调整用 TELEOPS_RATE_LIMIT_* 环境变量覆盖。
READ_LIMIT = int(os.environ.get("TELEOPS_RATE_LIMIT_READ", "120"))    # 读接口 /min/IP
WRITE_LIMIT = int(os.environ.get("TELEOPS_RATE_LIMIT_WRITE", "60"))   # 写接口 /min/IP
LOGIN_LIMIT = int(os.environ.get("TELEOPS_RATE_LIMIT_LOGIN", "5"))    # 登录注册 /min/IP

# 滑动窗口的"存储层"已被抽到 src.core.state_store（D3）：
# - 默认 LocalStateStore（进程内 deque），行为与改造前逐字节等价，单实例零变化
# - 设 TELEOPS_STATE_STORE=redis 后计数落 Redis ZSET，多副本共享同一份配额
#   （之前多 worker 时各算各的，N 副本 = N 倍配额，限流形同虚设）
from src.core.state_store import get_state_store


def allow(key: str, limit: int, window: float = WINDOW):
    """key 在窗口内未超限则记录并放行；超限返回 (False, retry_after_seconds)。"""
    return get_state_store().hit(key, limit, window)


def reset(key: str = None):
    """清空窗口（测试用）。key 为空则全清。"""
    get_state_store().reset(key)


def configure_rate_limit(enabled: bool = None, read: int = None,
                         write: int = None, login: int = None):
    """运行时调整限流配置（不重启生效）。传 None 表示保持原值。"""
    global ENABLED, READ_LIMIT, WRITE_LIMIT, LOGIN_LIMIT
    if enabled is not None:
        ENABLED = bool(enabled)
    if read is not None:
        READ_LIMIT = int(read)
    if write is not None:
        WRITE_LIMIT = int(write)
    if login is not None:
        LOGIN_LIMIT = int(login)
    reset()
