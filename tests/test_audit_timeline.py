# -*- coding: utf-8 -*-
"""可回放审计时间线（v0.8.45）：GET /audit/timeline。

核心断言：
1. 正序返回：items 按 ts 升序（id ASC），detail 为 JSON 时解析为对象；
2. 密度分桶：bucket=minute 按分钟聚合，返回各桶 count / by_result；
3. 摘要统计：summary.by_result 与 summary.total 正确；
4. 过滤：actor 精确匹配 + action_prefix 前缀匹配；
5. 租户隔离：普通用户 own 视图看不到他人私有域，admin 全量可见；
6. 匿名 → 401。
"""
import json
import uuid

import pytest

from src.core import db


@pytest.fixture(scope="module", autouse=True)
def _seed_first_user(client):
    """吃掉 admin 名额，保证本文件后续注册出来的都是普通用户。"""
    _register(client, "tl_seed_" + uuid.uuid4().hex[:8])


def _register(client, username):
    r = client.post("/auth/register", json={"username": username, "password": "Pytest123456"})
    assert r.status_code in (200, 201), r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _personal_ws(client, headers):
    wss = client.get("/workspaces", headers=headers).json()["workspaces"]
    return next(w["id"] for w in wss if w["owner_id"] is not None)


def _insert_row(ts, actor, action, ws=None, result="ok", detail=None, actor_id=None, ip=None):
    db.execute(
        "INSERT INTO audit_log (ts,actor,actor_id,action,workspace_id,detail,result,ip) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (ts, actor, actor_id, action, ws,
         json.dumps(detail) if isinstance(detail, (dict, list)) else detail,
         result, ip))


def _timeline(client, headers, **params):
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return client.get(f"/audit/timeline?{qs}" if qs else "/audit/timeline", headers=headers)


def test_timeline_requires_login(client):
    """匿名访问 /audit/timeline → 401。"""
    r = client.get("/audit/timeline")
    assert r.status_code == 401, r.text


def test_timeline_chronological(client, admin_headers):
    """正序返回 + detail 解析为对象（admin 全量可见，便于校验数据形态）。"""
    actor = "tl_chrono_" + uuid.uuid4().hex[:8]
    _insert_row("2026-01-01T10:00:00", actor, "timeline.test", result="ok",
                detail={"step": 1})
    _insert_row("2026-01-01T10:00:05", actor, "timeline.test", result="ok",
                detail={"step": 2})
    _insert_row("2026-01-01T10:00:09", actor, "timeline.test", result="ok",
                detail={"step": 3})

    d = _timeline(client, admin_headers, actor=actor, action_prefix="timeline.").json()
    items = d["items"]
    assert len(items) == 3, items
    # 正序：相邻 ts 递增（字符串 ISO 比较与时刻一致）
    assert items[0]["ts"] <= items[1]["ts"] <= items[2]["ts"]
    # detail 解析为对象
    assert items[0]["detail"] == {"step": 1}
    assert d["total"] == 3


def test_timeline_bucket_density(client, admin_headers):
    """bucket=minute 按分钟聚合密度。"""
    actor = "tl_bucket_" + uuid.uuid4().hex[:8]
    for _ in range(2):
        _insert_row("2026-03-03T08:00:00", actor, "timeline.bk", result="ok")
    for _ in range(3):
        _insert_row("2026-03-03T08:01:00", actor, "timeline.bk", result="ok")

    d = _timeline(client, admin_headers, actor=actor, action_prefix="timeline.",
                  bucket="minute").json()
    buckets = d["buckets"]
    assert len(buckets) == 2, buckets
    counts = sorted(b["count"] for b in buckets)
    assert counts == [2, 3]
    assert d["summary"]["total"] == 5


def test_timeline_summary_by_result(client, admin_headers):
    """summary.by_result 正确统计 ok/denied/error。"""
    actor = "tl_sum_" + uuid.uuid4().hex[:8]
    _insert_row("2026-04-04T09:00:00", actor, "timeline.s", result="ok")
    _insert_row("2026-04-04T09:00:01", actor, "timeline.s", result="denied")
    _insert_row("2026-04-04T09:00:02", actor, "timeline.s", result="error")
    _insert_row("2026-04-04T09:00:03", actor, "timeline.s", result="ok")

    d = _timeline(client, admin_headers, actor=actor, action_prefix="timeline.").json()
    br = d["summary"]["by_result"]
    assert br["ok"] == 2 and br["denied"] == 1 and br["error"] == 1, br
    assert d["summary"]["total"] == 4


def test_timeline_actor_and_action_prefix_filters(client, admin_headers):
    """actor 精确匹配 + action_prefix 前缀匹配（用唯一 action 名避免与种子数据碰撞）。"""
    _insert_row("2026-05-05T09:00:00", "tl_actorA", "tl_ap.auth.login", result="ok")
    _insert_row("2026-05-05T09:00:01", "tl_actorA", "tl_ap.workspace.create", result="ok")
    _insert_row("2026-05-05T09:00:02", "tl_actorB", "tl_ap.auth.login", result="ok")

    # action_prefix=tl_ap.auth. 只返回两条 auth 类
    d = _timeline(client, admin_headers, action_prefix="tl_ap.auth.").json()
    assert all(i["action"].startswith("tl_ap.auth.") for i in d["items"]), d["items"]
    assert d["total"] == 2

    # actor=tl_actorA 只返回该操作人的两条
    d = _timeline(client, admin_headers, actor="tl_actorA").json()
    assert all(i["actor"] == "tl_actorA" for i in d["items"]), d["items"]
    assert d["total"] == 2


def test_timeline_isolation(client, admin_headers):
    """普通用户 own 视图看不到他人私有域；admin 全量可见。"""
    h_a = _register(client, "tl_a_" + uuid.uuid4().hex[:8])
    h_b = _register(client, "tl_b_" + uuid.uuid4().hex[:8])
    ws_a = _personal_ws(client, h_a)

    # 在 A 的私有域里直接插一条审计（模拟 A 的操作）
    _insert_row("2026-06-06T09:00:00", "tl_a", "workspace.mode", ws=ws_a, result="ok")

    # B 的 own 视图不应出现 ws_a 这条
    d = _timeline(client, h_b, limit=500).json()
    assert d["scope"] == "own"
    assert all(i["workspace_id"] != ws_a for i in d["items"]), \
        "B 在回放时间线里看到了 A 的私有域记录，隔离失效"

    # admin 全量可见
    d = _timeline(client, admin_headers, limit=500).json()
    assert d["scope"] == "all"
    assert any(i["workspace_id"] == ws_a for i in d["items"]), \
        "admin 回放时间线未包含普通用户私有域记录"
