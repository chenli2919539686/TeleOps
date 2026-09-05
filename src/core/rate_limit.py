# -*- coding: utf-8 -*-
"""进程内滑动窗口限流（零第三方依赖）。

设计要点：
- 单进程适用：按客户端 IP（或其它 key）统计固定窗口（默认 60s）内的请求次数，
  超过阈值返回 429 + Retry-After。TeleOps 当前为单实例部署，进程内窗口足够；
  若未来多副本水平扩容，需换成 Redis 等共享计数器（见 README Phase 3 演进路径）。
- 滑动窗口用 deque 存命中时间戳，惰性清理，空间 ~O(limit)，时间 O(窗口内命中数)。
- 分三档限额：读 / 写 / 登录注册（登录档最严，防口令爆破）。
- 提供 configure_rate_limit() 支持运行时调整（测试用），无需重启。

用法：
    from src.core import rate_limit
    ok, retry = rate_limit.allow("r:127.0.0.1", 300)
"""
import os
import threading
import time
from collections import defaultdict, deque

# 默认配置（可用环境变量覆盖；中间件在 import 时读取一次）
WINDOW = 60.0                       # 窗口秒数
ENABLED = os.environ.get("TELEOPS_RATE_LIMIT", "on").strip().lower() in ("1", "on", "true", "yes")
# v0.8.12：调严到推荐档。登录注册最严防爆破，写接口 60/min（北向告警源正常频率），
# 读接口 120/min（前端轮询友好）。如需调整用 TELEOPS_RATE_LIMIT_* 环境变量覆盖。
READ_LIMIT = int(os.environ.get("TELEOPS_RATE_LIMIT_READ", "120"))    # 读接口 /min/IP
WRITE_LIMIT = int(os.environ.get("TELEOPS_RATE_LIMIT_WRITE", "60"))   # 写接口 /min/IP
LOGIN_LIMIT = int(os.environ.get("TELEOPS_RATE_LIMIT_LOGIN", "5"))    # 登录注册 /min/IP

_hits: "defaultdict[str, deque]" = defaultdict(deque)
_lock = threading.Lock()


def allow(key: str, limit: int, window: float = WINDOW):
    """key 在窗口内未超限则记录并放行；超限返回 (False, retry_after_seconds)。"""
    now = time.time()
    with _lock:
        dq = _hits[key]
        cutoff = now - window
        # 惰性弹出窗口外的旧时间戳
        while dq and dq[0] <= cutoff:
            dq.popleft()
        if len(dq) >= limit:
            if not dq:
                # 限额为 0（或队列被清空）的防御分支：窗口结束后才放行
                return False, int(window)
            # 最早的命中在 dq[0]+window 时刻滑出窗口，那之后才能放行
            retry_after = max(1, int(dq[0] + window - now) + 1)
            return False, retry_after
        dq.append(now)
        return True, 0


def reset(key: str = None):
    """清空窗口（测试用）。key 为空则全清。"""
    with _lock:
        if key is None:
            _hits.clear()
        else:
            _hits.pop(key, None)


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
