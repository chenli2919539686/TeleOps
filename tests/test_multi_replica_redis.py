#!/usr/bin/env python3
"""多副本 Redis 集成测试（D3 收官 / 永久回归守卫）。

用真实 Redis 二进制（tools/redis/redis-server.exe，Windows 上可用的 redis-64 3.0.x，
注意它只支持 RESP2/单字段 HSET）起一个纯内存实例，针对 D3 外部化的四类共享状态：

  - 限流（RedisStateStore 滑动窗口 ZSET+Lua）
  - 任务（RedisJobStore JSON）
  - 流状态（RedisStreamStateStore JSON）
  - 并发闸门（RedisSemaphoreStore ZSET+Lua）

为每类建**两个独立实例**（模拟两个副本），都连同一份 Redis，断言「在实例 A 写入 /
占用，实例 B 立即可见」——这是 D3「多副本无状态」核心承诺的回归守卫。

前置：tools/redis/redis-server.exe 存在（从 nuget redis-64 解压）。缺失则整文件 skip，
CI 不会因无二进制而红。本测试不触碰 data/ 任何文件，Redis 纯内存、随进程销毁。
"""
import os
import sys
import time
import socket
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from src.core import state_store as ss  # noqa: E402
from src.core import stream_state as ssm  # noqa: E402
from src.core import semaphore as sem_mod  # noqa: E402
import redis  # noqa: E402

REDIS_EXE = os.path.join(REPO, "tools", "redis", "redis-server.exe")


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


@pytest.fixture(scope="module")
def redis_url():
    if not os.path.exists(REDIS_EXE):
        pytest.skip("tools/redis/redis-server.exe 不存在：从 nuget redis-64 解压后可跑真实 Redis 集成测试")
    port = _free_port()
    proc = subprocess.Popen(
        [REDIS_EXE, "--port", str(port), "--save", "", "--appendonly", "no",
         "--maxmemory", "128mb", "--maxmemory-policy", "allkeys-lru"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"redis://127.0.0.1:{port}/0"
    client = redis.Redis.from_url(url, protocol=2)
    ok = False
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if client.ping():
                ok = True
                break
        except Exception:
            time.sleep(0.3)
    if not ok:
        proc.terminate()
        pytest.skip("Redis 未在 15s 内就绪（可能被沙箱拦截）")
    yield url
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _client(url, decode=True):
    return redis.Redis.from_url(url, decode_responses=decode, protocol=2)


# ---------------------------------------------------------------------------
# 1) 限流：滑动窗口跨副本共享配额
# ---------------------------------------------------------------------------
def test_ratelimit_shared_across_replicas(redis_url):
    c = _client(redis_url)
    a = ss.RedisStateStore(client=c)
    b = ss.RedisStateStore(client=c)
    key = "mr:ratelimit"
    a.reset(key)
    # 副本 A 连续命中 2 次（limit=2）后到顶
    assert a.hit(key, 2, 10)[0] is True
    assert a.hit(key, 2, 10)[0] is True
    assert a.hit(key, 2, 10)[0] is False
    # 副本 B 看到的是同一份计数 → 同样被限（证明配额跨副本共享）
    assert b.hit(key, 2, 10)[0] is False
    a.reset(key)


# ---------------------------------------------------------------------------
# 2) 任务：异步任务状态跨副本共享
# ---------------------------------------------------------------------------
def test_jobstore_shared_across_replicas(redis_url):
    c = _client(redis_url)
    a = ss.RedisJobStore(client=c)
    b = ss.RedisJobStore(client=c)
    a.set("j-mr-1", {"status": "running", "ts": 1.0, "kind": "probe"})
    got = b.get("j-mr-1")
    assert got is not None
    assert got.get("status") == "running" and got.get("kind") == "probe"
    assert any(v.get("status") == "running" for v in b.values())
    a.delete("j-mr-1")
    assert b.get("j-mr-1") is None


# ---------------------------------------------------------------------------
# 3) 流状态：告警流 running/rounds 跨副本共享（D3 核心）
# ---------------------------------------------------------------------------
def test_streamstate_shared_across_replicas(redis_url):
    c = _client(redis_url)
    a = ssm.RedisStreamStateStore(client=c)
    b = ssm.RedisStreamStateStore(client=c)
    st = ssm.new_state("core-net", profile="mixed")
    st["running"] = True
    st["rounds"] = 3
    st["started_by"] = "replica-A"
    a.save("core-net", st)
    # 副本 B 立即可见（队列模式下这就是前端轮询到任意副本都一致的基础）
    got = b.get("core-net")
    assert got is not None
    assert got["running"] is True
    assert got["rounds"] == 3
    # 副本 B 改写 → 副本 A 也看到（控制面双向共享）
    got["running"] = False
    b.save("core-net", got)
    assert a.get("core-net")["running"] is False
    a.delete("core-net")
    assert b.get("core-net") is None


# ---------------------------------------------------------------------------
# 4) 并发闸门：信号量许可池跨副本共享
# ---------------------------------------------------------------------------
def test_semaphore_shared_across_replicas(redis_url):
    c = _client(redis_url)
    key = "mr:sem"
    c.delete(getattr(sem_mod, "KEY_PREFIX", "teleops:sem:") + key)
    a = sem_mod.RedisSemaphoreStore(client=c, fail_open=False)
    b = sem_mod.RedisSemaphoreStore(client=c, fail_open=False)
    with a.semaphore(key, limit=1, lease=30, timeout=1) as ctx_a:
        assert ctx_a.granted is True, "副本 A 应成功拿到唯一许可"
        # 副本 B 看到同一份许可池 → 计数为 1（证明跨副本共享）
        assert b.count(key) == 1
        # 副本 B 再申请应失败（limit=1 且租约未过期）
        with b.semaphore(key, limit=1, lease=30, timeout=1) as ctx_b:
            assert ctx_b.granted is False
    # 副本 A 的 with 退出 → 自动释放许可
    assert b.count(key) == 0
