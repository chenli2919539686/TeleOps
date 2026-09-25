"""共享状态存储抽象（D3 第一步：让"多副本无状态"成为可选项）。

背景
----
改造前，几类状态被藏在单个 Python 进程的内存里（``rate_limit._hits`` 的 deque 字典、
``server._jobs`` / ``_streams`` / 信号量等）。单 uvicorn worker 时没问题，
一旦多 worker / 多副本水平扩容，这些计数就各算各的 —— 限流形同虚设
（N 个副本 = N 倍配额），任务状态也互相看不见。

本模块把"可被共享的计数类状态"抽成一层极薄的存储接口：

- :class:`LocalStateStore`（**默认**）：就是改造前的进程内 deque 实现，
  行为逐字节等价 → 单实例部署零变化，demo 不需要装 Redis。
- :class:`RedisStateStore`：同样的语义落在 Redis ZSET 上（Lua 保证原子），
  多副本共享同一份配额 → 直接支撑无状态水平扩容。

切换方式（无需改代码）::

    TELEOPS_STATE_STORE=redis          # local（默认） | redis
    TELEOPS_REDIS_URL=redis://127.0.0.1:6379/0

当前接入的消费者：限流滑动窗口（:mod:`src.core.rate_limit`）。
后续可陆续接入任务状态、Agent busy 状态灯等"需要跨副本可见"的状态。

失败策略
--------
Redis 不可用时默认**快速失败开放**（放行并记录告警），避免一个旁路组件
把整个服务打挂；可用 ``TELEOPS_STATE_STORE_FAIL_OPEN=off`` 改为失败拒绝。
"""
from __future__ import annotations

import os
import json
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from typing import Optional, Tuple

# 滑块窗口计数默认 Key 前缀（便于 Redis 侧清理与排查）
KEY_PREFIX = os.environ.get("TELEOPS_STATE_PREFIX", "teleops:rl:")


class StateStore(ABC):
    """计数类共享状态的存储接口。

    语义统一为「滑动窗口命中」：记录一次命中，返回是否放行与建议重试秒数。
    """

    @abstractmethod
    def hit(self, key: str, limit: int, window: float) -> Tuple[bool, int]:
        """记录一次命中，返回 ``(是否放行, retry_after 秒)``。"""

    @abstractmethod
    def reset(self, key: Optional[str] = None) -> None:
        """清空窗口计数；``key`` 为空则清空本服务自己的全部 key。"""


class LocalStateStore(StateStore):
    """进程内滑动窗口（零依赖，默认实现）。

    与改造前 ``rate_limit._hits`` 的实现完全等价：deque 存命中时间戳，
    惰性弹出窗口外数据，单进程内由线程锁保护。
    """

    def __init__(self):
        self._hits: "defaultdict[str, deque]" = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window: float) -> Tuple[bool, int]:
        now = time.time()
        with self._lock:
            dq = self._hits[key]
            cutoff = now - window
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

    def reset(self, key: Optional[str] = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)


# Lua：ZSET 版滑动窗口，整段在 Redis 内原子执行（清旧 / 计数 / 判限 / 写入）。
# 必须与 LocalStateStore.hit 的语义逐条对齐，否则限流行为会随后端漂移。
_SLIDING_WINDOW_LUA = """
local now    = tonumber(ARGV[1])
local limit  = tonumber(ARGV[2])
local window = tonumber(ARGV[3])
local member = ARGV[4]

-- 清掉滑出窗口的旧时间戳（score <= now-window，等价于本地 deque 的 popleft）
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
local cnt = redis.call('ZCARD', KEYS[1])

if cnt >= limit then
  if cnt == 0 then
    -- 限额为 0 的防御分支：窗口结束后才放行（与本地实现一致）
    return {0, math.floor(window)}
  end
  local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  local t = tonumber(oldest[2])
  local retry = math.floor(t + window - now) + 1
  if retry < 1 then retry = 1 end
  return {0, retry}
end

redis.call('ZADD', KEYS[1], now, member)
redis.call('PEXPIRE', KEYS[1], math.ceil(window * 1000))
return {1, 0}
"""


class RedisStateStore(StateStore):
    """Redis 版滑动窗口（ZSET + Lua 原子执行），供多副本共享配额。"""

    def __init__(self, redis_url: Optional[str] = None, client=None,
                 key_prefix: str = KEY_PREFIX, fail_open: bool = True):
        self._key_prefix = key_prefix
        self.fail_open = fail_open
        self._client = client
        self._url = redis_url or os.environ.get(
            "TELEOPS_REDIS_URL", "redis://127.0.0.1:6379/0")
        if self._client is None:
            from .redis_factory import from_url  # 延迟导入
            self._client = from_url(self._url, decode_responses=True)
        self._script = self._client.register_script(_SLIDING_WINDOW_LUA)

    def _k(self, key: str) -> str:
        return f"{self._key_prefix}{key}"

    def hit(self, key: str, limit: int, window: float) -> Tuple[bool, int]:
        now = time.time()
        # member 需唯一：同一毫秒的两次命中若共用 member，ZADD 会覆盖而非累加
        member = f"{time.time_ns()}-{uuid.uuid4().hex[:8]}"
        try:
            ok, retry = self._script(
                keys=[self._k(key)], args=[repr(now), limit, repr(window), member])
        except Exception as e:  # 连接失败/超时等旁路故障
            if self.fail_open:
                # 旁路组件故障不应压垮业务：放行，并让调用方有机会记录
                RedisStateStore.last_error = str(e)
                return True, 0
            raise
        return bool(int(ok)), int(retry)

    def reset(self, key: Optional[str] = None) -> None:
        if key is not None:
            self._client.delete(self._k(key))
            return
        # 不清库（FLUSHDB 太危险）：只删本前缀下的 key
        pattern = f"{self._key_prefix}*"
        for k in self._client.scan_iter(match=pattern, count=500):
            self._client.delete(k)

    last_error: Optional[str] = None


_store: Optional[StateStore] = None
_store_lock = threading.Lock()


def get_state_store() -> StateStore:
    """按环境变量返回全局状态后端（进程内缓存单例，首次调用时构建）。"""
    global _store
    if _store is not None:
        return _store
    with _store_lock:
        if _store is not None:
            return _store
        backend = os.environ.get("TELEOPS_STATE_STORE", "local").strip().lower()
        if backend == "redis":
            fail_open = os.environ.get(
                "TELEOPS_STATE_STORE_FAIL_OPEN", "on").strip().lower() in (
                "1", "on", "true", "yes")
            _store = RedisStateStore(fail_open=fail_open)
        else:
            _store = LocalStateStore()
        return _store


def configure_state_store(store: Optional[StateStore]) -> None:
    """显式指定后端（测试用）；传 None 则按环境变量重建。"""
    global _store
    with _store_lock:
        _store = store


# ---------------------------------------------------------------------------
# 异步任务状态（_jobs）
# ---------------------------------------------------------------------------
# 与上面的滑动窗口是两类不同的状态，因此单开一个接口：任务需要"按 key 存/取/遍历/
# 裁剪/过期清理"，而不是计数。做多副本时同样需要共享 —— 否则在 A 副本发起的任务，
# 前端轮询到 B 副本会查无此任务（状态灯永远转圈）。


class JobStore(ABC):
    """异步任务状态的存储接口（dict 语义 + 容量/TTL 治理）。"""

    @abstractmethod
    def get(self, key: str, default=None):
        ...

    @abstractmethod
    def set(self, key: str, value: dict) -> None:
        ...

    @abstractmethod
    def delete(self, key: str) -> None:
        ...

    @abstractmethod
    def values(self) -> list:
        """返回全部任务 dict（用于统计 running 数）。"""

    def count_running(self) -> int:
        return sum(1 for v in self.values() if v.get("status") == "running")

    @abstractmethod
    def trim(self, max_keep: int) -> None:
        """超过容量时删除最旧的（按 ts 升序）。"""

    @abstractmethod
    def prune_expired(self, ttl: float) -> None:
        """删除已结束（done/error）且超过 TTL 的任务。"""


class LocalJobStore(JobStore):
    """进程内任务表（默认）：直接存原生对象，与改造前完全一致。"""

    def __init__(self):
        self._d = {}
        self._lock = threading.Lock()

    def get(self, key, default=None):
        with self._lock:
            return self._d.get(key, default)

    def set(self, key, value):
        with self._lock:
            self._d[key] = value

    def delete(self, key):
        with self._lock:
            self._d.pop(key, None)

    def values(self):
        with self._lock:
            return list(self._d.values())

    def trim(self, max_keep):
        with self._lock:
            if len(self._d) <= max_keep:
                return
            oldest = sorted(self._d.keys(),
                            key=lambda k: self._d[k].get("ts", 0))[:len(self._d) - max_keep]
            for k in oldest:
                del self._d[k]

    def prune_expired(self, ttl):
        now = time.time()
        with self._lock:
            expired = [k for k, v in self._d.items()
                       if v.get("status") in ("done", "error")
                       and now - v.get("ts", 0) > ttl]
            for k in expired:
                del self._d[k]


class RedisJobStore(JobStore):
    """Redis 版任务表（每个任务一个 JSON 字符串 key），供多副本共享任务状态。"""

    def __init__(self, redis_url: Optional[str] = None, client=None,
                 key_prefix: str = "teleops:job:", ttl: float = 3600.0):
        self._prefix = key_prefix
        self._ttl = ttl
        self._client = client
        if self._client is None:
            from .redis_factory import from_url  # 延迟导入
            self._client = from_url(
                redis_url or os.environ.get("TELEOPS_REDIS_URL",
                                            "redis://127.0.0.1:6379/0"),
                decode_responses=True)

    def _k(self, key):
        return f"{self._prefix}{key}"

    @staticmethod
    def _dump(v):
        # default=str：任务结果里可能夹带非 JSON 原生对象，退化成字符串也不能写失败
        return json.dumps(v, ensure_ascii=False, default=str)

    @staticmethod
    def _load(raw):
        return json.loads(raw) if raw else None

    def get(self, key, default=None):
        raw = self._client.get(self._k(key))
        if raw is None:
            return default
        try:
            return self._load(raw)
        except Exception:
            return default

    def set(self, key, value):
        self._client.set(self._k(key), self._dump(value), ex=int(self._ttl))

    def delete(self, key):
        self._client.delete(self._k(key))

    def values(self):
        out = []
        for k in self._client.scan_iter(match=f"{self._prefix}*", count=500):
            raw = self._client.get(k)
            if raw is None:
                continue
            try:
                out.append(self._load(raw))
            except Exception:
                continue
        return out

    def trim(self, max_keep):
        items = []
        for k in self._client.scan_iter(match=f"{self._prefix}*", count=500):
            raw = self._client.get(k)
            if raw is None:
                continue
            try:
                items.append((k, self._load(raw)))
            except Exception:
                continue
        if len(items) <= max_keep:
            return
        items.sort(key=lambda kv: (kv[1] or {}).get("ts", 0))
        for k, _ in items[:len(items) - max_keep]:
            self._client.delete(k)

    def prune_expired(self, ttl):
        now = time.time()
        for k in self._client.scan_iter(match=f"{self._prefix}*", count=500):
            raw = self._client.get(k)
            if raw is None:
                continue
            try:
                v = self._load(raw)
            except Exception:
                self._client.delete(k)
                continue
            if v.get("status") in ("done", "error") and now - v.get("ts", 0) > ttl:
                self._client.delete(k)


_job_store: Optional[JobStore] = None
_job_store_lock = threading.Lock()


def get_job_store() -> JobStore:
    """按环境变量返回任务状态后端（与滑动窗口共用同一开关，避免配置分裂）。"""
    global _job_store
    if _job_store is not None:
        return _job_store
    with _job_store_lock:
        if _job_store is not None:
            return _job_store
        backend = os.environ.get("TELEOPS_STATE_STORE", "local").strip().lower()
        _job_store = RedisJobStore() if backend == "redis" else LocalJobStore()
        return _job_store


def configure_job_store(store: Optional[JobStore]) -> None:
    """显式指定任务状态后端（测试用）；None 则按环境变量重建。"""
    global _job_store
    with _job_store_lock:
        _job_store = store
