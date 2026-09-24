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
