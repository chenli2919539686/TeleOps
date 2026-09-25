#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""审批态 / 运行时设置 跨副本外部化集成测试（v0.8.44）。

用真实 Redis 二进制（tools/redis/redis-server.exe，redis-64 3.0.x，只支持 RESP2）
起一个纯内存实例，为审批单与设置各建**两个独立 store 实例**（模拟两个 API 副本），
断言「副本 A 创建/决定 / 副本 B 立即可见」「设置 A 切换 / 副本 B 立即可见」——
这是「多副本无状态、审批态不再各持一份」核心承诺的回归守卫。

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

from src.core import approvals  # noqa: E402
from src.core import settings  # noqa: E402
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
    # redis-64 3.0.x 不支持 HELLO/RESP3，强制 RESP2（经 redis_factory 读取该 env）
    os.environ.setdefault("TELEOPS_REDIS_PROTOCOL", "2")
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
# 1) 审批单：创建 / 决定 跨副本共享
# ---------------------------------------------------------------------------
def test_approvals_shared_across_replicas(redis_url):
    c = _client(redis_url)
    c.flushdb()
    a = approvals.RedisApprovalStore(client=c)
    b = approvals.RedisApprovalStore(client=c)
    # 副本 A 创建 → 副本 B 立即可见
    aid = a.create("tool.build", "alice", {"agent_id": "dev-1", "feedback": "x"})
    assert aid.startswith("apr-")
    got = b.get(aid)
    assert got is not None and got["status"] == "pending"
    assert any(i["id"] == aid for i in b.list_items(status="pending"))
    # 普通用户只看自己发起/处置的（跨副本过滤一致）
    assert b.list_items(uid="bob") == []
    # 副本 B 决定 → 副本 A 看到（双向共享）
    decided = b.decide(aid, "approved", "admin")
    assert decided is not None and decided["status"] == "approved"
    assert decided["decided_by"] == "admin"
    assert a.get(aid)["status"] == "approved"
    # 已决定的单子不可二次决定（Lua 原子裁决）
    again = b.decide(aid, "rejected", "admin")
    assert again["status"] == "approved"
    c.flushdb()


# ---------------------------------------------------------------------------
# 2) 运行时设置：切换 跨副本共享
# ---------------------------------------------------------------------------
def test_settings_shared_across_replicas(redis_url):
    c = _client(redis_url)
    c.flushdb()
    a = settings.RedisSettingsStore(client=c)
    b = settings.RedisSettingsStore(client=c)
    # 无值 → env 兜底（测试环境未设 TELEOPS_REQUIRE_APPROVAL）
    assert a.get() is False
    # 副本 A 开启 → 副本 B 立即可见
    a.set(True)
    assert b.get() is True
    # 副本 B 关回 → 副本 A 也看到（双向共享）
    b.set(False)
    assert a.get() is False
    c.flushdb()
