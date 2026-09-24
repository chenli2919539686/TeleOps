# -*- coding: utf-8 -*-
"""D3 状态层抽象回归测试。

验证两件事：
1. 抽出的 LocalStateStore 与改造前的进程内限流语义一致（放行/拒绝/隔离/重置）。
2. RedisStateStore（fakeredis + Lua 真实执行）与 LocalStateStore **行为对齐** ——
   否则切换后端会导致限流抖动/漂移，抽接口就失去意义。
"""
import time

import pytest

from src.core import state_store as ss


def _fake_redis():
    try:
        import fakeredis
    except ImportError:
        pytest.skip("未安装 fakeredis，跳过 Redis 后端用例")
    return fakeredis.FakeStrictRedis(decode_responses=True)


@pytest.fixture()
def redis_store():
    return ss.RedisStateStore(client=_fake_redis())


@pytest.fixture()
def local_store():
    return ss.LocalStateStore()


# ---------------- LocalStateStore 基本语义 ----------------
def test_allows_up_to_limit_then_blocks(local_store):
    for _ in range(3):
        ok, retry = local_store.hit("k", limit=3, window=60.0)
        assert ok is True and retry == 0
    ok, retry = local_store.hit("k", limit=3, window=60.0)
    assert ok is False
    assert retry >= 1, "超限时必须给出 >=1 的 Retry-After"


def test_keys_are_isolated(local_store):
    assert local_store.hit("a", 1, 60.0)[0] is True
    assert local_store.hit("a", 1, 60.0)[0] is False
    # b 是独立桶，不受 a 影响
    assert local_store.hit("b", 1, 60.0)[0] is True


def test_window_expires(local_store):
    assert local_store.hit("k", 1, 0.3)[0] is True
    assert local_store.hit("k", 1, 0.3)[0] is False
    time.sleep(0.35)
    # 窗口滑出后应重新放行
    assert local_store.hit("k", 1, 0.3)[0] is True


def test_reset_clears(local_store):
    local_store.hit("k", 1, 60.0)
    local_store.reset("k")
    assert local_store.hit("k", 1, 60.0)[0] is True
    # reset() 全清
    local_store.hit("k", 1, 60.0)
    local_store.reset()
    assert local_store.hit("k", 1, 60.0)[0] is True


# ---------------- RedisStateStore 对齐相同的语义 ----------------
def test_redis_allows_up_to_limit_then_blocks(redis_store):
    for _ in range(3):
        ok, retry = redis_store.hit("k", limit=3, window=60.0)
        assert ok is True and retry == 0
    ok, retry = redis_store.hit("k", limit=3, window=60.0)
    assert ok is False
    assert retry >= 1


def test_redis_keys_are_isolated(redis_store):
    assert redis_store.hit("a", 1, 60.0)[0] is True
    assert redis_store.hit("a", 1, 60.0)[0] is False
    assert redis_store.hit("b", 1, 60.0)[0] is True


def test_redis_window_expires(redis_store):
    assert redis_store.hit("k", 1, 0.3)[0] is True
    assert redis_store.hit("k", 1, 0.3)[0] is False
    time.sleep(0.35)
    assert redis_store.hit("k", 1, 0.3)[0] is True


def test_redis_reset_clears(redis_store):
    redis_store.hit("k", 1, 60.0)
    redis_store.reset("k")
    assert redis_store.hit("k", 1, 60.0)[0] is True
    redis_store.reset()
    assert redis_store.hit("k", 1, 60.0)[0] is True


def test_local_and_redis_decision_parity(local_store, redis_store):
    """同一串请求序列在两个后端的放行/拒绝判定必须完全一致（后端可换但不许漂移）。"""
    limit, window = 4, 60.0
    local_flags, redis_flags = [], []
    for i in range(10):
        local_flags.append(local_store.hit(f"p{i}", limit, window)[0])
        redis_flags.append(redis_store.hit(f"p{i}", limit, window)[0])
    # 每项都是独立 key 的首次命中 → 都应放行
    assert local_flags == redis_flags
    assert all(local_flags)

    # 同一 key 连续打超限：两侧判定序列需一致
    local_flags = [local_store.hit("same", limit, window)[0] for _ in range(8)]
    redis_flags = [redis_store.hit("same", limit, window)[0] for _ in range(8)]
    assert local_flags == redis_flags
    assert local_flags == [True, True, True, True, False, False, False, False]


def test_default_backend_is_local():
    """默认不引入 Redis 依赖：后端应是进程内实现。"""
    ss.configure_state_store(None)
    store = ss.get_state_store()
    assert isinstance(store, ss.LocalStateStore), \
        "默认后端必须是 LocalStateStore，避免未部署 Redis 时服务不可用"


def test_rate_limit_delegates_to_store(monkeypatch):
    """rate_limit 走的是状态层（换后端即可生效），而不是自己维护私有字典。"""
    from src.core import rate_limit as rl
    fake = ss.LocalStateStore()
    monkeypatch.setattr(ss, "_store", fake, raising=False)
    ss.configure_state_store(fake)
    assert rl.allow("delegated", 1, 60.0)[0] is True
    assert rl.allow("delegated", 1, 60.0)[0] is False
    rl.reset("delegated")
    assert rl.allow("delegated", 1, 60.0)[0] is True
    ss.configure_state_store(None)


# ---------------- JobStore（异步任务状态） ----------------
@pytest.fixture()
def local_jobs():
    return ss.LocalJobStore()


@pytest.fixture()
def redis_jobs():
    return ss.RedisJobStore(client=_fake_redis())


def test_jobstore_roundtrip(local_jobs):
    local_jobs.set("j1", {"status": "running", "result": None, "ts": time.time()})
    assert local_jobs.get("j1")["status"] == "running"
    assert local_jobs.get("nope") is None
    assert local_jobs.get("nope", {"status": "not_found"})["status"] == "not_found"
    local_jobs.delete("j1")
    assert local_jobs.get("j1") is None


def test_jobstore_count_running(local_jobs):
    now = time.time()
    local_jobs.set("a", {"status": "running", "ts": now})
    local_jobs.set("b", {"status": "done", "ts": now})
    local_jobs.set("c", {"status": "running", "ts": now})
    assert local_jobs.count_running() == 2


def test_jobstore_trim_keeps_newest(local_jobs):
    base = time.time()
    for i in range(5):
        local_jobs.set(f"j{i}", {"status": "done", "ts": base + i})
    local_jobs.trim(2)
    assert len(local_jobs.values()) == 2
    assert {v["ts"] for v in local_jobs.values()} == {base + 3, base + 4}


def test_jobstore_prune_expired_only_finished(local_jobs):
    now = time.time()
    local_jobs.set("old_done", {"status": "done", "ts": now - 7200})
    local_jobs.set("old_run", {"status": "running", "ts": now - 7200})
    local_jobs.set("new_done", {"status": "done", "ts": now})
    local_jobs.prune_expired(3600)
    assert local_jobs.get("old_done") is None, "已结束且超 TTL 应被清理"
    assert local_jobs.get("old_run") is not None, "仍在跑的任务不能被清掉"
    assert local_jobs.get("new_done") is not None


def test_jobstore_local_and_redis_parity(local_jobs, redis_jobs):
    """两个后端在同一串操作后必须一致，切换后端才不会行为漂移。"""
    base = time.time()
    for store in (local_jobs, redis_jobs):
        store.set("x", {"status": "running", "result": None, "error": None, "ts": base})
        store.set("y", {"status": "done", "result": {"ok": 1}, "error": None, "ts": base + 1})
    assert local_jobs.get("x") == redis_jobs.get("x")
    assert local_jobs.get("y") == redis_jobs.get("y")
    assert local_jobs.count_running() == redis_jobs.count_running() == 1
    for store in (local_jobs, redis_jobs):
        store.delete("x")
    assert local_jobs.get("x") is None and redis_jobs.get("x") is None


def test_jobstore_redis_survives_non_json_result(redis_jobs):
    """结果里夹带非 JSON 原生对象时，Redis 后端应退化保存而不是写入失败。"""

    class _Weird:
        def __repr__(self):
            return "<weird>"

    redis_jobs.set("w", {"status": "done", "result": _Weird(), "ts": time.time()})
    got = redis_jobs.get("w")
    assert got is not None and got["status"] == "done"
    assert "weird" in str(got["result"])


def test_start_job_goes_through_job_store():
    """_start_job 的结果应能从状态层读到（多副本共享的前提）。"""
    from src.api.server import _jobs as server_jobs
    from src.api.server import _start_job
    jid = _start_job(lambda: {"echo": 42})
    for _ in range(100):
        job = server_jobs.get(jid)
        if job and job.get("status") != "running":
            break
        time.sleep(0.05)
    job = server_jobs.get(jid)
    assert job is not None, "任务应落在状态层而不是本地私有字典"
    assert job["status"] == "done", job
    assert job["result"] == {"echo": 42}
