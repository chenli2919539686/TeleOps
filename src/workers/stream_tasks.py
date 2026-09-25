"""告警流的队列任务（供 RQ worker 消费）。

设计要点：
- **剧本不进 Redis**：``build_playlist`` 是确定性的（固定 seed=42），worker 侧
  按 profile 用同样的样本重建即可得到同一份剧本，省掉大数组的传输与序列化；
  只把「播到第几条」(idx) 放进共享状态。
- **处理回调可重建**：真实处置逻辑（ops Agent）由 server 在启动时注册工厂，
  worker 进程 import server 即可得到同一套工厂 —— 所以 worker 必须先 import
  server，不能只 import 本模块。
- **一次任务只播一条**：播完按 interval 把下一条排进队列；stop 只是把
  running 置 False，下一个 tick 看到就自然停下（不再续排）。
"""
from __future__ import annotations

import os
from typing import Any, Callable, Optional

_PROCESSOR_FACTORY: Optional[Callable[[str], Callable[[dict], dict]]] = None


def set_processor_factory(fn: Callable[[str], Callable[[dict], dict]]) -> None:
    """注册「按业务域取处置回调」的工厂（由 server 在启动时调用）。"""
    global _PROCESSOR_FACTORY
    _PROCESSOR_FACTORY = fn


def processor_for(ws_id: str) -> Callable[[dict], dict]:
    if _PROCESSOR_FACTORY is None:
        raise RuntimeError(
            "未注册处置回调工厂：worker 进程需先 import src.api.server "
            "（它会在启动时注册），否则无法重建 ops Agent 处置链路")
    return _PROCESSOR_FACTORY(ws_id)


_QUEUE_FACTORY: Optional[Callable[[], Any]] = None


def set_queue_factory(fn: Optional[Callable[[], Any]]) -> None:
    """注入队列工厂（测试用）：让 tick 用内存版 Redis，不必真连 6379。"""
    global _QUEUE_FACTORY
    _QUEUE_FACTORY = fn


def _queue():
    if _QUEUE_FACTORY is not None:
        return _QUEUE_FACTORY()
    from redis import Redis
    from rq import Queue
    conn = Redis.from_url(os.environ.get("TELEOPS_REDIS_URL",
                                         "redis://127.0.0.1:6379/0"))
    return Queue(os.environ.get("TELEOPS_STREAM_QUEUE", "teleops:stream"),
                 connection=conn)


def tick(ws_id: str) -> dict:
    """播一条告警：取剧本 → 处置 → 写回共享状态 → 续排下一条。

    这是入队的最小单元（必须是模块级可 import 函数，RQ 不接受 __main__ 里的函数）。
    """
    from datetime import timedelta

    from src.core import stream_state as ss
    from src.core.alert_stream import build_playlist
    from src.core.data_files import load_alerts
    from src.core.stream_executor import apply_tick_result

    store = ss.get_stream_state_store()
    st = store.get(ws_id)
    if not st or not st.get("running"):
        return {"skipped": True, "ws_id": ws_id}

    playlist = build_playlist(load_alerts().get("alerts", []),
                              profile=st.get("profile", "mixed"))
    n = len(playlist)
    if n == 0:
        st["last_error"] = "剧本为空"
        st["running"] = False
        store.save(ws_id, st)
        return {"error": "empty_playlist", "ws_id": ws_id}

    idx = int(st.get("idx", 0))
    alert = playlist[idx % n]

    try:
        item = processor_for(ws_id)(alert) or {}
    except Exception as e:          # 单条失败不打断整条流水线
        item = {"error": f"{type(e).__name__}: {e}",
                "summary": "处置异常，已跳过（不打断流水线）"}

    apply_tick_result(ws_id, item, alert)

    # 推进指针 / 循环
    st = store.get(ws_id) or st
    idx += 1
    if idx >= n:
        if not st.get("loop", True):
            st["running"] = False
            st["idx"] = idx
        else:
            st["idx"] = 0
            st["rounds"] = int(st.get("rounds", 0)) + 1
    else:
        st["idx"] = idx
    store.save(ws_id, st)

    # 续排下一条（保留节拍）
    if st.get("running"):
        delay = max(0.0, float(st.get("interval_ms", 1200)) / 1000.0)
        if delay > 0:
            _queue().enqueue_in(timedelta(seconds=delay), tick, ws_id)
        else:
            _queue().enqueue(tick, ws_id)
    return {"ok": True, "ws_id": ws_id, "idx": st.get("idx"), "seq": st.get("seq")}
