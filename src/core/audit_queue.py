"""异步审计写入：把 db.audit 调用从请求线程移出，后台线程消费，降低写操作延迟。

设计要点：
- 入队（enqueue）O(1) 立即返回，请求路径零阻塞；
- 后台 daemon 线程串行执行待审计调用；调用仍走 db.audit（自带 _LOCK 串行化，
  线程安全），存储语义不变（仍是 audit_log 表，前端 /audit 读取无感）；
- atexit 注册 flush()，进程退出前尽量排空内存队列，尽量不丢审计；
- 队列满降级（计数丢弃），审计旁路绝不拖垮业务。
"""
from __future__ import annotations

import atexit
import os
import threading
from queue import Empty, Queue
from typing import Callable, Optional

_MAX_QUEUE = 20000


def _sync_enabled() -> bool:
    """是否同步执行审计（测试用开关），每次调用时读 env，便于运行期切换。"""
    return os.environ.get("TELEOPS_AUDIT_SYNC", "0").strip().lower() in (
        "1", "on", "true", "yes")


class AuditWriter:
    def __init__(self, max_queue: int = _MAX_QUEUE):
        self._queue: "Queue[Callable[[], None]]" = Queue(maxsize=max_queue)
        self._dropped = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="audit-writer", daemon=True)
        self._thread.start()
        atexit.register(self.flush)

    def enqueue(self, fn: Callable[[], None]) -> None:
        """提交一个审计调用（无参 callable），立即返回，不阻塞调用方。

        ``TELEOPS_AUDIT_SYNC=1`` 时**同步执行**（测试用）：审计断言类用例会在动作
        之后立刻查 /audit，若仍走后台线程就会撞上"还没落库"的竞态，表现为
        CI 上不同用例轮流红（每次换一个）。测试环境因此默认同步；生产保持异步。
        env 在**调用时**读取，便于单测在运行期切换（见 tests/test_audit_queue.py）。
        """
        if _sync_enabled():
            try:
                fn()
            except Exception:
                pass  # 与后台线程一致：审计失败不影响主流程
            return
        try:
            self._queue.put_nowait(fn)
        except Exception:
            self._dropped += 1

    @property
    def dropped(self) -> int:
        return self._dropped

    def _drain_one(self, timeout: float) -> bool:
        try:
            fn = self._queue.get(timeout=timeout)
        except Empty:
            return False
        try:
            fn()
        except Exception:
            pass  # db.audit 内部已静默吞异常，这里双重保险
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            self._drain_one(1.0)

    def flush(self) -> None:
        """排空队列并停止消费线程（供 atexit / 测试用）。"""
        while self._drain_one(0):
            pass
        self._stop.set()


_writer: Optional[AuditWriter] = None
_writer_lock = threading.Lock()


def get_writer() -> AuditWriter:
    """进程内单例：首次调用起 daemon 线程并注册 atexit flush。"""
    global _writer
    if _writer is None:
        with _writer_lock:
            if _writer is None:
                _writer = AuditWriter()
    return _writer
