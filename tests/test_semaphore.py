# -*- coding: utf-8 -*-
"""D3 收官：并发闸门（信号量）外部化。

默认本地实现（threading.Semaphore，行为不变）；Redis 实现让多副本共享同一份
许可池。这里重点验证分布式版最难也最要命的一条：**持有者崩溃漏掉许可后，
系统不能永久死锁** —— 靠租约 + 每次申请前回收过期许可来兜住。
"""
import time

import pytest

from src.core import semaphore as sem


def _fake_redis():
    try:
        import fakeredis
    except ImportError:
        pytest.skip("未安装 fakeredis，跳过 Redis 后端用例")
    return fakeredis.FakeStrictRedis(decode_responses=True)


@pytest.fixture()
def redis_store():
    return sem.RedisSemaphoreStore(client=_fake_redis())


@pytest.fixture()
def local_store():
    return sem.LocalSemaphoreStore()


# ---------------- 本地实现（默认） ----------------
def test_local_grants_up_to_limit(local_store):
    with local_store.semaphore("k", 2, timeout=0.1) as c1:
        assert c1.granted
        with local_store.semaphore("k", 2, timeout=0.1) as c2:
            assert c2.granted
            # 额度用尽：第三个拿不到（短超时，不阻塞测试）
            with local_store.semaphore("k", 2, timeout=0.1) as c3:
                assert not c3.granted
    # 释放后可再拿
    with local_store.semaphore("k", 2, timeout=0.1) as c4:
        assert c4.granted


def test_local_releases_on_exception(local_store):
    """异常路径也必须释放，否则一次异常就永久占掉一个许可。"""
    with pytest.raises(RuntimeError):
        with local_store.semaphore("k", 1, timeout=0.1):
            raise RuntimeError("boom")
    with local_store.semaphore("k", 1, timeout=0.1) as c:
        assert c.granted, "异常后许可必须已归还"


def test_default_store_is_local():
    """默认不得引入 redis 依赖。"""
    sem.configure_semaphore_store(None)
    assert isinstance(sem.get_semaphore_store(), sem.LocalSemaphoreStore)


# ---------------- Redis 实现 ----------------
def test_redis_grants_up_to_limit(redis_store):
    with redis_store.semaphore("k", 2, timeout=0.2) as c1:
        assert c1.granted
        with redis_store.semaphore("k", 2, timeout=0.2) as c2:
            assert c2.granted
            with redis_store.semaphore("k", 2, timeout=0.2) as c3:
                assert not c3.granted, "超过上限的申请不应拿到许可"
    assert redis_store.count("k") == 0, "退出上下文后许可应全部归还"


def test_redis_releases_on_exception(redis_store):
    with pytest.raises(RuntimeError):
        with redis_store.semaphore("k", 1, timeout=0.2):
            raise RuntimeError("boom")
    assert redis_store.count("k") == 0


def test_redis_reclaims_expired_lease(redis_store):
    """核心用例：持有者崩溃漏掉许可 → 超过租约后必须能被回收。

    没有这条，几次进程崩溃就会把所有许可漏光，全系统永久死锁。
    """
    client = redis_store._client
    key = redis_store._k("leaky")
    lease = 1.0
    # 伪造一个"很久以前申请、持有者已崩"的许可
    client.zadd(key, {"dead-token": time.time() - lease - 5})
    assert redis_store.count("leaky") == 1

    # 新申请者应顺手把它回收掉，从而拿到许可
    with redis_store.semaphore("leaky", 1, timeout=0.5, lease=lease) as c:
        assert c.granted, "过期租约必须被回收，否则会永久死锁"


def test_redis_does_not_reclaim_live_lease(redis_store):
    """反向用例：租约没超时的许可不能被误回收（否则并发上限失效）。"""
    client = redis_store._client
    key = redis_store._k("live")
    client.zadd(key, {"live-token": time.time()})   # 刚申请，租约未过期
    with redis_store.semaphore("live", 1, timeout=0.3, lease=120) as c:
        assert not c.granted, "仍在租约内的许可不能被抢"


def test_redis_fail_open_when_broker_down():
    """Redis 挂了不能把业务打死：默认放行（并发上限暂时失效但服务不断）。"""
    class _Broken:
        def register_script(self, _lua):
            raise ConnectionError("redis down")

        def zcard(self, _k):
            raise ConnectionError("redis down")

    store = sem.RedisSemaphoreStore(client=_Broken(), fail_open=True)
    with store.semaphore("k", 1, timeout=0.1) as c:
        assert not c.granted          # 没拿到许可
        # 但上下文正常进出，业务不中断（这就是 fail-open）
    assert store.last_error is not None
