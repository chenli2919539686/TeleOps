# -*- coding: utf-8 -*-
"""异步审计写入单测 + 与 db 集成的端到端测。

说明：
- db 是模块级全局连接，conftest 在 import 前把 TELEOPS_DB_FILE 指向临时 SQLite，
  故集成测不会污染生产 data/teleops.db；
- AuditWriter 后台线程调 db.audit 经 db._LOCK 串行化，线程安全；
- 单例 get_writer() 在 server 运行路径懒加载，测试用 new 实例隔离，flush 后停线程。
"""
from __future__ import annotations

from src.core.audit_queue import AuditWriter


def test_enqueue_nonblocking_and_async():
    """入队 O(1) 立即返回；审计调用在 flush 后才真正执行（请求路径零阻塞）。"""
    w = AuditWriter(max_queue=100)
    calls = []
    w.enqueue(lambda: calls.append(1))
    assert calls == []           # 入队即返回，尚未执行
    w.flush()
    assert calls == [1]          # 排空后被执行


def test_order_preserved():
    """后台线程串行消费，顺序与入队一致。"""
    w = AuditWriter(max_queue=100)
    order = []
    for i in range(5):
        w.enqueue(lambda i=i: order.append(i))
    w.flush()
    assert order == [0, 1, 2, 3, 4]


def test_queue_full_degrades():
    """队列满时降级丢弃（计数），flush 不抛异常。"""
    w = AuditWriter(max_queue=2)
    for _ in range(5):
        w.enqueue(lambda: None)
    assert w.dropped >= 1
    w.flush()                     # 不抛


def test_integration_persists_to_db():
    """经 AuditWriter 落 db.audit，flush 后 audit_log 可见（端到端异步写）。"""
    from src.core import db
    before = db.query_one("SELECT COUNT(*) AS c FROM audit_log")["c"]
    w = AuditWriter(max_queue=100)
    w.enqueue(lambda: db.audit(
        "probe", "test.audit.async", workspace_id=None,
        detail={"k": "v"}, result="ok", actor_id=None, ip="127.0.0.1"))
    w.flush()
    after = db.query_one("SELECT COUNT(*) AS c FROM audit_log")["c"]
    assert after == before + 1
