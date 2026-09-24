"""告警流执行器抽象（D3 第三步：把"谁来跑这条流"变成可选项）。

两种实现：
- :class:`ThreadStreamExecutor`（**默认**）：进程内线程播放，就是现状的
  ``AlertStream``，行为完全不变，单机/演示零依赖。
- :class:`QueueStreamExecutor`：把"播一条告警"变成一个 RQ 任务，由外部
  worker 进程消费 —— 播放不再占用 API 进程，多副本下任何副本都能看到流状态。

选型说明（为何是 RQ 而不是 Celery）
----------------------------------
两者都依赖 Unix ``fork()``：**Windows 跑不了它们的 worker**（实测
``hasattr(os, 'fork') == False``）。因此本机（Windows）只能验证"入队"侧，
真正的 worker 要在 Linux/WSL2 部署环境起。这也是为什么默认仍是线程执行器 ——
保证开发机和演示不受影响，队列模式是给部署态准备的。
"""
from __future__ import annotations

import os
import threading
from typing import Any, Callable, Dict, List, Optional

from src.core import stream_state as ss


class StreamExecutor:
    """一条告警流的"播放器"接口。"""

    def start(self, ws_id: str, playlist: List[dict], **opts) -> None:
        raise NotImplementedError

    def stop(self, ws_id: str) -> None:
        raise NotImplementedError

    def is_running(self, ws_id: str) -> bool:
        raise NotImplementedError

    def status(self, ws_id: str) -> dict:
        raise NotImplementedError

    def feed(self, ws_id: str, after: int = 0) -> List[dict]:
        raise NotImplementedError

    def tasks(self, ws_id: str, limit: int = 50,
              agent_id: Optional[str] = None) -> List[dict]:
        raise NotImplementedError

    def stop_all(self) -> List[str]:
        """停掉所有在跑的流，返回被停掉的 ws_id 列表（供 /stream/reset-demo 使用）。"""
        raise NotImplementedError


class ThreadStreamExecutor(StreamExecutor):
    """进程内线程播放（默认）：直接驱动现有的 AlertStream，行为与改造前一致。"""

    def __init__(self, stream_of: Callable[[Optional[str]], Any],
                 streams: Optional[Dict[str, Any]] = None):
        """
        :param stream_of: 取（或建）该业务域 AlertStream 的函数，即 server 的 _stream_of
        :param streams: 全部流的映射（server 的 _streams），stop_all 需要
        """
        self._stream_of = stream_of
        self._streams = streams

    def stop_all(self):
        stopped = []
        if self._streams is None:
            return stopped
        for ws_id, st in list(self._streams.items()):
            if getattr(st, "running", False):
                st.stop()
                stopped.append(ws_id)
        return stopped

    def start(self, ws_id, playlist, process=None, **opts):
        stream = self._stream_of(ws_id)
        # 线程模式下处置回调要挂在 AlertStream 上（队列模式由 worker 侧工厂重建，
        # 传进来的 process 会被忽略）
        if process is not None:
            stream._process = process
        stream.start(
            playlist,
            profile=opts.get("profile", "mixed"),
            interval_ms=opts.get("interval_ms", 1200),
            loop=opts.get("loop", True),
            ops_agent_id=opts.get("ops_agent_id"),
            started_by=opts.get("started_by", ""),
        )

    def stop(self, ws_id):
        self._stream_of(ws_id).stop()

    def is_running(self, ws_id):
        return bool(self._stream_of(ws_id).running)

    def status(self, ws_id):
        return self._stream_of(ws_id).status()

    def feed(self, ws_id, after=0):
        return self._stream_of(ws_id).feed(after=after)

    def tasks(self, ws_id, limit=50, agent_id=None):
        items = self._stream_of(ws_id).tasks(limit=limit)
        if agent_id:
            items = [t for t in items if t.get("assigned_agent") == agent_id]
        return items


class QueueStreamExecutor(StreamExecutor):
    """队列播放：每次入队一个"播一条告警"的任务，由外部 worker 消费。

    播放所需的剧本不进 Redis —— ``build_playlist`` 是确定性的（固定 seed），
    worker 侧按 profile 重建即可得到同一份剧本，省掉大数组的传输与序列化。
    """

    def __init__(self, queue, state_store=None, queue_name: str = "teleops:stream"):
        self._queue = queue
        self._state = state_store or ss.get_stream_state_store()

    # ---------------- 控制 ----------------
    def start(self, ws_id, playlist, process=None, **opts):
        # process 在此模式下不使用：worker 侧会用自己的工厂重建处置回调
        st = self._state.get(ws_id) or ss.new_state(ws_id)
        st.update({
            "running": True,
            "profile": opts.get("profile", "mixed"),
            "interval_ms": opts.get("interval_ms", 1200),
            "loop": opts.get("loop", True),
            "started_by": opts.get("started_by", ""),
            "started_at": ss._now_iso(),
            "ops_agent_id": opts.get("ops_agent_id") or "",
            "playlist_len": len(playlist),
            # mode 存进共享状态：worker 侧靠它重建处置回调（见 server 的工厂）
            "mode": opts.get("mode") or "auto",
            "idx": 0, "rounds": 0, "seq": 0,
            "feed": [], "tasks": [], "current": None, "last_error": None,
            "stats": {"ingested": 0, "noise": 0, "real": 0, "created": 0,
                      "reused": 0, "pending": 0, "errors": 0},
        })
        self._state.save(ws_id, st)
        self._enqueue_tick(ws_id, delay=0)

    def stop(self, ws_id):
        st = self._state.get(ws_id)
        if not st:
            return
        st["running"] = False
        st["current"] = None
        self._state.save(ws_id, st)

    def is_running(self, ws_id):
        st = self._state.get(ws_id)
        return bool(st and st.get("running"))

    def stop_all(self):
        stopped = []
        for ws_id, st in self._state.all().items():
            if st.get("running"):
                st["running"] = False
                st["current"] = None
                self._state.save(ws_id, st)
                stopped.append(ws_id)
        return stopped

    # ---------------- 读取（供 API 进程使用） ----------------
    def status(self, ws_id):
        st = self._state.get(ws_id) or ss.new_state(ws_id)
        return {
            "running": bool(st.get("running")),
            "profile": st.get("profile", "mixed"),
            "interval_ms": st.get("interval_ms", 1200),
            "loop": st.get("loop", True),
            "rounds": st.get("rounds", 0),
            "queue_remaining": max(0, (st.get("playlist_len") or 0) - (st.get("idx") or 0)),
            "started_by": st.get("started_by", ""),
            "started_at": st.get("started_at"),
            "uptime_s": 0,
            "stats": st.get("stats", {}),
            "current": st.get("current"),
            "last_error": st.get("last_error"),
        }

    def feed(self, ws_id, after=0):
        st = self._state.get(ws_id)
        if not st:
            return []
        return [f for f in st.get("feed", []) if f.get("seq", 0) > after]

    def tasks(self, ws_id, limit=50, agent_id=None):
        st = self._state.get(ws_id)
        if not st:
            return []
        items = list(st.get("tasks", []))[-limit:]
        if agent_id:
            items = [t for t in items if t.get("assigned_agent") == agent_id]
        return items

    # ---------------- 内部 ----------------
    def _enqueue_tick(self, ws_id: str, delay: int = 0):
        from src.workers import stream_tasks  # 延迟导入，避免 rq 成为硬依赖
        if delay and delay > 0:
            self._queue.enqueue_in(_import_delay(delay), stream_tasks.tick, ws_id)
        else:
            self._queue.enqueue(stream_tasks.tick, ws_id)


def _import_delay(seconds: float):
    from datetime import timedelta
    return timedelta(seconds=seconds)


_executor: Optional[StreamExecutor] = None
_executor_lock = threading.Lock()


def get_stream_executor(stream_of: Optional[Callable] = None,
                        streams: Optional[Dict[str, Any]] = None) -> StreamExecutor:
    """按环境变量返回流执行器；默认线程执行器（与现状一致）。

    ``TELEOPS_STREAM_EXECUTOR=queue`` 时才切到队列模式（需要 rq + 可用 Redis，
    且真正播放要跑在能 fork 的 Unix worker 上）。
    """
    global _executor
    if _executor is not None:
        return _executor
    with _executor_lock:
        if _executor is not None:
            return _executor
        mode = os.environ.get("TELEOPS_STREAM_EXECUTOR", "thread").strip().lower()
        if mode == "queue":
            import redis as _redis  # 延迟导入
            from rq import Queue as _RQQueue
            conn = _redis.Redis.from_url(
                os.environ.get("TELEOPS_REDIS_URL", "redis://127.0.0.1:6379/0"))
            _executor = QueueStreamExecutor(_RQQueue("teleops:stream", connection=conn))
        else:
            if stream_of is None:
                raise ValueError("线程执行器需要 stream_of（server 的 _stream_of）")
            _executor = ThreadStreamExecutor(stream_of, streams=streams)
        return _executor


def configure_stream_executor(executor: Optional[StreamExecutor]) -> None:
    """显式指定执行器（测试用）；None 则按环境变量重建。"""
    global _executor
    with _executor_lock:
        _executor = executor


# 供 worker 侧复用：把「一条告警处置完」的结果写回共享状态
def apply_tick_result(ws_id: str, item: dict, alert: dict,
                      state_store=None) -> Dict[str, Any]:
    """把一次处置结果并入流状态（feed/任务队列/统计），返回更新后的状态。"""
    store = state_store or ss.get_stream_state_store()
    st = store.get(ws_id)
    if st is None:
        return {}
    st["seq"] = int(st.get("seq", 0)) + 1
    item = dict(item or {})
    item["seq"] = st["seq"]
    item["at"] = ss._now_iso()
    feed = st.get("feed", [])
    feed.append(item)
    st["feed"] = feed[-300:]                       # 与 AlertStream.FEED_MAX 对齐
    st["stats"]["ingested"] = int(st["stats"].get("ingested", 0)) + 1
    if item.get("noise"):
        st["stats"]["noise"] = int(st["stats"].get("noise", 0)) + 1
    else:
        st["stats"]["real"] = int(st["stats"].get("real", 0)) + 1
    if item.get("loop") == "created":
        st["stats"]["created"] = int(st["stats"].get("created", 0)) + 1
    if item.get("loop") == "reused":
        st["stats"]["reused"] = int(st["stats"].get("reused", 0)) + 1
    if item.get("error"):
        st["stats"]["errors"] = int(st["stats"].get("errors", 0)) + 1
        st["last_error"] = str(item["error"])
    st["current"] = None
    store.save(ws_id, st)
    return st
