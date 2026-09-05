# -*- coding: utf-8 -*-
"""告警流多租户隔离（v0.8.18）：流水线从全局单例改为按业务域多实例。

背景：此前 stream = AlertStream() 全局只有一条，任何用户点启动，所有人
（含 admin）的界面都会轮询到 running=true 并「自动跑」，且停止按钮默认
disabled 只有启动者能解锁 → 管理员看到 LIVE 却停不掉。

核心断言：
1. 普通用户在公共域 core-net 启动 → 403（公共域仅 admin 可写）；
2. admin 在公共域启动 → 200，status 带 started_by；
3. 用户 A 在个人域启动，不影响公共域（core-net 仍 running=false）——域间隔离；
4. 用户 B 查/停 A 的个人域流 → 404（不可见即不暴露存在）；
5. 按域停止：A 停自己域的流 → running=false；
6. 匿名启动 → 401（全局写鉴权中间件）；
7. /stream/reset-demo 清全局工具库 → 仅 admin 可用。
"""
import base64
import json
import uuid

import pytest


@pytest.fixture(scope="module", autouse=True)
def _seed_first_user(client):
    """首个注册用户会自动成为管理员——先注册一个占位用户吃掉 admin 名额，
    保证本文件后续 _register 出来的都是普通用户（权限断言可预期）。"""
    _register(client, "st_seed_" + uuid.uuid4().hex[:8])


def _register(client, username):
    r = client.post("/auth/register", json={"username": username, "password": "Pytest123456"})
    assert r.status_code in (200, 201), r.text
    token = r.json()["token"]
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    uid = json.loads(base64.urlsafe_b64decode(payload))["uid"]
    return headers, uid


def _personal_ws(client, headers):
    wss = client.get("/workspaces", headers=headers).json()["workspaces"]
    return next(w["id"] for w in wss if w["owner_id"] is not None)


def _start(client, headers, ws_id):
    return client.post("/stream/start", headers=headers,
                       json={"profile": "story", "interval_ms": 200,
                             "loop": True, "workspace_id": ws_id})


def _status(client, headers, ws_id):
    return client.get(f"/stream/status?workspace_id={ws_id}", headers=headers)


def _stop(client, headers, ws_id):
    return client.post(f"/stream/stop?workspace_id={ws_id}", headers=headers)


def test_normal_user_cannot_start_stream_in_public_domain(client):
    """公共域 core-net 仅 admin 可启动流水线，普通用户 403。"""
    headers, _ = _register(client, "stA_" + uuid.uuid4().hex[:8])
    r = _start(client, headers, "core-net")
    assert r.status_code == 403, r.text


def test_anonymous_cannot_start_stream(client):
    """匿名（无 JWT）启动 → 401：写接口统一走鉴权中间件。"""
    r = client.post("/stream/start", json={"workspace_id": "core-net"})
    assert r.status_code == 401, r.text


def test_admin_can_start_and_stop_public_stream(client, admin_headers):
    """admin 在公共域启动 → 200 且 status.started_by 正确；停止后 running=false。"""
    r = _start(client, admin_headers, "core-net")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["started_by"], "启动响应应带 started_by"
    try:
        s = _status(client, admin_headers, "core-net").json()
        assert s["running"] is True
        assert s["started_by"] == d["started_by"]
    finally:
        r2 = _stop(client, admin_headers, "core-net")
        assert r2.status_code == 200, r2.text
    assert _status(client, admin_headers, "core-net").json()["running"] is False


def test_stream_isolated_between_workspaces(client):
    """A 在个人域启动：自己的域 running=true，公共域不受影响（running=false）。"""
    headers, _ = _register(client, "stB_" + uuid.uuid4().hex[:8])
    ws = _personal_ws(client, headers)
    r = _start(client, headers, ws)
    assert r.status_code == 200, r.text
    try:
        assert _status(client, headers, ws).json()["running"] is True
        # 域间隔离：公共域的流水线纹丝不动，admin 不会被别人的流「自动跑」
        assert _status(client, headers, "core-net").json()["running"] is False
    finally:
        assert _stop(client, headers, ws).status_code == 200
    assert _status(client, headers, ws).json()["running"] is False


def test_other_user_cannot_see_or_stop_personal_stream(client):
    """B 既看不到也停不掉 A 个人域的流（不可见域 404，不暴露存在）。"""
    ha, _ = _register(client, "stC_" + uuid.uuid4().hex[:8])
    hb, _ = _register(client, "stD_" + uuid.uuid4().hex[:8])
    ws = _personal_ws(client, ha)
    assert _start(client, ha, ws).status_code == 200
    try:
        assert _status(client, hb, ws).status_code == 404, "不可见域的流不应被窥探"
        assert _stop(client, hb, ws).status_code == 404, "不可见域的流不应能被停"
        assert _status(client, ha, ws).json()["running"] is True, "B 的请求不应影响 A 的流"
    finally:
        assert _stop(client, ha, ws).status_code == 200


def test_reset_demo_admin_only(client):
    """/stream/reset-demo 清全局工具库 → 仅 admin 可用，普通用户 403。"""
    headers, _ = _register(client, "stE_" + uuid.uuid4().hex[:8])
    r = client.post("/stream/reset-demo", headers=headers)
    assert r.status_code == 403, r.text
