"""告警流状态共享层（D3 第三步：流水线调度外部化的前提）。

为什么需要这一层
----------------
改造前，一条告警流 = 进程内一个 ``AlertStream`` 对象：线程、feed、任务队列、
计数器全在这个对象里。于是"谁启动的流只有那个进程看得到" —— 一旦把播放搬到
外部任务队列（worker 进程），API 进程就必须能从**别处**读到流的运行状态，
否则前端轮询到的永远是本机的空状态。

本模块把"一条流的全部可变状态"抽成可共享的快照：

- :class:`LocalStreamStateStore`（默认）：进程内字典，与现状等价；
- :class:`RedisStreamStateStore`：JSON 落到 Redis，worker 与 API 共享同一份。

注意：本层只管"状态"，不管"谁来跑"（线程还是 worker）—— 那是
:mod:`src.core.stream_executor` 的职责。
"""
from __future__ import annotations

import os
import json
import threading
import time
from abc import ABC, abstractmethod
from typing import Dict, Optional

KEY_PREFIX = os.environ.get("TELEOPS_STREAM_PREFIX", "teleops:stream:")


def new_state(ws_id: str, profile: str = "mixed", interval_ms: int = 1200,
              loop: bool = True, started_by: str = "",
              playlist_len: int = 0, ops_agent_id: str = "") -> dict:
    """一条流的初始状态快照（字段与 AlertStream.status() 的对外契约保持一致）。"""
    return {
        "ws_id": ws_id,
        "running": False,
        "profile": profile,
        "interval_ms": interval_ms,
        "loop": loop,
        "started_by": started_by,
        "started_at": None,
        "ops_agent_id": ops_agent_id,
        "playlist_len": playlist_len,
        "rounds": 0,
        "idx": 0,
        "seq": 0,
        "feed": [],            # 环形缓冲，由写入方按 FEED_MAX 裁剪
        "tasks": [],           # 作战室任务队列
        "current": None,
        "last_error": None,
        "stats": {"ingested": 0, "noise": 0, "real": 0, "created": 0,
                  "reused": 0, "pending": 0, "errors": 0},
    }


class StreamStateStore(ABC):
    @abstractmethod
    def get(self, ws_id: str) -> Optional[dict]:
        ...

    @abstractmethod
    def save(self, ws_id: str, state: dict) -> None:
        ...

    @abstractmethod
    def delete(self, ws_id: str) -> None:
        ...

    @abstractmethod
    def all(self) -> Dict[str, dict]:
        ...


class LocalStreamStateStore(StreamStateStore):
    """进程内实现（默认）：直接存原生 dict，与改造前一致。"""

    def __init__(self):
        self._d: Dict[str, dict] = {}
        self._lock = threading.Lock()

    def get(self, ws_id):
        with self._lock:
            return self._d.get(ws_id)

    def save(self, ws_id, state):
        with self._lock:
            self._d[ws_id] = state

    def delete(self, ws_id):
        with self._lock:
            self._d.pop(ws_id, None)

    def all(self):
        with self._lock:
            return dict(self._d)


def _as_str(x):
    """redis 客户端可能配了 decode_responses=False（RQ 需要），这里兜底统一成 str。"""
    return x.decode("utf-8") if isinstance(x, (bytes, bytearray)) else x


class RedisStreamStateStore(StreamStateStore):
    """Redis 实现：worker 与 API 进程共享同一份流状态。

    注意：``save`` 必须整体写回。**外部存储不像进程内字典那样支持原地改字段** ——
    ``store.get(k)["x"] = 1`` 改的是一份临时拷贝，不会生效。
    """

    def __init__(self, redis_url: Optional[str] = None, client=None,
                 key_prefix: str = KEY_PREFIX, ttl: int = 86400):
        self._prefix = key_prefix
        self._ttl = ttl
        self._client = client
        if self._client is None:
            from .redis_factory import from_url  # 延迟导入
            self._client = from_url(
                redis_url or os.environ.get("TELEOPS_REDIS_URL",
                                            "redis://127.0.0.1:6379/0"),
                decode_responses=True)

    def _k(self, ws_id):
        return f"{self._prefix}{ws_id}"

    def get(self, ws_id):
        raw = self._client.get(self._k(ws_id))
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    def save(self, ws_id, state):
        # default=str：feed 里可能夹带非 JSON 原生对象，退化保存也不能写失败
        self._client.set(self._k(ws_id),
                         json.dumps(state, ensure_ascii=False, default=str),
                         ex=self._ttl)

    def delete(self, ws_id):
        self._client.delete(self._k(ws_id))

    def all(self):
        out = {}
        for k in self._client.scan_iter(match=f"{self._prefix}*", count=500):
            raw = self._client.get(k)
            if raw is None:
                continue
            try:
                st = json.loads(raw)
            except Exception:
                continue
            out[_as_str(k)[len(self._prefix):]] = st
        return out


_store: Optional[StreamStateStore] = None
_lock = threading.Lock()


def get_stream_state_store() -> StreamStateStore:
    """按环境变量返回流状态后端（与限流/任务共用 TELEOPS_STATE_STORE 开关）。"""
    global _store
    if _store is not None:
        return _store
    with _lock:
        if _store is not None:
            return _store
        backend = os.environ.get("TELEOPS_STATE_STORE", "local").strip().lower()
        _store = RedisStreamStateStore() if backend == "redis" else LocalStreamStateStore()
        return _store


def configure_stream_state_store(store: Optional[StreamStateStore]) -> None:
    """显式指定后端（测试用）；None 则按环境变量重建。"""
    global _store
    with _lock:
        _store = store


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


def _ts() -> float:
    return time.time()
