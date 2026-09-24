#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""告警流队列 worker 入口（D3 第三步：流调度外部化）。

用法::

    # Linux / WSL2 / macOS（标准 worker，会 fork 子进程）
    python scripts/stream_worker.py

    # Windows（无 fork）→ 自动改用 SimpleWorker，单进程串行消费
    python scripts/stream_worker.py

前置：
  1. Redis 可用（TELEOPS_REDIS_URL，默认 redis://127.0.0.1:6379/0）
  2. API 侧设 TELEOPS_STREAM_EXECUTOR=queue，否则 /stream/start 仍走线程播放
     （worker 空转不会有任务进来）

说明：worker 必须先 import src.api.server —— 它会在启动时注册「按业务域取处置
回调」的工厂，worker 靠这个工厂重建 ops Agent 处置链路；只 import 任务模块会
拿不到处理器而报错。
"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

QUEUE_NAME = os.environ.get("TELEOPS_STREAM_QUEUE", "teleops:stream")
REDIS_URL = os.environ.get("TELEOPS_REDIS_URL", "redis://127.0.0.1:6379/0")


def main() -> int:
    # 关键：先 import server，它会注册处置回调工厂
    import src.api.server  # noqa: F401

    from redis import Redis
    from rq import Queue, SimpleWorker, Worker

    conn = Redis.from_url(REDIS_URL)
    queue = Queue(QUEUE_NAME, connection=conn)

    # Windows 没有 os.fork，标准 Worker 起不来 → 用不需要 fork 的 SimpleWorker。
    # 代价是单进程串行且没有 worker 子进程隔离，仅用于本地联调/演示；
    # 生产部署请在 Unix 上用标准 Worker。
    use_simple = (os.name == "nt") or os.environ.get(
        "TELEOPS_RQ_SIMPLE_WORKER", "").strip().lower() in ("1", "on", "true", "yes")

    print(f"[stream_worker] queue={QUEUE_NAME} redis={REDIS_URL} "
          f"worker={'SimpleWorker(无 fork)' if use_simple else 'Worker(fork)'}")
    if use_simple and os.name == "nt":
        print("  提示：Windows 无 fork，已自动降级 SimpleWorker；"
              "生产请部署到 Linux/WSL2 用标准 Worker。")

    worker_cls = SimpleWorker if use_simple else Worker
    w = worker_cls([queue], connection=conn)
    w.work()
    return 0


if __name__ == "__main__":
    sys.exit(main())
