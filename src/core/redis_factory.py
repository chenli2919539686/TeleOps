"""Redis 客户端工厂：统一从 URL 构造 redis-py 客户端。

为什么存在：redis-py 8.x 默认走 RESP3 并在握手时发 ``HELLO`` 命令，而 Redis 6 以前
（如 Windows 移植版 3.0.x）或某些托管/代理 Redis 不支持 ``HELLO`` 会直接报错。
通过环境变量 ``TELEOPS_REDIS_PROTOCOL`` 选择协议版本：

- 不设置 → 走 redis-py 默认（RESP3/HELLO，要求 Redis 6+，生产推荐）；
- 设为 ``2`` → 强制 RESP2，兼容旧版 Redis 与部分代理。

所有 Redis 后端（限流/任务/流/信号量/RQ 队列）统一经此工厂创建客户端，避免散落
``from_url`` 导致协议版本不一致。
"""
import os

import redis


def from_url(url: str, decode_responses: bool = True, **kwargs):
    """与 ``redis.Redis.from_url`` 等价，但支持 ``TELEOPS_REDIS_PROTOCOL`` 覆盖协议版本。"""
    proto = os.environ.get("TELEOPS_REDIS_PROTOCOL")
    if proto:
        kwargs["protocol"] = int(proto)
    return redis.Redis.from_url(url, decode_responses=decode_responses, **kwargs)
