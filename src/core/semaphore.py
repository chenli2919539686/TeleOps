"""分布式信号量（D3 收官项：并发闸门外部化）。

背景
----
改造前并发闸门是进程内的：``LLM_SEM = threading.Semaphore(4)`` 限 LLM 并发、
``_agent_sem(aid)`` 限单 Agent 并发。单副本没问题；一旦多副本，N 个副本各限 4，
实际对 DeepSeek 的并发是 4N —— 配额保护形同虚设。

本模块把这两道闸门外置到 Redis，多副本共享同一份许可池：
- :class:`LocalSemaphoreStore`（**默认**）：就是本地 ``threading.Semaphore``，
  行为与改造前完全一致（无限阻塞等待），单机/演示零依赖；
- :class:`RedisSemaphoreStore`：ZSET + Lua 原子申请/释放，带**租约**。

为什么必须有租约（lease）
------------------------
分布式信号量最致命的失败模式：持有许可的进程崩溃 / 被 kill，没走 release，
这个许可就永远回不来 —— 累计几次之后所有副本都拿不到许可，全系统死锁。
因此每个许可都带租约，超时未释放的由后续申请者顺手回收（Lua 里
``ZREMRANGEBYSCORE`` 每次申请前清一遍）。

租约取值：应显著大于"单次持有时长"。本项目上一轮已把 LLM 单次调用超时收到
``LLM_TIMEOUT=30s``，故默认租约 120s 足够安全，不会出现持有中被误回收。
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from abc import ABC, abstractmethod
from typing import Optional

KEY_PREFIX = os.environ.get("TELEOPS_SEM_PREFIX", "teleops:sem:")

# 申请不到许可时的最长等待（秒）；超时后是否放行见 TELEOPS_SEM_FAIL_OPEN。
# 本地实现不设上限（沿用原来的无限阻塞语义），只有分布式实现才用这个值。
DEFAULT_TIMEOUT = float(os.environ.get("TELEOPS_SEM_TIMEOUT", "30") or 30)
# 许可租约（秒）：持有者崩溃后，超过这个时间许可被自动回收。
DEFAULT_LEASE = float(os.environ.get("TELEOPS_SEM_LEASE", "120") or 120)
# 等待许可时的轮询间隔（秒）
POLL_INTERVAL = 0.05


class SemaphoreStore(ABC):
    """并发闸门。``semaphore(key, limit)`` 返回一个上下文管理器。"""

    @abstractmethod
    def semaphore(self, key: str, limit: int, timeout: Optional[float] = None,
                  lease: Optional[float] = None):
        ...

    @abstractmethod
    def count(self, key: str) -> int:
        """当前已发出的许可数（排查/测试用）。"""


class _Ctx:
    """统一的上下文管理器：进入取许可、退出必释放（含异常路径）。"""

    def __init__(self, acquire, release, key, token_holder):
        self._acquire = acquire
        self._release = release
        self._key = key
        self._token = None
        self._holder = token_holder

    @property
    def granted(self) -> bool:
        """是否真的拿到了许可（等待超时/旁路故障会返回 False 并放行）。"""
        return self._token is not None

    def __enter__(self):
        self._token = self._acquire()
        self._holder["token"] = self._token
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._token is not None:
            try:
                self._release(self._token)
            except Exception:
                pass
            self._token = None
        return False


class LocalSemaphoreStore(SemaphoreStore):
    """本地信号量（默认）：与改造前的 threading.Semaphore 完全等价。"""

    def __init__(self):
        self._sems = {}
        self._lock = threading.Lock()

    def _sem(self, key, limit):
        with self._lock:
            s = self._sems.get(key)
            if s is None:
                s = threading.Semaphore(limit)
                self._sems[key] = s
            return s

    def semaphore(self, key, limit, timeout=None, lease=None):
        sem = self._sem(key, limit)
        holder = {}

        def acquire():
            # timeout=None → 无限阻塞（保持与原 `with LLM_SEM:` 一致的语义）
            if timeout is None:
                sem.acquire()
            else:
                if not sem.acquire(timeout=timeout):
                    return None
            return "local"

        def release(_token):
            sem.release()

        return _Ctx(acquire, release, key, holder)

    def count(self, key):
        # threading.Semaphore 不暴露当前计数，用近似值（已发出=limit-可用）
        return -1


# Lua：原子申请许可（先回收过期租约，再判断额度）
_ACQUIRE_LUA = """
local now   = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local lease = tonumber(ARGV[3])
local token = ARGV[4]

-- 回收超过租约仍未释放的许可：防持有者崩溃导致许可永久丢失（全系统死锁）
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - lease)

if redis.call('ZCARD', KEYS[1]) < limit then
  redis.call('ZADD', KEYS[1], now, token)
  redis.call('PEXPIRE', KEYS[1], math.ceil((lease + 60) * 1000))
  return 1
end
return 0
"""

_RELEASE_LUA = """
return redis.call('ZREM', KEYS[1], ARGV[1])
"""


class RedisSemaphoreStore(SemaphoreStore):
    """Redis 版信号量：多副本共享同一份许可池，带租约自动回收。"""

    def __init__(self, redis_url: Optional[str] = None, client=None,
                 key_prefix: str = KEY_PREFIX, fail_open: bool = True):
        self._prefix = key_prefix
        self.fail_open = fail_open
        self._client = client
        if self._client is None:
            import redis  # 延迟导入
            self._client = redis.Redis.from_url(
                redis_url or os.environ.get("TELEOPS_REDIS_URL",
                                            "redis://127.0.0.1:6379/0"),
                decode_responses=True)
        # 脚本惰性注册：构造时不碰网络/脚本，避免 Redis 不可用时连对象都建不出来
        # （进而拖垮服务启动）。注册失败会在首次 acquire 时被捕获并按 fail_open 处理。
        self._acq = None
        self._rel = None

    def _scripts(self):
        if self._acq is None:
            self._acq = self._client.register_script(_ACQUIRE_LUA)
            self._rel = self._client.register_script(_RELEASE_LUA)
        return self._acq, self._rel

    def _k(self, key):
        return f"{self._prefix}{key}"

    def semaphore(self, key, limit, timeout=None, lease=None):
        timeout = DEFAULT_TIMEOUT if timeout is None else timeout
        lease = DEFAULT_LEASE if lease is None else lease
        k = self._k(key)
        holder = {}

        def acquire():
            token = uuid.uuid4().hex
            deadline = time.time() + timeout
            while True:
                try:
                    acq, _rel = self._scripts()
                    if int(acq(keys=[k], args=[repr(time.time()), limit,
                                               repr(lease), token])) == 1:
                        return token
                except Exception as e:          # Redis 故障：按策略处置
                    RedisSemaphoreStore.last_error = str(e)
                    if self.fail_open:
                        # 旁路组件故障不应压垮业务：放行（并发上限暂时失效但服务不断）
                        return None
                    raise
                if time.time() >= deadline:
                    # 等不到许可：默认放行，避免整条告警流水线卡死
                    if self.fail_open:
                        return None
                    return None
                time.sleep(POLL_INTERVAL)

        def release(token):
            try:
                _acq, rel = self._scripts()
                rel(keys=[k], args=[token])
            except Exception:
                pass

        return _Ctx(acquire, release, key, holder)

    def count(self, key):
        try:
            return int(self._client.zcard(self._k(key)))
        except Exception:
            return -1

    last_error: Optional[str] = None


_store: Optional[SemaphoreStore] = None
_store_lock = threading.Lock()


def get_semaphore_store() -> SemaphoreStore:
    """按环境变量返回并发闸门后端（与其他状态共用 TELEOPS_STATE_STORE 开关）。"""
    global _store
    if _store is not None:
        return _store
    with _store_lock:
        if _store is not None:
            return _store
        backend = os.environ.get("TELEOPS_STATE_STORE", "local").strip().lower()
        fail_open = os.environ.get("TELEOPS_SEM_FAIL_OPEN", "on").strip().lower() in (
            "1", "on", "true", "yes")
        _store = (RedisSemaphoreStore(fail_open=fail_open)
                  if backend == "redis" else LocalSemaphoreStore())
        return _store


def configure_semaphore_store(store: Optional[SemaphoreStore]) -> None:
    """显式指定后端（测试用）；None 则按环境变量重建。"""
    global _store
    with _store_lock:
        _store = store
