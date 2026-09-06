# -*- coding: utf-8 -*-
"""操作审计日志（v0.8.21）：多租户问责 —— 谁在何时对哪个业务域做了什么。

核心断言：
1. 写操作会落审计：创建 Agent 后 /audit 能查到 agent.create；
2. 认证类动作落审计：注册/登录/登出（workspace_id 为空，靠 actor_id 归属本人）；
3. 租户隔离：普通用户只看到自己可见域 + 自己的认证记录，看不到别人的私有域操作；
4. 管理员看全量：admin 的 /audit scope=all，能查到普通用户个人域的操作；
5. 指定他人私有域过滤 → 404（不暴露域是否存在）；
6. 匿名访问 /audit → 401；
7. 审计写入失败不影响主流程（db.audit 静默吞异常）。
"""
import base64
import json
import uuid

import pytest

from src.core import db


@pytest.fixture(scope="module", autouse=True)
def _seed_first_user(client):
    """首个注册用户会自动成为管理员——先占位吃掉 admin 名额，
    保证本文件后续注册出来的都是普通用户（权限断言可预期）。"""
    _register(client, "ad_seed_" + uuid.uuid4().hex[:8])


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


def _audit(client, headers, **params):
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return client.get(f"/audit?{qs}" if qs else "/audit", headers=headers)


def test_write_op_is_audited(client, admin_headers):
    """创建 Agent 后审计里能查到对应 agent.create 记录。"""
    headers, uid = _register(client, "au_u1_" + uuid.uuid4().hex[:8])
    ws_id = _personal_ws(client, headers)

    r = client.post(f"/workspaces/{ws_id}/agents", headers=headers,
                    json={"kind": "ops", "name": "审计探针Agent",
                          "scope": ["测试域"], "description": "用于审计测试"})
    assert r.status_code == 200, r.text

    d = _audit(client, headers, workspace_id=ws_id, limit=50).json()
    assert d["scope"] == "own"
    hits = [i for i in d["items"] if i["action"] == "agent.create"]
    assert hits, f"未审计到 agent.create：{[i['action'] for i in d['items']]}"
    top = hits[0]
    assert top["workspace_id"] == ws_id
    assert top["result"] == "ok"
    assert "审计探针Agent" in (top["detail"] or "")


def test_auth_actions_audited(client):
    """注册/登录/登出落到审计（workspace_id 为空，靠 actor_id 归属本人）。"""
    username = "au_auth_" + uuid.uuid4().hex[:8]
    headers, uid = _register(client, username)

    # 登录（重新登一次，确保有一条 login 记录）
    r = client.post("/auth/login", json={"username": username, "password": "Pytest123456"})
    assert r.status_code == 200, r.text

    d = _audit(client, headers, limit=100).json()
    actions = {i["action"] for i in d["items"]}
    assert "auth.register" in actions, f"缺注册审计：{actions}"
    assert "auth.login" in actions, f"缺登录审计：{actions}"
    login = next(i for i in d["items"] if i["action"] == "auth.login")
    assert login["workspace_id"] is None
    assert login["actor_id"] == uid

    # 登出（登出会把当前 token 拉黑，故先取一个新 token 再查审计）
    r = client.post("/auth/logout", headers=headers)
    assert r.status_code == 200, r.text
    fresh = client.post("/auth/login",
                        json={"username": username, "password": "Pytest123456"}).json()["token"]
    fh = {"Authorization": "Bearer " + fresh}
    d = _audit(client, fh, limit=100).json()
    assert "auth.logout" in {i["action"] for i in d["items"]}


def test_denied_op_is_audited(client):
    """越权写操作被 403 拒绝时也要留痕（result=denied），便于事后追责。"""
    headers, _ = _register(client, "au_deny_" + uuid.uuid4().hex[:8])
    # 公共域 core-net 仅 admin 可写 → 普通用户 403
    r = client.post("/workspaces/core-net/agents", headers=headers,
                    json={"kind": "ops", "name": "越权Agent"})
    assert r.status_code == 403, r.text

    d = _audit(client, headers, limit=100).json()
    denied = [i for i in d["items"] if i["result"] == "denied"]
    assert denied, "越权操作未落审计，无法事后追责"
    assert any(i["action"] == "agent.create" for i in denied)


def test_normal_user_cannot_see_other_private_ws(client):
    """普通用户看不到别人私有域的审计记录。"""
    h_a, _ = _register(client, "au_a_" + uuid.uuid4().hex[:8])
    h_b, _ = _register(client, "au_b_" + uuid.uuid4().hex[:8])
    ws_a = _personal_ws(client, h_a)

    # A 在自己域里动一下，产生一条审计
    r = client.post(f"/workspaces/{ws_a}/agents", headers=h_a,
                    json={"kind": "ops", "name": "A的私有Agent"})
    assert r.status_code == 200, r.text

    # B 的审计列表里不应出现 A 的私有域
    d = _audit(client, h_b, limit=200).json()
    assert d["scope"] == "own"
    assert all(i["workspace_id"] != ws_a for i in d["items"]), \
        "B 看到了 A 私有域的审计记录，租户隔离失效"

    # B 显式指定 A 的私有域过滤 → 404（不暴露域存在）
    r = _audit(client, h_b, workspace_id=ws_a)
    assert r.status_code == 404, r.text


def test_admin_sees_all(client, admin_headers):
    """管理员 scope=all，能看到普通用户个人域的操作。"""
    h_a, _ = _register(client, "au_adm_" + uuid.uuid4().hex[:8])
    ws_a = _personal_ws(client, h_a)
    r = client.post(f"/workspaces/{ws_a}/agents", headers=h_a,
                    json={"kind": "dev", "name": "管理员可见Agent"})
    assert r.status_code == 200, r.text

    d = _audit(client, admin_headers, workspace_id=ws_a, limit=50).json()
    assert d["scope"] == "all"
    assert any(i["action"] == "agent.create" for i in d["items"])


def test_anonymous_audit_requires_login(client):
    """匿名访问审计日志 → 401。"""
    r = client.get("/audit")
    assert r.status_code == 401, r.text


def test_audit_write_failure_does_not_break_flow(client):
    """审计写入失败必须静默吞掉，绝不影响主业务流程。"""
    assert db.audit("probe", "test.action", workspace_id="no-such-ws",
                    detail={"x": 1}) is None
    # 传入不可序列化的对象也不会抛（内部 try/except 兜住）
    db.audit("probe", "test.action", detail=object())
