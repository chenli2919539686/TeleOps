# -*- coding: utf-8 -*-
"""D3 第三步：告警流调度外部化（队列执行器 / 共享流状态）。

默认仍是线程执行器（demo 零变化），队列模式给多副本部署用。这里验证：
1. 共享流状态两种后端（本地/Redis）行为对齐；
2. 队列执行器 start 会真的入队、stop 能停下、stop_all 能全停；
3. tick 任务跑一条：处置结果写回共享状态（feed/seq）、并续排下一条；
   停止后续跑的 tick 应自行跳过、不再续排（不会"停不下来"）。

注意：RQ 的 worker 依赖 Unix fork，Windows 起不来 —— 所以这里只验证入队与
任务逻辑本身；真跑 worker 要在 Linux/WSL2（或用不需要 fork 的 SimpleWorker）。
"""
import pytest

from src.core import stream_state as ss
from src.core import stream_executor as se


def _fake_redis():
    """供状态存储用（decode_responses=True 便于断言）。"""
    try:
        import fakeredis
    except ImportError:
        pytest.skip("未安装 fakeredis，跳过 Redis 后端用例")
    return fakeredis.FakeStrictRedis(decode_responses=True)


def _fake_redis_raw():
    """供 RQ 用：RQ 会把任务体 zlib 压缩后存 Redis，开了 decode_responses
    会让 fakeredis 拿二进制当 UTF-8 解而炸掉，所以这里必须保持原始 bytes。"""
    import fakeredis
    return fakeredis.FakeStrictRedis()


@pytest.fixture()
def fake_queue(monkeypatch):
    from rq import Queue
    q = Queue("teleops:stream", connection=_fake_redis_raw())
    # 让 tick 内部创建队列时也用这个内存队列
    from src.workers import stream_tasks
    monkeypatch.setattr(stream_tasks, "_QUEUE_FACTORY", lambda: q, raising=False)
    return q


@pytest.fixture()
def state(monkeypatch):
    st = ss.LocalStreamStateStore()
    monkeypatch.setattr(ss, "_store", st, raising=False)
    ss.configure_stream_state_store(st)
    return st


# ---------------- 共享流状态 ----------------
def test_stream_state_local_redis_parity():
    local = ss.LocalStreamStateStore()
    redis_ = ss.RedisStreamStateStore(client=_fake_redis())
    for store in (local, redis_):
        store.save("ws-1", ss.new_state("ws-1", profile="story", playlist_len=6))
    assert local.get("ws-1") == redis_.get("ws-1")
    # 关键：外部存储不支持"原地改字段"（get 出来的是拷贝），必须整体写回。
    # 这里故意对两种后端用同一套 get→改→save 写法，锁死这个契约。
    for store in (local, redis_):
        st = store.get("ws-1")
        st["running"] = True
        store.save("ws-1", st)
    assert local.get("ws-1")["running"] is True
    assert redis_.get("ws-1")["running"] is True
    for store in (local, redis_):
        store.delete("ws-1")
    assert local.get("ws-1") is None and redis_.get("ws-1") is None


def test_stream_state_default_is_local():
    ss.configure_stream_state_store(None)
    assert isinstance(ss.get_stream_state_store(), ss.LocalStreamStateStore)


# ---------------- 队列执行器 ----------------
def test_queue_executor_start_enqueues_and_stop(state, fake_queue):
    ex = se.QueueStreamExecutor(fake_queue, state_store=state)
    assert ex.is_running("ws-1") is False
    ex.start("ws-1", [{"alert_id": "A-1"}], profile="story",
             interval_ms=0, loop=False, started_by="alice")
    assert ex.is_running("ws-1") is True
    # 启动即入队一个 tick
    assert len(fake_queue) == 1, "start 应把第一条播报任务放进队列"
    st = state.get("ws-1")
    assert st["started_by"] == "alice"
    assert st["profile"] == "story"

    ex.stop("ws-1")
    assert ex.is_running("ws-1") is False


def test_tick_processes_one_alert_and_reschedules(state, fake_queue):
    """tick 跑一条：结果写回共享状态，并在仍运行时续排下一条。"""
    from src.workers import stream_tasks

    seen = []

    def _fake_processor(alert):
        seen.append(alert)
        return {"noise": False, "summary": "ok", "loop": "none"}

    stream_tasks.set_processor_factory(lambda ws_id: _fake_processor)

    ex = se.QueueStreamExecutor(fake_queue, state_store=state)
    ex.start("ws-1", [], profile="story", interval_ms=0, loop=True)
    first_job = fake_queue.jobs[0]
    # 直接执行任务体（等价于 worker 取到任务后调用）
    stream_tasks.tick("ws-1")
    fake_queue.jobs.remove(first_job) if first_job in fake_queue.jobs else None

    st = state.get("ws-1")
    assert len(seen) == 1, "应只处置一条告警"
    assert st["seq"] == 1, "处置结果应写回共享状态（seq 递增）"
    assert len(st["feed"]) == 1
    assert st["feed"][0]["seq"] == 1
    assert st["stats"]["ingested"] == 1


def test_tick_after_stop_is_skipped_and_stops_loop(state, fake_queue):
    """停止后残留的 tick 应自行跳过，不再续排 —— 不会『停不下来』。"""
    from src.workers import stream_tasks
    calls = {"n": 0}

    def _processor(alert):
        calls["n"] += 1
        return {"noise": True, "summary": "x"}

    stream_tasks.set_processor_factory(lambda ws_id: _processor)

    ex = se.QueueStreamExecutor(fake_queue, state_store=state)
    ex.start("ws-1", [], profile="story", interval_ms=0, loop=True)
    ex.stop("ws-1")

    out = stream_tasks.tick("ws-1")
    assert out.get("skipped") is True, "已停止的流不应再处置"
    assert calls["n"] == 0


def test_stop_all_stops_every_running_stream(state, fake_queue):
    ex = se.QueueStreamExecutor(fake_queue, state_store=state)
    ex.start("ws-a", [], interval_ms=0, loop=True)
    ex.start("ws-b", [], interval_ms=0, loop=True)
    ex.start("ws-c", [], interval_ms=0, loop=True)
    ex.stop("ws-b")
    stopped = ex.stop_all()
    assert sorted(stopped) == ["ws-a", "ws-c"], "只应停仍在跑的流"
    assert ex.is_running("ws-a") is False and ex.is_running("ws-c") is False


def test_simple_worker_really_consumes_tick(state, fake_queue):
    """端到端：真让 RQ 的 worker 把任务消费掉。

    RQ 的标准 Worker 依赖 fork（Windows 没有），但 SimpleWorker 不需要 fork，
    可以在本机跑 —— 用 burst 模式把队列消费完即退出，验证"入队→worker→处置"
    这条链路真的通，而不只是入队成功。
    """
    from rq import SimpleWorker

    from src.workers import stream_tasks

    processed = []

    def _processor(alert):
        processed.append(alert.get("alert_id"))
        return {"noise": True, "summary": "噪声，已抑制", "loop": "none"}

    stream_tasks.set_processor_factory(lambda ws_id: _processor)

    ex = se.QueueStreamExecutor(fake_queue, state_store=state)
    ex.start("ws-1", [], profile="story", interval_ms=0, loop=False)

    w = SimpleWorker([fake_queue], connection=fake_queue.connection)
    w.work(burst=True)            # 处理完即退出，不常驻

    assert processed, "worker 应真的取到任务并处置"
    st = state.get("ws-1")
    assert st["seq"] >= 1, "处置结果应写回共享状态"
    assert len(st["feed"]) >= 1


# ---------------- 默认执行器仍是无依赖的线程版 ----------------
def test_default_executor_is_thread_based():
    """默认不得引入 rq/redis 依赖：应是线程执行器。"""
    se.configure_stream_executor(None)
    ex = se.get_stream_executor(lambda ws_id: None, streams={})
    assert isinstance(ex, se.ThreadStreamExecutor), \
        "默认必须是线程执行器，避免未部署 Redis 时 demo 起不来"
